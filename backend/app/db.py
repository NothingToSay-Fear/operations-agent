"""应用数据库与只读业务数据库的异步连接管理。"""

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings


def make_engine(url: str, *, readonly: bool = False):
    if url.startswith("sqlite") and not readonly:
        filename = make_url(url).database
        if filename and filename != ":memory:":
            Path(filename.removeprefix("file:")).resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, pool_pre_ping=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def setup(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            if readonly:
                cursor.execute("PRAGMA query_only=ON")
            cursor.close()
    elif readonly:

        @event.listens_for(engine.sync_engine, "connect")
        def setup_postgres(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("SET default_transaction_read_only = on")
            cursor.execute("SET statement_timeout = '15000ms'")
            cursor.close()

    return engine


class Database:
    def __init__(self, settings: Settings):
        self.engine = make_engine(settings.app_database_url)
        self.commerce = make_engine(settings.commerce_database_url, readonly=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.read_sessions = async_sessionmaker(self.commerce, expire_on_commit=False)

    async def close(self):
        await self.engine.dispose()
        await self.commerce.dispose()
