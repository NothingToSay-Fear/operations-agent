from datetime import date

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.db import Database
from app.evaluation_database import (
    grant_commerce_read_access,
    require_postgres_url,
    reset_application_database,
    reset_commerce_database,
)
from app.main import create_app
from app.models import User
from app.seed import seed_database


@pytest_asyncio.fixture
async def settings():
    app_url = require_postgres_url("TEST_APP_DATABASE_URL")
    commerce_admin_url = require_postgres_url("TEST_COMMERCE_ADMIN_URL")
    commerce_reader_url = require_postgres_url("TEST_COMMERCE_DATABASE_URL")
    await reset_application_database(app_url)
    await reset_commerce_database(commerce_admin_url)
    settings = Settings(
        _env_file=None,
        app_database_url=app_url,
        commerce_admin_url=commerce_admin_url,
        commerce_database_url=commerce_reader_url,
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
    await grant_commerce_read_access(commerce_admin_url, commerce_reader_url)
    try:
        yield settings
    finally:
        await reset_application_database(app_url)
        await reset_commerce_database(commerce_admin_url)


@pytest_asyncio.fixture
async def database(settings):
    db = Database(settings)
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
