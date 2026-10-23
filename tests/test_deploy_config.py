import re
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from unittest.mock import patch

from src.main import app
from src.seed import mask_recipient_email
from src.services.status_mapping import PAYPAL_EVENT_TO_REQUEST_STATUS

@pytest.mark.asyncio
async def test_healthz_endpoint_success_no_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/healthz")
        assert res.status_code == 200
        data = res.json()
        assert set(data.keys()) == {"status"}
        assert data["status"] == "ok"

@pytest.mark.asyncio
async def test_healthz_endpoint_db_failure_returns_503():
    transport = ASGITransport(app=app)
    # Monkeypatch async_session_factory to raise on execute
    class FailingSession:
        async def __aenter__(self):
            return self
        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass
        async def execute(self, stmt):
            raise RuntimeError("Database connection pool timeout")

    with patch("src.main.async_session_factory", return_value=FailingSession()):
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/healthz")
            assert res.status_code == 503
            data = res.json()
            assert set(data.keys()) == {"status"}
            assert data["status"] == "db_unavailable"
            # Must not leak internal exception details
            assert "timeout" not in res.text
            assert "RuntimeError" not in res.text

def test_mask_recipient_email():
    assert mask_recipient_email("sb-contractor@personal.example.com") == "sb***@personal.example.com"
    assert mask_recipient_email("ab@domain.com") == "ab***@domain.com"
    assert mask_recipient_email("a@domain.com") == "a***@domain.com"
    assert mask_recipient_email("invalid-email") == "***"

def test_deployment_doc_webhook_events_match_status_mapping():
    with open("docs/DEPLOYMENT.md", "r", encoding="utf-8") as f:
        content = f.read()

    # Extract all PAYMENT.* events listed in DEPLOYMENT.md
    doc_events = set(re.findall(r"`(PAYMENT\.[A-Z0-9.\-_]+)`", content))
    assert len(doc_events) >= 11, f"Expected at least 11 webhook events in DEPLOYMENT.md, found {len(doc_events)}"

    # Optional events allowed
    optional_events = {"PAYMENT.PAYOUTSBATCH.PROCESSING", "PAYMENT.PAYOUTSBATCH.SUCCESS"}

    # Every item event in doc must be in status_mapping
    for event in doc_events:
        if event in optional_events:
            continue
        assert event in PAYPAL_EVENT_TO_REQUEST_STATUS, f"Event {event} in DEPLOYMENT.md not in status_mapping"

def test_supabase_session_pooler_asyncpg_url_parsing():
    # Verify Supabase session pooler string with ?ssl=require parses cleanly
    supabase_url = (
        "postgresql+asyncpg://postgres.myproject:secretpassword@"
        "aws-0-us-east-1.pooler.supabase.com:5432/postgres?ssl=require"
    )
    u = make_url(supabase_url)
    assert u.drivername == "postgresql+asyncpg"
    assert u.port == 5432
    assert u.query.get("ssl") == "require"
    # Ensure create_async_engine accepts it without crashing
    engine = create_async_engine(supabase_url)
    assert engine is not None
