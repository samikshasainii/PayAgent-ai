import os
import asyncio
import pytest
import pytest_asyncio

# Configure environment strictly for test execution before importing app modules
TEST_DB_FILE = "./data/test_payagent.db"
os.environ["APP_ENV"] = "test"
# TEST_DATABASE_URL points the suite at a disposable Postgres database (tables are dropped before every test)
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{TEST_DB_FILE}"
os.environ["MOCK_PAYPAL"] = "true"
os.environ["OPERATOR_TOKEN"] = "test-operator-secret-token"

def pytest_report_header(config):
    url = os.environ.get("DATABASE_URL", "")
    dialect = "postgresql" if "postgres" in url else "sqlite"
    return f"test database: {dialect}"

from src.database import engine, Base, async_session_factory
from src.config import settings
from src.services.paypal_client import paypal_client
from src.services import gate2_reviewer


@pytest.fixture(autouse=True)
async def isolated_test_db():
    """Ensure clean tables in isolated test database for every test."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Never let a background Gate 2 review outlive its test (the next test drops the tables)
    await gate2_reviewer.drain()
    await engine.dispose()


async def settle(client, submit_resp, headers):
    """
    Async Gate 2: asserts the submission was accepted (202 RECEIVED), waits for the background
    review (and dispatch) to finish, and returns the request's resulting JSON.
    """
    assert submit_resp.status_code == 202, submit_resp.text
    assert submit_resp.json()["status"] == "RECEIVED"
    await gate2_reviewer.drain()
    resp = await client.get(f"/api/v1/payout-requests/{submit_resp.json()['payout_request_id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01):
    """Polls predicate until it returns truthy or timeout expires. Returns elapsed seconds if result is True, else the truthy result."""
    loop = asyncio.get_running_loop()
    start = loop.time()
    while True:
        res = predicate()
        if asyncio.iscoroutine(res):
            res = await res
        if res:
            if res is True:
                return loop.time() - start
            return res
        if loop.time() - start > timeout:
            raise TimeoutError(f"wait_until timed out after {timeout}s")
        await asyncio.sleep(interval)
