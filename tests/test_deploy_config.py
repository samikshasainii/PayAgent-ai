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

    # Parse ONLY the subscription list: lines matching r"^\s*\*\s+`(PAYMENT\.[A-Z0-9.\-_]+)`"
    doc_events = set(re.findall(r"^\s*\*\s+`(PAYMENT\.[A-Z0-9.\-_]+)`", content, flags=re.MULTILINE))
    assert len(doc_events) >= 11, f"Expected at least 11 webhook events in DEPLOYMENT.md, found {len(doc_events)}"

    # Optional events allowed
    optional_events = {"PAYMENT.PAYOUTSBATCH.PROCESSING", "PAYMENT.PAYOUTSBATCH.SUCCESS"}

    # Bidirectional check:
    # 1. Every event in status_mapping must be documented in DEPLOYMENT.md
    assert set(PAYPAL_EVENT_TO_REQUEST_STATUS) <= doc_events, (
        f"Missing events in DEPLOYMENT.md: {set(PAYPAL_EVENT_TO_REQUEST_STATUS) - doc_events}"
    )

    # 2. Every non-optional event in doc must be in status_mapping
    for event in doc_events:
        if event in optional_events:
            continue
        assert event in PAYPAL_EVENT_TO_REQUEST_STATUS, f"Event {event} in DEPLOYMENT.md not in status_mapping"

@pytest.mark.asyncio
async def test_prod_seed_masks_recipient_in_stdout(monkeypatch, capsys):
    from src.seed import seed_database
    from src.config import settings

    raw_email = "sb-contractor-secret@personal.example.com"
    masked_email = "sb***@personal.example.com"

    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "prod-secure-token-32-chars-minimum-xyz")
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", raw_email)

    await seed_database(reset=False)
    captured = capsys.readouterr().out

    assert raw_email not in captured, "Raw recipient email leaked in production seed output"
    assert masked_email in captured, "Masked recipient email not found in production seed output"

import os
import sys
import subprocess
from fastapi import HTTPException
import scripts.seed_hosted as seed_hosted
from src.database import async_session_factory
from src.services.auth import get_authenticated_agent

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

VALID_HOSTED_ENV = {
    "DATABASE_URL": "postgresql+asyncpg://postgres.ref:secretpass123@aws-0-us-east-1.pooler.supabase.com:5432/postgres?ssl=require",
    "APP_ENV": "production",
    "OPERATOR_TOKEN": "secretoperator12345678901234567890123",
    "SANDBOX_RECIPIENT_EMAIL": "sb-contractor-live@personal.example.com",
}

def _apply_valid_seed_hosted_preconditions(monkeypatch, env_overrides=None):
    """Sets all preconditions to valid so refusal tests isolate strictly the condition under test."""
    monkeypatch.setattr(seed_hosted, "__package__", "scripts")
    monkeypatch.setattr(sys, "argv", ["scripts.seed_hosted"])
    monkeypatch.setattr(os.path, "isfile", lambda p: True if p == ".env.hosted" else os.path.exists(p))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: type("Obj", (), {"returncode": 0})())

    env = VALID_HOSTED_ENV.copy()
    if env_overrides:
        env.update(env_overrides)
    monkeypatch.setattr("scripts.seed_hosted.dotenv_values", lambda f: env)
    return env

def test_seed_hosted_refuses_when_not_package(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(monkeypatch)
    monkeypatch.setattr(seed_hosted, "__package__", "")
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "run as: python -m scripts.seed_hosted" in captured.err

def test_seed_hosted_refuses_flags(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["scripts.seed_hosted", "--reset"])
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "scripts/seed_hosted.py does not accept command-line flags or arguments" in captured.err

def test_seed_hosted_refuses_when_env_not_gitignored(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(monkeypatch)
    # Only git check-ignore returns non-zero
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: type("Obj", (), {"returncode": 1})())
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert ".env.hosted is not gitignored" in captured.err

def test_seed_hosted_refuses_missing_env_file(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(monkeypatch)
    # Only isfile returns False
    monkeypatch.setattr(os.path, "isfile", lambda p: False)
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert ".env.hosted file not found" in captured.err

def test_seed_hosted_refuses_sqlite_url(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(monkeypatch, {"DATABASE_URL": "sqlite+aiosqlite:///test.db"})
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "DATABASE_URL must start with 'postgresql+asyncpg://' and contain '.pooler.supabase.com'" in captured.err

def test_seed_hosted_refuses_non_pooler_host(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(
        monkeypatch, {"DATABASE_URL": "postgresql+asyncpg://user:pass@ep-db.neon.tech/postgres"}
    )
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "DATABASE_URL must start with 'postgresql+asyncpg://' and contain '.pooler.supabase.com'" in captured.err

def test_seed_hosted_refuses_app_env_not_production(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(monkeypatch, {"APP_ENV": "development"})
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "APP_ENV must be set to 'production'" in captured.err

def test_seed_hosted_refuses_missing_or_dummy_recipient(monkeypatch, capsys):
    # Dummy fallback address refused
    _apply_valid_seed_hosted_preconditions(
        monkeypatch, {"SANDBOX_RECIPIENT_EMAIL": "sb-personal-contractor@personal.example.com"}
    )
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "SANDBOX_RECIPIENT_EMAIL is missing from .env.hosted or set to fallback dummy address" in captured.err

    # Missing recipient refused
    _apply_valid_seed_hosted_preconditions(monkeypatch, {"SANDBOX_RECIPIENT_EMAIL": ""})
    with patch.dict(os.environ, clear=False):
        with pytest.raises(SystemExit) as exc:
            seed_hosted.main()
        assert exc.value.code == 2
    captured2 = capsys.readouterr()
    assert "SANDBOX_RECIPIENT_EMAIL is missing from .env.hosted or set to fallback dummy address" in captured2.err

def test_seed_hosted_success_masks_secrets_and_calls_seed(monkeypatch, capsys):
    _apply_valid_seed_hosted_preconditions(monkeypatch)
    called = {}

    async def mock_seed(reset=False, recreate_schema=False, yes_drop_everything=False, keys_output_path=None):
        called["reset"] = reset
        called["keys_output_path"] = keys_output_path

    monkeypatch.setattr("src.seed.seed_database", mock_seed)
    with patch.dict(os.environ, clear=False):
        seed_hosted.main()

    captured = capsys.readouterr()
    assert "Connecting to hosted Supabase database host: aws-0-us-east-1.pooler.supabase.com" in captured.out
    assert "secretpass123" not in captured.out
    assert "secretpass123" not in captured.err
    assert "secretoperator" not in captured.out
    assert "secretoperator" not in captured.err
    assert "postgresql+asyncpg" not in captured.out
    assert called == {"reset": False, "keys_output_path": ".env.hosted.agents"}

def test_seed_hosted_does_not_pollute_outer_environ(monkeypatch):
    orig_db = os.environ.get("DATABASE_URL")
    orig_env = os.environ.get("APP_ENV")

    _apply_valid_seed_hosted_preconditions(monkeypatch)

    async def mock_seed(reset=False, recreate_schema=False, yes_drop_everything=False, keys_output_path=None):
        pass

    monkeypatch.setattr("src.seed.seed_database", mock_seed)
    with patch.dict(os.environ, clear=False):
        seed_hosted.main()

    assert os.environ.get("DATABASE_URL") == orig_db, "DATABASE_URL leaked outside test scope"
    assert os.environ.get("APP_ENV") == orig_env, "APP_ENV leaked outside test scope"

@pytest.mark.asyncio
async def test_prod_seed_rerun_leaves_paid_task_paid(monkeypatch, capsys):
    from src.seed import seed_database
    from src.config import settings
    from src.models.task import TaskModel
    from src.schemas.enums import TaskStatus
    from sqlalchemy import select

    monkeypatch.setattr(settings, "APP_ENV", "production")
    monkeypatch.setattr(settings, "OPERATOR_TOKEN", "prod-secure-token-32-chars-minimum-xyz")
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", "sb-custom@personal.example.com")

    # Initial seed
    await seed_database(reset=False)

    # Transition task to PAID
    async with async_session_factory() as db:
        stmt = select(TaskModel).where(TaskModel.task_id == "task-pr-audit-101")
        task = (await db.execute(stmt)).scalar_one()
        task.status = TaskStatus.PAID
        await db.commit()

    # Re-run seed in production
    await seed_database(reset=False)
    captured = capsys.readouterr().out

    # Assert status was NOT reverted to OPEN
    async with async_session_factory() as db:
        stmt = select(TaskModel).where(TaskModel.task_id == "task-pr-audit-101")
        task_after = (await db.execute(stmt)).scalar_one()
        assert task_after.status == TaskStatus.PAID, "Production re-run unexpectedly reverted PAID task to OPEN"

    # Assert specific log message
    assert "[.] Task already present: task-pr-audit-101 (left as PAID)" in captured

@pytest.mark.asyncio
async def test_agent_keys_file_lifecycle_and_rotation(monkeypatch, capsys):
    from src.seed import seed_database
    from src.config import settings

    # 1. Refusal when path isn't gitignored
    with pytest.raises(RuntimeError, match="is not gitignored"):
        await seed_database(reset=False, keys_output_path="unignored_secret_file.txt")

    # 2. File creation and authentication with gitignored path
    test_key_file = ".env.hosted.test_agents"
    if os.path.exists(test_key_file):
        os.remove(test_key_file)

    try:
        monkeypatch.setattr(settings, "APP_ENV", "production")
        monkeypatch.setattr(settings, "OPERATOR_TOKEN", "prod-secure-token-32-chars-minimum-xyz")

        await seed_database(reset=False, keys_output_path=test_key_file)
        assert os.path.isfile(test_key_file), "Keys file was not created"

        captured = capsys.readouterr().out
        with open(test_key_file, "r", encoding="utf-8") as f:
            content = f.read()

        # Parse written keys
        keys = {}
        for line in content.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                keys[k.strip()] = v.strip()

        key_reviewer_v1 = keys.get("AGENT_CODE_REVIEWER_01_KEY")
        key_labeler_v1 = keys.get("AGENT_DATA_LABELER_02_KEY")
        assert key_reviewer_v1 and key_labeler_v1

        # Never appears in stdout
        assert key_reviewer_v1 not in captured
        assert key_labeler_v1 not in captured

        # Authenticate with written key
        async with async_session_factory() as db:
            agent = await get_authenticated_agent(authorization=f"Bearer {key_reviewer_v1}", db=db)
            assert agent.agent_id == "agent-code-reviewer-01"

        # 3. Re-run rotates keys
        await seed_database(reset=False, keys_output_path=test_key_file)
        captured_rerun = capsys.readouterr().out
        assert "[.] Agent key rotated: agent-code-reviewer-01" in captured_rerun

        with open(test_key_file, "r", encoding="utf-8") as f:
            content_v2 = f.read()

        keys_v2 = {}
        for line in content_v2.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                keys_v2[k.strip()] = v.strip()

        key_reviewer_v2 = keys_v2.get("AGENT_CODE_REVIEWER_01_KEY")
        assert key_reviewer_v2 != key_reviewer_v1
        assert key_reviewer_v2 not in captured_rerun

        # OLD key gets 401
        async with async_session_factory() as db:
            with pytest.raises(HTTPException) as exc_info:
                await get_authenticated_agent(authorization=f"Bearer {key_reviewer_v1}", db=db)
            assert exc_info.value.status_code == 401

            # NEW key authenticates
            agent_v2 = await get_authenticated_agent(authorization=f"Bearer {key_reviewer_v2}", db=db)
            assert agent_v2.agent_id == "agent-code-reviewer-01"

    finally:
        if os.path.exists(test_key_file):
            os.remove(test_key_file)

@pytest.mark.asyncio
async def test_lifespan_poller_enabled_switch(monkeypatch, caplog):
    import logging
    from contextlib import asynccontextmanager
    from src.main import lifespan, app, mcp_server
    from src.services.poller import poller
    from src.config import settings

    @asynccontextmanager
    async def dummy_mcp_run():
        yield

    monkeypatch.setattr(mcp_server.session_manager, "run", dummy_mcp_run)

    # Ensure clean starting state
    await poller.stop()

    # 1. POLLER_ENABLED=False: poller does not start and warning is logged
    monkeypatch.setattr(settings, "POLLER_ENABLED", False)
    with caplog.at_level(logging.WARNING):
        async with lifespan(app):
            assert poller._running is False
            assert "Reconciliation poller DISABLED (POLLER_ENABLED=false); webhooks are the only settlement path" in caplog.text

    assert poller._running is False

    # 2. POLLER_ENABLED=True: poller starts
    monkeypatch.setattr(settings, "POLLER_ENABLED", True)
    async with lifespan(app):
        assert poller._running is True

    # Shutdown stops the poller
    assert poller._running is False


def test_simulator_enabled_default_off():
    """M3.8 8a: SIMULATOR_ENABLED must default to False so backend changes are inert when deployed to Render."""
    from src.config import Settings, settings
    assert settings.SIMULATOR_ENABLED is False
    fresh_settings = Settings()
    assert fresh_settings.SIMULATOR_ENABLED is False

