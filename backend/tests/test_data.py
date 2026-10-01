from datetime import date
import json

import pytest
from sqlalchemy import func, select, text

from app.analytics import (
    InventoryQuery,
    Query,
    get_products,
    ProductQuery,
    query_inventory,
    query_metrics,
    query_order_facts,
)
from app.commerce_models import CommerceBase, Order, OrderLine, Visit
from app.seed import SCENARIOS, generate, reconcile


@pytest.mark.parametrize("group_by", ["channel", "product"])
async def test_period_comparison_reconciles_all_groups_before_top_limit(database, group_by):
    from decimal import Decimal
    from app.analytics import PeriodComparison, compare_metrics

    periods = dict(
        start_date=date(2026, 9, 22),
        end_date=date(2026, 9, 28),
        previous_start_date=date(2026, 9, 15),
        previous_end_date=date(2026, 9, 21),
    )
    async with database.read_sessions() as session:
        result = await compare_metrics(session, PeriodComparison(**periods, group_by=group_by, limit=1))
        before = await query_metrics(
            session, Query(start_date=periods["previous_start_date"], end_date=periods["previous_end_date"])
        )
        after = await query_metrics(
            session, Query(start_date=periods["start_date"], end_date=periods["end_date"])
        )
    assert result["previous_total"] == before["rows"][0]["paid_gmv"]
    assert result["current_total"] == after["rows"][0]["paid_gmv"]
    assert result["total_groups"] > len(result["rows"]) == 1
    assert sum((Decimal(str(row["delta"])) for row in result["rows"]), Decimal(0)) + Decimal(
        str(result["other_delta"])
    ) == Decimal(str(result["delta"]))


async def test_period_comparison_handles_disappeared_new_and_zero_net_groups(monkeypatch):
    from app import analytics

    async def source(session, query, *, all_groups=False):
        assert all_groups
        values = {"A": 200, "B": 0, "C": 10} if query.start_date.day == 22 else {"A": 0, "B": 200, "D": 10}
        return {
            "rows": [{"group": key, "paid_gmv": value} for key, value in values.items()],
            "as_of": "2026-09-28",
            "metric_version": "1.0",
            "warnings": [],
        }

    monkeypatch.setattr(analytics, "query_metrics", source)
    result = await analytics.compare_metrics(
        None,
        analytics.PeriodComparison(
            start_date=date(2026, 9, 22),
            end_date=date(2026, 9, 28),
            previous_start_date=date(2026, 9, 15),
            previous_end_date=date(2026, 9, 21),
            group_by="product",
            limit=1,
        ),
    )
    assert result["total_groups"] == 4 and result["delta"] == 0
    assert result["rows"][0]["group"] == "A" and result["rows"][0]["delta"] == 200
    assert result["rows"][0]["change_percent"] is None
    assert result["rows"][0]["net_contribution_percent"] is None
    assert result["other_delta"] == -200


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_reproducible_reconciled_scenarios(scenario):
    kwargs = dict(seed=12, days=28, sku_count=12, order_target=250, scenario=scenario)
    first, second = generate(**kwargs), generate(**kwargs)
    assert reconcile(first)["passed"]
    for model, rows in first.items():
        assert rows == second[model]
        assert "scenario" not in json.dumps(rows, default=str)
    for table in CommerceBase.metadata.tables.values():
        assert "shop_id" not in table.c and "store_id" not in table.c


async def test_metrics_reconcile_to_fact_tables_and_period_unique(database):
    async with database.read_sessions() as session:
        q = Query(start_date=date(2026, 9, 1), end_date=date(2026, 9, 28))
        result = (await query_metrics(session, q))["rows"][0]
        amount = await session.scalar(
            select(func.sum(OrderLine.paid_cents)).join(Order).where(Order.collected_date <= q.end_date)
        )
        visitors = await session.scalar(
            select(func.count(func.distinct(Visit.visitor_id))).where(Visit.collected_date <= q.end_date)
        )
        assert result["paid_gmv"] == amount / 100
        assert result["visitors"] == visitors
        assert result["sessions"] >= visitors
        assert result["aov"] == round(result["paid_gmv"] / result["paid_orders"], 2)
        daily = await query_metrics(session, q.model_copy(update={"group_by": "day"}))
        assert sum(row["visitors"] for row in daily["rows"]) > visitors


async def test_readonly_connection_rejects_writes(database):
    async with database.read_sessions() as session:
        with pytest.raises(Exception):
            await session.execute(text("UPDATE products SET price_cents=0"))


async def test_queries_cutoff_and_inventory(database):
    async with database.read_sessions() as session:
        with pytest.raises(ValueError, match="截止"):
            await query_metrics(session, Query(start_date=date(2026, 9, 1), end_date=date(2026, 10, 1)))
        inventory = await query_inventory(session, InventoryQuery(as_of=date(2026, 9, 14)))
        assert all(r["available"] == r["physical"] - r["reserved"] >= 0 for r in inventory["rows"])
        summary = inventory["risk_summary"]
        assert summary["as_of"] == "2026-09-14"
        assert summary["horizon_end"] == "2026-09-28"
        assert summary["risk_positions"] >= summary["shown_positions"]
        assert all(row["shortage_units"] > 0 for row in summary["rows"])
        assert all(
            row["suggested_order_units"] % row["minimum_order"] == 0
            for row in summary["rows"]
        )
        assert "arrived_date" not in json.dumps(inventory)
        facts = await query_order_facts(
            session, Query(start_date=date(2026, 9, 1), end_date=date(2026, 9, 10), as_of=date(2026, 9, 10))
        )
        assert all(
            not r["delivered_date"] or r["delivered_date"] <= "2026-09-10"
            for r in facts["order_line_samples"]
        )
        assert (await get_products(session, ProductQuery(search="%")))["rows"] == []


async def test_shipment_and_inventory_movement_evidence(database):
    async with database.read_sessions() as session:
        facts = await query_order_facts(
            session, Query(start_date=date(2026, 9, 1), end_date=date(2026, 9, 28), include_shipments=True)
        )
        assert facts["shipment_samples"]
        assert all(s["shipped_date"] <= "2026-09-28" for s in facts["shipment_samples"])
        inventory = await query_inventory(
            session, InventoryQuery(product_ids=["SKU-0001"], demand_days=28, include_movements=True)
        )
        assert inventory["movements"]
        assert all(m["product_id"] == "SKU-0001" for m in inventory["movements"])


def test_does_not_silently_overwrite_seed():
    with pytest.raises(ValueError):
        generate(days=2)
