from datetime import date

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.db import Database
from app.main import create_app
from app.models import Base, User
from app.seed import seed_database


@pytest_asyncio.fixture
async def settings(tmp_path):
    commerce_path = (tmp_path / "commerce.db").as_posix()
    settings = Settings(
        _env_file=None,
        app_database_url=f"sqlite+aiosqlite:///{(tmp_path / 'app.db').as_posix()}",
        commerce_admin_url=f"sqlite+aiosqlite:///{commerce_path}",
        commerce_database_url=f"sqlite+aiosqlite:///file:{commerce_path}?mode=ro&uri=true",
        llm_model="",
        llm_provider="openai",
        llm_api_key="",
        worker_poll_seconds=0.02,
    )
    await seed_database(
        settings.commerce_admin_url,
        days=28,
        sku_count=12,
        order_target=400,
        as_of=date(2026, 9, 28),
        scenario="mixed",
    )
    return settings


@pytest_asyncio.fixture
async def database(settings):
    db = Database(settings)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with db.sessions() as session, session.begin():
        session.add(User(id="test-user", username="test-user", password_hash="unused"))
    yield db
    await db.close()


@pytest_asyncio.fixture
async def client(settings):
    app = create_app(settings, start_worker=False)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            client.test_app = app
            response = await client.post(
                "/api/auth/register", json={"username": "alice", "password": "test-password-123"}
            )
            assert response.status_code == 200
            yield client
