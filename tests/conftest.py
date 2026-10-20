import os
import asyncio
import pytest
import pytest_asyncio

# Configure environment strictly for test execution before importing app modules
TEST_DB_FILE = "./data/test_payagent.db"
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{TEST_DB_FILE}"
os.environ["MOCK_PAYPAL"] = "true"
os.environ["OPERATOR_TOKEN"] = "test-operator-secret-token"

from src.database import engine, Base, async_session_factory
from src.config import settings
from src.services.paypal_client import paypal_client


@pytest.fixture(autouse=True)
async def isolated_test_db():
    """Ensure clean tables in isolated test database for every test."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

