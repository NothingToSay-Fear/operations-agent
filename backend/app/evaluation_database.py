"""评测专用 PostgreSQL 数据库的创建、授权与重置。"""

import os

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.commerce_models import CommerceBase
from app.db import make_engine
from app.models import Base


def require_postgres_url(name: str) -> str:
    # 评测明确拒绝 SQLite，避免检索索引和 SQL 行为与正式 PostgreSQL 环境不一致。
    value = os.environ.get(name, "").strip()
    if not value.startswith("postgresql+"):
        raise RuntimeError(f"{name} 必须配置为独立的 PostgreSQL 数据库地址")
    return value


def evaluation_urls(prefix: str, settings) -> tuple[str, str, str]:
    app_url = require_postgres_url(f"{prefix}_APP_DATABASE_URL")
    commerce_admin_url = require_postgres_url(f"{prefix}_COMMERCE_ADMIN_URL")
    commerce_reader_url = require_postgres_url(f"{prefix}_COMMERCE_DATABASE_URL")
    production_urls = {
        settings.app_database_url,
        settings.commerce_database_url,
        getattr(settings, "commerce_admin_url", ""),
    }
    if {app_url, commerce_admin_url, commerce_reader_url} & production_urls:
        raise RuntimeError("评测数据库地址不能指向正在运行的应用或业务数据库")
    return app_url, commerce_admin_url, commerce_reader_url


async def reset_application_database(url: str, *, retrieval_indexes: bool = False):
    # 评测库独立重置；调用方只能传入专用 RAG 或 Agent 评测连接串。
    engine = make_engine(url)
    try:
        async with engine.begin() as connection:
            extension = await connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            if extension != 1:
                raise RuntimeError("测试数据库未预装 vector 扩展")
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
            if retrieval_indexes:
                for table in ("knowledge_segments", "history_units"):
                    await connection.execute(
                        text(
                            f"CREATE INDEX IF NOT EXISTS ix_{table}_terms "
                            f"ON {table} USING gin (to_tsvector('simple', search_terms))"
                        )
                    )
                    await connection.execute(
                        text(
                            f"CREATE INDEX IF NOT EXISTS ix_{table}_vector "
                            f"ON {table} USING hnsw (embedding vector_cosine_ops)"
                        )
                    )
    finally:
        await engine.dispose()


async def reset_commerce_database(url: str):
    engine = make_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(CommerceBase.metadata.drop_all)
    finally:
        await engine.dispose()


async def grant_commerce_read_access(admin_url: str, reader_url: str):
    username = make_url(reader_url).username
    if not username:
        raise RuntimeError("测试经营只读数据库地址缺少账号")
    role = username.replace('"', '""')
    engine = make_engine(admin_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
            await connection.execute(text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"'))
    finally:
        await engine.dispose()
