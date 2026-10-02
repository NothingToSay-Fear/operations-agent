"""只读的结构化经营查询与库存风险汇总。"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from math import ceil
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import case, distinct, func, select

from app.commerce_models import (
    Campaign,
    Dataset,
    InventoryMovement,
    MarketingDay,
    Order,
    OrderLine,
    Product,
    Purchase,
    Refund,
    Shipment,
    Visit,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Query(StrictModel):
    start_date: date
    end_date: date
    as_of: date | None = None
    group_by: str = Field(default="total", pattern="^(total|day|channel|product)$")
    product_ids: list[str] = Field(default_factory=list, max_length=30)
    channels: list[str] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=30, ge=1, le=100)
    include_shipments: bool = False

    @model_validator(mode="after")
    def valid_period(self):
        if self.end_date < self.start_date or (self.end_date - self.start_date).days > 730:
            raise ValueError("时间范围无效或超过730天")
        return self


class PeriodComparison(Query):
    previous_start_date: date
    previous_end_date: date
    group_by: Literal["channel", "product"] = "channel"
    metric: Literal["paid_gmv", "units", "refund_amount", "gross_profit_before_refunds"] = "paid_gmv"
    limit: int = Field(default=10, ge=1, le=30)

    @model_validator(mode="after")
    def comparable_periods(self):
        if self.previous_end_date >= self.start_date:
            raise ValueError("对比周期必须早于当前周期且不能重叠")
        if self.previous_end_date - self.previous_start_date != self.end_date - self.start_date:
            raise ValueError("对比周期必须与当前周期等长")
        return self


class ProductQuery(StrictModel):
    product_ids: list[str] = Field(default_factory=list, max_length=30)
    search: str = Field(default="", max_length=100)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=20, ge=1, le=100)


class InventoryQuery(StrictModel):
    as_of: date | None = None
    product_ids: list[str] = Field(default_factory=list, max_length=30)
    demand_days: int = Field(default=14, ge=1, le=90)
    limit: int = Field(default=30, ge=1, le=100)
    include_movements: bool = False


def inventory_risk_summary(rows: list[dict], as_of: date, demand_days: int, maximum_rows: int = 10) -> dict:
    # 先在数据库侧聚合，再按交期、在途、最小订货量和需求假设计算补货建议。
    """将全量库存仓位转换为可直接用于补货判断的受控风险摘要。"""
    horizon_end = as_of + timedelta(days=demand_days)
    risks = []
    for row in rows:
        daily = Decimal(str(row["observed_daily_units"]))
        if daily <= 0:
            continue
        demand_units = daily * demand_days
        arrivals = row["in_transit"]
        reliable_transit = sum(
            item["quantity"]
            for item in arrivals
            if not item["overdue"] and date.fromisoformat(item["expected_date"]) <= horizon_end
        )
        overdue_transit = sum(item["quantity"] for item in arrivals if item["overdue"])
        supply_units = Decimal(str(row["available"] + reliable_transit))
        shortage_units = max(Decimal(0), demand_units - supply_units)
        if shortage_units <= 0:
            continue
        moq = int(row["minimum_order"])
        suggested_order = ceil(float(shortage_units) / moq) * moq
        coverage = row["coverage_days"]
        stockout_before_lead = coverage is not None and Decimal(str(coverage)) < Decimal(str(row["lead_days"]))
        risks.append(
            {
                "product_id": row["product_id"],
                "name": row["name"],
                "warehouse": row["warehouse"],
                "available": row["available"],
                "observed_daily_units": row["observed_daily_units"],
                "coverage_days": coverage,
                "lead_days": row["lead_days"],
                "minimum_order": moq,
                "demand_units_next_window": round(float(demand_units), 3),
                "reliable_in_transit_units": reliable_transit,
                "overdue_in_transit_units": overdue_transit,
                "supply_units_next_window": round(float(supply_units), 3),
                "shortage_units": round(float(shortage_units), 3),
                "suggested_order_units": suggested_order,
                "stockout_before_lead": stockout_before_lead,
                "expected_arrivals": [
                    {
                        "quantity": item["quantity"],
                        "expected_date": item["expected_date"],
                        "overdue": item["overdue"],
                    }
                    for item in arrivals
                ],
            }
        )
    risks.sort(
        key=lambda item: (
            not item["stockout_before_lead"],
            item["coverage_days"] if item["coverage_days"] is not None else float("inf"),
            -item["shortage_units"],
            item["product_id"],
        )
    )
    return {
        "as_of": str(as_of),
        "horizon_end": str(horizon_end),
        "demand_days": demand_days,
        "demand_assumption": "未来需求按截止日前 demand_days 天的已支付销量日均值外推。",
        "supply_assumption": "供给=当前可用库存+窗口内预计到货且未逾期的在途；逾期在途单独列示，不计入可靠供给。",
        "order_assumption": "建议补货量按窗口缺口向上取整到最小订货量的整数倍。",
        "risk_positions": len(risks),
        "shown_positions": min(len(risks), maximum_rows),
        "truncated": len(risks) > maximum_rows,
        "rows": risks[:maximum_rows],
    }


METRICS = {
    "paid_gmv": {
        "name": "支付商品GMV",
        "unit": "CNY",
        "formula": "sum(paid order line amount) / 100",
        "note": "不含运费，不扣退款；按支付日",
    },
    "paid_orders": {
        "name": "支付订单数",
        "unit": "orders",
        "formula": "count(distinct paid order id)",
        "note": "商品分组订单数不可相加为总体订单数",
    },
    "visitors": {
        "name": "去重访客",
        "unit": "people",
        "formula": "count(distinct visitor_id)",
        "note": "按查询周期去重；各组不可直接相加",
    },
    "sessions": {
        "name": "访问次数",
        "unit": "sessions",
        "formula": "count(visits)",
        "note": "可累加，与去重访客不同",
    },
    "order_visitor_ratio": {
        "name": "支付订单/访客比率",
        "unit": "ratio",
        "formula": "paid_orders / visitors",
        "note": "不是支付人数转化率；零分母返回null",
    },
    "aov": {"name": "商品客单价", "unit": "CNY", "formula": "paid_gmv / paid_orders", "note": "不含运费"},
    "refund_amount": {
        "name": "期间退款到账额",
        "unit": "CNY",
        "formula": "sum(refunds at settlement date) / 100",
        "note": "可能来自此前订单，不代表同期订单退款率",
    },
    "gross_profit_before_refunds": {
        "name": "退款前商品毛利",
        "unit": "CNY",
        "formula": "paid_gmv - historical product cost",
        "note": "未扣退款、广告、运费及其他费用；不是净利润",
    },
}


async def capabilities(session):
    data = await session.get(Dataset, 1)
    if not data:
        raise ValueError("尚未初始化模拟经营数据")
    return {
        **data.meta,
        "dimensions": ["day", "channel", "product"],
        "channels": ["自然搜索", "付费搜索", "内容推荐", "直接访问", "联盟推广"],
        "sources": [
            "orders",
            "order_lines",
            "payments",
            "refunds",
            "inventory_movements",
            "purchases",
            "visits",
            "marketing_daily",
            "campaigns",
        ],
        "warnings": [
            "全部数据为模拟数据",
            "存在迟到数据，统计受采集截止时间约束",
            "不提供未经验证的因果结论",
        ],
    }


async def cutoff(session, requested=None):
    meta = await capabilities(session)
    latest = date.fromisoformat(meta["as_of"])
    if requested and requested > latest:
        raise ValueError(f"数据截止 {latest}，不能访问未来事实")
    return requested or latest, meta


def scope(q, date_column, product_column, channel_column):
    conditions = [date_column.between(q.start_date, q.end_date)]
    if q.product_ids:
        conditions.append(product_column.in_(q.product_ids))
    if q.channels:
        conditions.append(channel_column.in_(q.channels))
    return conditions


def dimension(q, day, product, channel):
    return {"day": day, "product": product, "channel": channel}.get(q.group_by)


async def aggregate(session, cols, source, conditions, group):
    statement = (
        select(*(([group.label("group")] if group is not None else []) + cols))
        .select_from(source)
        .where(*conditions)
    )
    if group is not None:
        statement = statement.group_by(group)
    rows = (await session.execute(statement)).mappings().all()
    return {str(row["group"]) if group is not None else "total": dict(row) for row in rows}


async def query_metrics(session, q: Query, *, all_groups=False):
    # 指标查询只接受结构化筛选条件，不拼接用户提供的 SQL 或列名。
    as_of, meta = await cutoff(session, q.as_of)
    if q.end_date > as_of:
        raise ValueError("查询结束日期超过数据截止时间")
    paid = Order.__table__.join(OrderLine, Order.id == OrderLine.order_id)
    grouped = await aggregate(
        session,
        [
            func.sum(OrderLine.paid_cents).label("gmv_cents"),
            func.sum(OrderLine.unit_cost_cents * OrderLine.quantity).label("cost_cents"),
            func.count(distinct(Order.id)).label("paid_orders"),
            func.sum(OrderLine.quantity).label("units"),
        ],
        paid,
        scope(q, Order.paid_date, OrderLine.product_id, Order.channel)
        + [Order.paid_date.is_not(None), Order.collected_date <= as_of],
        dimension(q, Order.paid_date, OrderLine.product_id, Order.channel),
    )
    traffic = await aggregate(
        session,
        [func.count(Visit.id).label("sessions"), func.count(distinct(Visit.visitor_id)).label("visitors")],
        Visit,
        scope(q, Visit.date, Visit.product_id, Visit.channel) + [Visit.collected_date <= as_of],
        dimension(q, Visit.date, Visit.product_id, Visit.channel),
    )
    refund_source = Refund.__table__.join(OrderLine, Refund.order_line_id == OrderLine.id).join(
        Order, OrderLine.order_id == Order.id
    )
    refunds = await aggregate(
        session,
        [func.sum(Refund.amount_cents).label("refund_cents"), func.count(Refund.id).label("refund_cases")],
        refund_source,
        scope(q, Refund.refunded_date, OrderLine.product_id, Order.channel)
        + [Refund.collected_date <= as_of, Order.collected_date <= as_of],
        dimension(q, Refund.refunded_date, OrderLine.product_id, Order.channel),
    )
    result = []
    for key in grouped.keys() | traffic.keys() | refunds.keys():
        g, t, r = grouped.get(key, {}), traffic.get(key, {}), refunds.get(key, {})
        gmv, cost, count = g.get("gmv_cents") or 0, g.get("cost_cents") or 0, g.get("paid_orders") or 0
        visitors = t.get("visitors") or 0
        result.append(
            dict(
                group=key,
                paid_gmv=gmv / 100,
                paid_orders=count,
                units=g.get("units") or 0,
                visitors=visitors,
                sessions=t.get("sessions") or 0,
                order_visitor_ratio=round(count / visitors, 6) if visitors else None,
                aov=round(gmv / count / 100, 2) if count else None,
                refund_amount=(r.get("refund_cents") or 0) / 100,
                refund_cases=r.get("refund_cases") or 0,
                gross_profit_before_refunds=(gmv - cost) / 100,
            )
        )
    result.sort(
        key=(lambda x: x["group"]) if q.group_by == "day" else (lambda x: (-x["paid_gmv"], x["group"]))
    )
    return dict(
        rows=result if all_groups else result[: q.limit],
        total_groups=len(result),
        truncated=not all_groups and len(result) > q.limit,
        scope=q.model_dump(mode="json"),
        as_of=str(as_of),
        simulated=True,
        metric_version=meta["metric_version"],
        warnings=[
            "按采集截止时间过滤；近期数据可能未齐",
            "支付GMV不含运费，退款按到账日单独统计",
            "各分组去重访客/订单不可直接相加",
        ],
    )


async def compare_metrics(session, q: PeriodComparison):
    # 对比查询同时返回两个周期及分组差额，供 Agent 生成可追溯的贡献结论。
    """先对齐全部分组并计算差额，再选主要贡献项，避免分别取两期头部造成遗漏。"""
    params = q.model_dump(exclude={"previous_start_date", "previous_end_date", "metric"})
    current = await query_metrics(session, Query(**params), all_groups=True)
    previous = await query_metrics(
        session,
        Query(**{**params, "start_date": q.previous_start_date, "end_date": q.previous_end_date}),
        all_groups=True,
    )
    before = {row["group"]: Decimal(str(row[q.metric])) for row in previous["rows"]}
    after = {row["group"]: Decimal(str(row[q.metric])) for row in current["rows"]}
    old_total, new_total = sum(before.values(), Decimal(0)), sum(after.values(), Decimal(0))
    delta = new_total - old_total
    rows = []
    for group in before.keys() | after.keys():
        old, new = before.get(group, Decimal(0)), after.get(group, Decimal(0))
        difference = new - old
        rows.append(
            {
                "group": group,
                "previous": float(old),
                "current": float(new),
                "delta": float(difference),
                "change_percent": round(float(difference / old * 100), 4) if old else None,
                "net_contribution_percent": round(float(difference / delta * 100), 4) if delta else None,
            }
        )
    rows.sort(key=lambda row: (-abs(row["delta"]), row["group"]))
    selected = rows[: q.limit]
    return {
        "rows": selected,
        "metric": q.metric,
        "group_by": q.group_by,
        "previous_total": float(old_total),
        "current_total": float(new_total),
        "delta": float(delta),
        "change_percent": round(float(delta / old_total * 100), 4) if old_total else None,
        "other_delta": float(delta - sum((Decimal(str(row["delta"])) for row in selected), Decimal(0))),
        "total_groups": len(rows),
        "truncated": len(rows) > q.limit,
        "scope": q.model_dump(mode="json"),
        "as_of": current["as_of"],
        "simulated": True,
        "metric_version": current["metric_version"],
        "warnings": current["warnings"]
        + [
            "基于全量分组差额排序；未展示分组的合计差额见 other_delta",
            "净变化较小时贡献比例可超过100%；算术贡献不代表已验证的因果关系",
        ],
    }


async def get_products(session, q: ProductQuery):
    statement = select(Product)
    if q.product_ids:
        statement = statement.where(Product.id.in_(q.product_ids))
    if q.search:
        statement = statement.where(Product.name.contains(q.search, autoescape=True))
    rows = (await session.scalars(statement.order_by(Product.id).offset(q.offset).limit(q.limit + 1))).all()
    return {
        "rows": [
            dict(
                id=p.id,
                spu=p.spu,
                name=p.name,
                category=p.category,
                attributes=p.attributes,
                price=p.price_cents / 100,
                cost=p.cost_cents / 100,
                lead_days=p.lead_days,
                minimum_order=p.moq,
            )
            for p in rows[: q.limit]
        ],
        "has_more": len(rows) > q.limit,
        "offset": q.offset,
        "simulated": True,
        "currency": "CNY",
    }


async def query_inventory(session, q: InventoryQuery):
    # 库存查询在服务端完成风险汇总，避免大表分页结果迫使模型反复读取。
    as_of, _ = await cutoff(session, q.as_of)
    reserved = InventoryMovement.kind.in_(["reserve", "release"])
    statement = select(
        InventoryMovement.product_id,
        InventoryMovement.warehouse,
        func.sum(case((~reserved, InventoryMovement.quantity), else_=0)).label("physical"),
        func.sum(case((reserved, InventoryMovement.quantity), else_=0)).label("reserved"),
    ).where(InventoryMovement.date <= as_of)
    if q.product_ids:
        statement = statement.where(InventoryMovement.product_id.in_(q.product_ids))
    positions = (
        (await session.execute(statement.group_by(InventoryMovement.product_id, InventoryMovement.warehouse)))
        .mappings()
        .all()
    )
    demand_statement = (
        select(OrderLine.product_id, func.sum(OrderLine.quantity))
        .join(Order)
        .where(
            Order.paid_date.between(as_of - timedelta(days=q.demand_days - 1), as_of),
            Order.collected_date <= as_of,
        )
        .group_by(OrderLine.product_id)
    )
    if q.product_ids:
        demand_statement = demand_statement.where(OrderLine.product_id.in_(q.product_ids))
    demand = {pid: units / q.demand_days for pid, units in (await session.execute(demand_statement)).all()}
    products = {p.id: p for p in (await session.scalars(select(Product))).all()}
    pending = (
        await session.scalars(
            select(Purchase).where(
                Purchase.ordered_date <= as_of,
                (Purchase.arrived_date.is_(None)) | (Purchase.arrived_date > as_of),
            )
        )
    ).all()
    in_transit = defaultdict(list)
    for p in pending:
        in_transit[(p.product_id, p.warehouse)].append(
            dict(
                id=p.id,
                quantity=p.quantity,
                expected_date=str(p.expected_date),
                overdue=p.expected_date < as_of,
            )
        )
    result = []
    for row in positions:
        p = products[row["product_id"]]
        available = row["physical"] - row["reserved"]
        daily = demand.get(p.id, 0)
        result.append(
            dict(
                product_id=p.id,
                name=p.name,
                warehouse=row["warehouse"],
                physical=row["physical"],
                reserved=row["reserved"],
                available=available,
                in_transit=in_transit[(p.id, row["warehouse"])],
                observed_daily_units=round(daily, 3),
                coverage_days=round(available / daily, 2) if daily else None,
                lead_days=p.lead_days,
                minimum_order=p.moq,
            )
        )
    result.sort(
        key=lambda r: (r["coverage_days"] if r["coverage_days"] is not None else 1e9, r["product_id"])
    )
    movements = []
    if q.include_movements:
        movement_query = select(InventoryMovement).where(
            InventoryMovement.date.between(as_of - timedelta(days=q.demand_days - 1), as_of)
        )
        if q.product_ids:
            movement_query = movement_query.where(InventoryMovement.product_id.in_(q.product_ids))
        moves = (
            await session.scalars(
                movement_query.order_by(InventoryMovement.date.desc(), InventoryMovement.id).limit(100)
            )
        ).all()
        movements = [
            dict(
                product_id=m.product_id,
                warehouse=m.warehouse,
                date=str(m.date),
                kind=m.kind,
                quantity=m.quantity,
                reference=m.reference,
            )
            for m in moves
        ]
    return dict(
        rows=result[: q.limit],
        risk_summary=inventory_risk_summary(result, as_of, q.demand_days),
        movements=movements,
        movements_limit=100,
        total_positions=len(result),
        truncated=len(result) > q.limit,
        as_of=str(as_of),
        demand_days=q.demand_days,
        simulated=True,
        warnings=[
            "历史销量可能因缺货受到截断，不等同于真实需求",
            "在途仅显示当时已下单且未到货的信息，不使用未来实际到货日",
            "覆盖天数为当前可用库存/历史日均销量，未计未来活动",
        ],
    )


async def query_order_facts(session, q: Query):
    as_of, _ = await cutoff(session, q.as_of)
    if q.end_date > as_of:
        raise ValueError("查询结束日期超过数据截止时间")
    statement = (
        select(
            Order.id,
            Order.order_date,
            Order.channel,
            OrderLine.product_id,
            OrderLine.quantity,
            OrderLine.paid_cents,
            OrderLine.discount_cents,
            Order.shipped_date,
            Order.delivered_date,
        )
        .join(OrderLine)
        .where(
            *scope(q, Order.order_date, OrderLine.product_id, Order.channel), Order.collected_date <= as_of
        )
        .order_by(Order.order_date.desc(), Order.id)
        .limit(q.limit + 1)
    )
    rows = (await session.execute(statement)).mappings().all()
    samples = []
    for row in rows[: q.limit]:
        samples.append(
            {
                k: (str(v) if isinstance(v, date) else v)
                for k, v in row.items()
                if k not in {"shipped_date", "delivered_date"}
            }
            | {
                "shipped_date": str(row["shipped_date"])
                if row["shipped_date"] and row["shipped_date"] <= as_of
                else None,
                "delivered_date": str(row["delivered_date"])
                if row["delivered_date"] and row["delivered_date"] <= as_of
                else None,
            }
        )
    refund_stmt = (
        select(
            Refund.reason,
            OrderLine.product_id,
            func.count(Refund.id).label("cases"),
            func.sum(Refund.amount_cents).label("amount_cents"),
        )
        .join(OrderLine)
        .join(Order)
        .where(
            *scope(q, Refund.refunded_date, OrderLine.product_id, Order.channel),
            Refund.collected_date <= as_of,
            Order.collected_date <= as_of,
        )
        .group_by(Refund.reason, OrderLine.product_id)
    )
    refunds = [
        dict(row)
        for row in (
            await session.execute(refund_stmt.order_by(func.sum(Refund.amount_cents).desc()).limit(q.limit))
        ).mappings()
    ]
    shipments = []
    if q.include_shipments:
        shipment_statement = (
            select(Shipment, OrderLine.product_id, OrderLine.order_id)
            .join(OrderLine)
            .join(Order)
            .where(
                *scope(q, Shipment.shipped_date, OrderLine.product_id, Order.channel),
                Order.collected_date <= as_of,
                Shipment.shipped_date <= as_of,
            )
            .order_by(Shipment.shipped_date.desc(), Shipment.id)
            .limit(q.limit)
        )
        shipments = [
            dict(
                id=s.id,
                order_id=oid,
                product_id=pid,
                quantity=s.quantity,
                shipped_date=str(s.shipped_date),
                delivered_date=str(s.delivered_date)
                if s.delivered_date and s.delivered_date <= as_of
                else None,
            )
            for s, pid, oid in (await session.execute(shipment_statement)).all()
        ]
    return dict(
        order_line_samples=samples,
        shipment_samples=shipments,
        samples_truncated=len(rows) > q.limit,
        refund_breakdown=refunds,
        scope=q.model_dump(mode="json"),
        as_of=str(as_of),
        simulated=True,
        warnings=[
            "订单样本不是全量，不能直接用样本计算总体指标",
            "退款按到账时间筛选，与订单创建时间窗口不同",
            "金额字段单位为分",
        ],
    )


async def query_marketing(session, q: Query):
    if q.product_ids or q.group_by == "product":
        raise ValueError("当前投放费用仅支持日期/渠道维度，无法按商品分摊")
    as_of, _ = await cutoff(session, q.as_of)
    if q.end_date > as_of:
        raise ValueError("查询结束日期超过数据截止时间")
    group = dimension(q, MarketingDay.date, None, MarketingDay.channel)
    conditions = [MarketingDay.date.between(q.start_date, q.end_date), MarketingDay.collected_date <= as_of]
    if q.channels:
        conditions.append(MarketingDay.channel.in_(q.channels))
    rows = await aggregate(
        session,
        [
            func.sum(MarketingDay.impressions).label("impressions"),
            func.sum(MarketingDay.clicks).label("clicks"),
            func.sum(MarketingDay.spend_cents).label("spend_cents"),
            func.count().label("daily_records"),
        ],
        MarketingDay,
        conditions,
        group,
    )
    campaigns = (
        await session.scalars(
            select(Campaign).where(
                Campaign.start_date <= as_of,
                Campaign.end_date >= q.start_date,
                Campaign.start_date <= q.end_date,
            )
        )
    ).all()
    return dict(
        rows=[dict(group=k, **{a: b for a, b in v.items() if a != "group"}) for k, v in sorted(rows.items())][
            : q.limit
        ],
        campaigns=[
            dict(
                id=c.id,
                name=c.name,
                start=str(c.start_date),
                end=str(c.end_date),
                discount_percent=c.discount_percent,
                budget_cents=c.budget_cents,
                product_ids=c.product_ids,
                rule=c.rule,
            )
            for c in campaigns
        ],
        as_of=str(as_of),
        simulated=True,
        warnings=["金额单位为分；不将订单收入伪装为平台归因收入", "缺少日记录不等于零花费"],
    )
