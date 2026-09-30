"""首次部署时幂等初始化模拟数据，不替换已有数据集。"""

import asyncio
import json

from sqlalchemy import inspect

from app.config import get_settings
from app.db import make_engine
from app.seed import seed_database


async def main():
    settings = get_settings()
    url = settings.commerce_admin_url
    engine = make_engine(url)
    async with engine.connect() as connection:
        exists = await connection.run_sync(lambda c: inspect(c).has_table("dataset"))
        populated = False
        if exists:
            from sqlalchemy import select
            from app.commerce_models import Dataset

            populated = (await connection.scalar(select(Dataset.id).limit(1))) is not None
    await engine.dispose()
    if populated:
        print("Existing dataset retained; seed skipped.")
        return
    report = await seed_database(
        url,
        days=settings.seed_days,
        sku_count=settings.seed_skus,
        order_target=settings.seed_orders,
        scenario=settings.seed_scenario,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
