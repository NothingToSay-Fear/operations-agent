"""可复现的业务事实生成器，禁止注册为 Agent 工具。"""

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import random

from sqlalchemy import func, inspect, select

from app.commerce_models import (
    Campaign,
    CommerceBase,
    Dataset,
    InventoryMovement,
    MarketingDay,
    Order,
    OrderLine,
    Payment,
    PriceHistory,
    Product,
    Purchase,
    Refund,
    Shipment,
    RawImport,
    Visit,
)
from app.config import get_settings
from app.db import make_engine

CHANNELS = ["自然搜索", "付费搜索", "内容推荐", "直接访问", "联盟推广"]
SCENARIOS = [
    "baseline",
    "traffic_drop",
    "stockout",
    "refund_wave",
    "promotion_margin",
    "supply_delay",
    "mixed",
]


def generate(
    *, seed=42, days=180, sku_count=200, order_target=50000, as_of=date(2026, 9, 28), scenario="mixed"
):
    if not 28 <= days <= 730 or not 8 <= sku_count <= 1000 or not 100 <= order_target <= 500000:
        raise ValueError("days=28..730, sku_count=8..1000, order_target=100..500000")
    if scenario not in SCENARIOS:
        raise ValueError("unknown scenario")
    rng = random.Random(seed)
    start = as_of - timedelta(days=days - 1)
    tables = defaultdict(list)
    categories = [("家居", "保温杯"), ("服饰", "棉质T恤"), ("运动", "轻量背包"), ("数码", "桌面支架")]
    products = []
    for i in range(sku_count):
        category, name = categories[(i // 4) % 4]
        price = rng.randint(39, 299) * 100
        p = dict(
            id=f"SKU-{i + 1:04}",
            spu=f"SPU-{i // 4 + 1:04}",
            name=f"{name} {i // 4 + 1:03} / {['白', '黑', '蓝', '灰'][i % 4]}",
            category=category,
            attributes={"颜色": ["白", "黑", "蓝", "灰"][i % 4], "材质": "以产品规格为准"},
            price_cents=price,
            cost_cents=price * rng.randint(42, 65) // 100,
            lead_days=rng.randint(3, 12),
            moq=rng.choice([5, 10, 20]),
        )
        products.append(p)
        tables[Product].append(p)
        tables[PriceHistory].append(
            dict(
                id=i + 1,
                product_id=p["id"],
                effective_date=start,
                price_cents=price,
                cost_cents=p["cost_cents"],
            )
        )
    weights = [1 / (i + 3) ** 0.85 for i in range(sku_count)]
    campaigns = [
        dict(
            id="CAM-01",
            name="月度精选促销",
            start_date=as_of - timedelta(days=20),
            end_date=as_of - timedelta(days=15),
            discount_percent=15,
            budget_cents=2000000,
            product_ids=[p["id"] for p in products[: sku_count // 2]],
            rule="仅指定商品，商品原价减15%，不叠加其他优惠，运费独立核算。",
        ),
        dict(
            id="CAM-02",
            name="秋日回馈",
            start_date=as_of - timedelta(days=6),
            end_date=as_of,
            discount_percent=35 if scenario in {"promotion_margin", "mixed"} else 10,
            budget_cents=3000000,
            product_ids=[p["id"] for p in products[: sku_count // 3]],
            rule="仅指定商品参与，按本活动折扣计算成交额，不承诺销量增长。",
        ),
    ]
    tables[Campaign] = campaigns
    stock = {
        p["id"]: max(25, int(order_target * weights[i] / sum(weights) * 0.18)) for i, p in enumerate(products)
    }
    warehouse = {p["id"]: f"WH-{i % 3 + 1:02}" for i, p in enumerate(products)}
    for p in products:
        tables[InventoryMovement].append(
            dict(
                id=f"OPEN-{p['id']}",
                product_id=p["id"],
                warehouse=warehouse[p["id"]],
                date=start,
                kind="opening",
                quantity=stock[p["id"]],
                reference="opening",
            )
        )
    arrivals = defaultdict(list)
    returns = defaultdict(list)
    on_order = Counter()
    order_no = visit_no = marketing_no = 0
    for day_i in range(days):
        day = start + timedelta(days=day_i)
        for pid, qty, ref in returns[day]:
            stock[pid] += qty
            tables[InventoryMovement].append(
                dict(
                    id=f"RETURN-{ref}",
                    product_id=pid,
                    warehouse=warehouse[pid],
                    date=day,
                    kind="return",
                    quantity=qty,
                    reference=ref,
                )
            )
        for purchase in arrivals[day]:
            pid = purchase["product_id"]
            stock[pid] += purchase["quantity"]
            on_order[pid] -= purchase["quantity"]
            tables[InventoryMovement].append(
                dict(
                    id=f"IN-{purchase['id']}",
                    product_id=pid,
                    warehouse=warehouse[pid],
                    date=day,
                    kind="purchase",
                    quantity=purchase["quantity"],
                    reference=purchase["id"],
                )
            )
        last_week = day > as_of - timedelta(days=7)
        stock_shock = scenario in {"stockout", "mixed"} and day >= as_of - timedelta(days=10)
        if stock_shock and day == as_of - timedelta(days=10):
            for p in products[:3]:
                pid = p["id"]
                if stock[pid]:
                    tables[InventoryMovement].append(
                        dict(
                            id=f"ADJ-{pid}",
                            product_id=pid,
                            warehouse=warehouse[pid],
                            date=day,
                            kind="adjustment",
                            quantity=-stock[pid],
                            reference="质检隔离批次",
                        )
                    )
                    stock[pid] = 0
        for i, p in enumerate(products):
            pid = p["id"]
            demand = order_target / days * weights[i] / sum(weights)
            reorder = max(8, int(demand * p["lead_days"] * 1.8))
            if stock[pid] + on_order[pid] < reorder and not (stock_shock and i < 3):
                qty = max(p["moq"], int(demand * 24) // p["moq"] * p["moq"])
                expected = day + timedelta(days=p["lead_days"])
                late = (
                    6
                    if scenario in {"supply_delay", "mixed"} and i < 8 and day > as_of - timedelta(days=20)
                    else 0
                )
                actual = expected + timedelta(days=late)
                purchase = dict(
                    id=f"PO-{len(tables[Purchase]) + 1:06}",
                    product_id=pid,
                    warehouse=warehouse[pid],
                    ordered_date=day,
                    expected_date=expected,
                    arrived_date=actual if actual <= as_of else None,
                    quantity=qty,
                )
                tables[Purchase].append(purchase)
                arrivals[actual].append(purchase)
                on_order[pid] += qty
        campaign = next((c for c in campaigns if c["start_date"] <= day <= c["end_date"]), None)
        season = (1.15 if day.weekday() >= 5 else 0.94) * (1.3 if campaign else 1)
        day_visits = max(10, int(order_target / days / 0.14 * season * rng.uniform(0.88, 1.12)))
        visits_by_channel = Counter()
        for _ in range(day_visits):
            channel = rng.choices(CHANNELS, [30, 25, 20, 15, 10])[0]
            if (
                last_week
                and scenario in {"traffic_drop", "mixed"}
                and channel == "付费搜索"
                and rng.random() < 0.7
            ):
                continue
            p = rng.choices(products, weights)[0]
            pid = p["id"]
            visit_no += 1
            customer = f"CUSTOMER-{rng.randint(1, max(500, order_target // 2)):07}"
            collected = min(as_of + timedelta(days=1), day + timedelta(days=1 if rng.random() < 0.015 else 0))
            tables[Visit].append(
                dict(
                    id=visit_no,
                    date=day,
                    collected_date=collected,
                    product_id=pid,
                    channel=channel,
                    visitor_id=customer,
                )
            )
            visits_by_channel[channel] += 1
            conversion = 0.14 * (1.2 if campaign else 1) * (1.15 if channel == "直接访问" else 1)
            if rng.random() > conversion or stock[pid] <= 0:
                continue
            order_no += 1
            oid = f"ORDER-{order_no:08}"
            paid = rng.random() > 0.06
            shipped = day + timedelta(days=rng.choice([0, 1, 1, 2])) if paid else None
            delivered = shipped + timedelta(days=rng.randint(1, 4)) if paid else None
            complete_shipped, complete_delivered = shipped, delivered
            items = [p]
            if rng.random() < 0.18:
                other = rng.choices(products, weights)[0]
                if other["id"] != pid and stock[other["id"]] > 0:
                    items.append(other)
            subtotal = discount = paid_total = 0
            for line_i, item in enumerate(items):
                ipid = item["id"]
                qty = min(stock[ipid], rng.choices([1, 2, 3], [85, 12, 3])[0])
                gross = item["price_cents"] * qty
                reduction = (
                    gross * campaign["discount_percent"] // 100
                    if campaign and ipid in campaign["product_ids"]
                    else 0
                )
                amount = gross - reduction if paid else 0
                lid = f"{oid}-{line_i + 1}"
                tables[OrderLine].append(
                    dict(
                        id=lid,
                        order_id=oid,
                        product_id=ipid,
                        quantity=qty,
                        unit_price_cents=item["price_cents"],
                        unit_cost_cents=item["cost_cents"],
                        discount_cents=reduction,
                        paid_cents=amount,
                    )
                )
                subtotal += gross
                discount += reduction
                paid_total += amount
                if not paid:
                    continue
                stock[ipid] -= qty
                tables[InventoryMovement].append(
                    dict(
                        id=f"RES-{lid}",
                        product_id=ipid,
                        warehouse=warehouse[ipid],
                        date=day,
                        kind="reserve",
                        quantity=qty,
                        reference=lid,
                    )
                )
                batches = [(qty, shipped, delivered)]
                if qty > 1 and order_no % 7 == 0:
                    batches = [
                        (1, shipped, delivered),
                        (qty - 1, shipped + timedelta(days=1), delivered + timedelta(days=1)),
                    ]
                complete_shipped = max(complete_shipped, batches[-1][1])
                complete_delivered = max(complete_delivered, batches[-1][2])
                for batch_index, (batch_qty, batch_shipped, batch_delivered) in enumerate(batches, 1):
                    if batch_shipped <= as_of:
                        ship_id = f"SHIP-{lid}-{batch_index}"
                        tables[Shipment].append(
                            dict(
                                id=ship_id,
                                order_line_id=lid,
                                shipped_date=batch_shipped,
                                delivered_date=batch_delivered if batch_delivered <= as_of else None,
                                quantity=batch_qty,
                            )
                        )
                        for kind, prefix in [("release", "REL"), ("sale", "OUT")]:
                            tables[InventoryMovement].append(
                                dict(
                                    id=f"{prefix}-{lid}-{batch_index}",
                                    product_id=ipid,
                                    warehouse=warehouse[ipid],
                                    date=batch_shipped,
                                    kind=kind,
                                    quantity=-batch_qty,
                                    reference=ship_id,
                                )
                            )
                refund_rate = (
                    0.22
                    if scenario in {"refund_wave", "mixed"}
                    and item in products[3:10]
                    and day > as_of - timedelta(days=22)
                    else 0.035
                )
                if rng.random() < refund_rate:
                    requested = batches[-1][2] + timedelta(days=rng.randint(1, 6))
                    refunded = requested + timedelta(days=rng.randint(1, 4))
                    returned = refunded + timedelta(days=3) if rng.random() < 0.65 else None
                    if refunded <= as_of:
                        rid = f"REF-{lid}"
                        refund_qty = 1
                        tables[Refund].append(
                            dict(
                                id=rid,
                                order_line_id=lid,
                                requested_date=requested,
                                refunded_date=refunded,
                                collected_date=refunded + timedelta(days=int(rng.random() < 0.05)),
                                returned_date=returned if returned and returned <= as_of else None,
                                quantity=refund_qty,
                                amount_cents=amount // qty,
                                reason=rng.choice(["尺寸不合适", "质量问题", "与描述不符", "不再需要"]),
                            )
                        )
                        if returned and returned <= as_of:
                            returns[returned].append((ipid, refund_qty, rid))
            shipping = 600 if paid and paid_total < 9900 else 0
            tables[Order].append(
                dict(
                    id=oid,
                    order_date=day,
                    collected_date=collected,
                    customer_id=customer,
                    channel=channel,
                    campaign_id=campaign["id"] if campaign else None,
                    status="paid" if paid else "cancelled",
                    subtotal_cents=subtotal,
                    discount_cents=discount,
                    shipping_cents=shipping,
                    paid_cents=paid_total + shipping,
                    paid_date=day if paid else None,
                    shipped_date=complete_shipped if complete_shipped and complete_shipped <= as_of else None,
                    delivered_date=complete_delivered
                    if complete_delivered and complete_delivered <= as_of
                    else None,
                )
            )
            if paid:
                tables[Payment].append(
                    dict(
                        id=f"PAY-{oid}",
                        order_id=oid,
                        paid_date=day,
                        amount_cents=paid_total + shipping,
                        method=rng.choice(["银行卡", "电子钱包"]),
                    )
                )
        for channel, clicks in visits_by_channel.items():
            marketing_no += 1
            # 广告导入缺失代表真实数据缺口，不能生成虚假的零花费记录。
            if day == as_of and channel == "联盟推广":
                continue
            tables[MarketingDay].append(
                dict(
                    id=marketing_no,
                    date=day,
                    collected_date=day,
                    channel=channel,
                    impressions=clicks * rng.randint(8, 20),
                    clicks=clicks,
                    spend_cents=clicks * rng.randint(90, 180) if channel in {"付费搜索", "联盟推广"} else 0,
                )
            )
    # 模拟源数据重试导入，规范事实按源业务键去重。
    canonical_orders = {}
    for order in tables[Order]:
        raw_id = f"RAW-{order['id']}"
        payload = json.loads(json.dumps(order, default=str))
        record = dict(
            id=raw_id,
            source="order_backend",
            source_record_id=order["id"],
            batch_id=f"BATCH-{order['collected_date']}",
            received_date=order["collected_date"],
            payload=payload,
            duplicate_of=None,
        )
        tables[RawImport].append(record)
        canonical_orders.setdefault(order["id"], order)
        if int(order["id"].rsplit("-", 1)[1]) % 83 == 0:
            tables[RawImport].append({**record, "id": raw_id + "-retry", "duplicate_of": raw_id})
            canonical_orders.setdefault(order["id"], order)
    tables[Order] = list(canonical_orders.values())
    manifest = dict(
        generator_version="1.1",
        seed=seed,
        simulated=True,
        start_date=str(start),
        as_of=str(as_of),
        days=days,
        sku_count=sku_count,
        orders=order_no,
        visits=visit_no,
        timezone="Asia/Shanghai",
        currency="CNY",
        metric_version="1.0",
        ingestion={
            "raw_order_records": len(tables[RawImport]),
            "deduplicated_orders": len(tables[Order]),
            "duplicate_records": len(tables[RawImport]) - len(tables[Order]),
        },
    )
    manifest["fingerprint"] = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:16]
    tables[Dataset] = [dict(id=1, meta=manifest)]
    return tables


def reconcile(tables) -> dict:
    orders = {o["id"]: o for o in tables[Order]}
    products = {p["id"] for p in tables[Product]}
    lines = {line["id"]: line for line in tables[OrderLine]}
    amounts, subtotals, discounts = Counter(), Counter(), Counter()
    for line in lines.values():
        assert line["order_id"] in orders and line["product_id"] in products
        amounts[line["order_id"]] += line["paid_cents"]
        subtotals[line["order_id"]] += line["unit_price_cents"] * line["quantity"]
        discounts[line["order_id"]] += line["discount_cents"]
    for oid, o in orders.items():
        assert amounts[oid] + o["shipping_cents"] == o["paid_cents"]
        assert subtotals[oid] == o["subtotal_cents"] and discounts[oid] == o["discount_cents"]
        if o["paid_date"]:
            assert o["paid_cents"] == o["subtotal_cents"] - o["discount_cents"] + o["shipping_cents"]
    for payment in tables[Payment]:
        assert payment["amount_cents"] == orders[payment["order_id"]]["paid_cents"]
    shipped_quantities = Counter()
    for shipment in tables[Shipment]:
        line = lines[shipment["order_line_id"]]
        shipped_quantities[line["id"]] += shipment["quantity"]
        assert shipment["shipped_date"] >= orders[line["order_id"]]["paid_date"]
        assert not shipment["delivered_date"] or shipment["delivered_date"] >= shipment["shipped_date"]
        assert shipped_quantities[line["id"]] <= line["quantity"]
    assert len({r["source_record_id"] for r in tables[RawImport]}) == len(orders)
    refunded = Counter()
    for refund in tables[Refund]:
        line = lines[refund["order_line_id"]]
        refunded[line["id"]] += refund["amount_cents"]
        assert refunded[line["id"]] <= line["paid_cents"]
        assert refund["quantity"] <= line["quantity"]
        assert refund["refunded_date"] >= refund["requested_date"] >= orders[line["order_id"]]["paid_date"]
    daily = defaultdict(Counter)
    for move in tables[InventoryMovement]:
        daily[(move["product_id"], move["warehouse"])][
            (move["date"], "reserved" if move["kind"] in {"reserve", "release"} else "physical")
        ] += move["quantity"]
    for balances in daily.values():
        physical = reserved = 0
        for day in sorted({key[0] for key in balances}):
            physical += balances[(day, "physical")]
            reserved += balances[(day, "reserved")]
            assert physical >= 0 and reserved >= 0 and physical >= reserved, (day, physical, reserved)
    return {
        "passed": True,
        "orders": len(orders),
        "order_lines": len(lines),
        "refunds": len(tables[Refund]),
        "inventory_positions": len(daily),
        "checks": [
            "foreign_keys",
            "order_amounts",
            "payments",
            "refund_bounds",
            "daily_inventory",
            "split_shipments",
            "ingestion_deduplication",
        ],
    }


async def seed_database(url: str, **options):
    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            exists = await conn.run_sync(lambda c: inspect(c).has_table("dataset"))
            if exists and await conn.scalar(select(func.count()).select_from(Dataset)):
                raise ValueError("数据集已存在；请使用新的数据库路径生成其他情境，不自动覆盖经营数据。")
            await conn.run_sync(CommerceBase.metadata.create_all)
            if await conn.scalar(select(func.count()).select_from(Dataset)):
                raise ValueError("数据集已存在；请使用新的数据库路径生成其他情境，不自动覆盖经营数据。")
            tables = generate(**options)
            report = reconcile(tables)
            for table in CommerceBase.metadata.sorted_tables:
                model = next((cls for cls in tables if cls.__table__ is table), None)
                if model is None:
                    # 小规模评测集可能没有某类可选事实，例如分批履约。
                    continue
                rows = tables[model]
                for offset in range(0, len(rows), 1000):
                    await conn.execute(table.insert(), rows[offset : offset + 1000])
        return {"manifest": tables[Dataset][0]["meta"], "reconciliation": report}
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--skus", type=int, default=200)
    parser.add_argument("--orders", type=int, default=50000)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 9, 28))
    parser.add_argument("--scenario", choices=SCENARIOS, default="mixed")
    parser.add_argument("--url", default=None)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()
    report = asyncio.run(
        seed_database(
            args.url or get_settings().commerce_admin_url,
            seed=args.seed,
            days=args.days,
            sku_count=args.skus,
            order_target=args.orders,
            as_of=args.as_of,
            scenario=args.scenario,
        )
    )
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(output, encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
