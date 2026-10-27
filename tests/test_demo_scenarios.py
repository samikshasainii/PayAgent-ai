import pytest
import logging
from unittest.mock import patch
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from src.main import app
from src.config import settings
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.schemas.enums import AgentStatus, TaskStatus, PayoutRequestStatus
from src.services.demo_scenarios import simulator_limiter
from src.services.gate2_reviewer import drain


@pytest.fixture(autouse=True)
def reset_limiter():
    simulator_limiter.reset()
    yield
    simulator_limiter.reset()


@pytest.mark.asyncio
async def test_switch_off_returns_404_even_with_valid_token(monkeypatch):
    """When SIMULATOR_ENABLED is False (default), endpoints return 404 before auth runs."""
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", False)
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Scenario endpoint returns 404
        res1 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
        assert res1.status_code == 404

        # Tasks endpoint returns 404
        res2 = await client.post("/api/v1/demo/tasks", json={"agent_id": "agent-code-reviewer-01"}, headers=auth_headers)
        assert res2.status_code == 404

        # Even with wrong/missing token, returns 404 (does not reveal endpoint existence)
        res3 = await client.post("/api/v1/demo/scenarios/happy")
        assert res3.status_code == 404


@pytest.mark.asyncio
async def test_switch_on_requires_operator_token(monkeypatch):
    """When SIMULATOR_ENABLED is True, endpoints enforce operator authorization (401 on missing/wrong token)."""
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing token -> 401
        res1 = await client.post("/api/v1/demo/scenarios/happy")
        assert res1.status_code == 401
        assert res1.json()["detail"]["error"] == "OPERATOR_AUTH_INVALID"

        # Wrong token -> 401
        res2 = await client.post("/api/v1/demo/scenarios/happy", headers={"Authorization": "Bearer wrong-operator-token"})
        assert res2.status_code == 401
        assert res2.json()["detail"]["error"] == "OPERATOR_AUTH_INVALID"

        # Demo tasks endpoint missing token -> 401
        res3 = await client.post("/api/v1/demo/tasks", json={"agent_id": "agent-code-reviewer-01"})
        assert res3.status_code == 401


@pytest.mark.asyncio
async def test_unknown_scenario_returns_404(monkeypatch):
    """Unrecognized scenario names return 404."""
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/v1/demo/scenarios/invalid_scenario", headers=auth_headers)
        assert res.status_code == 404
        data = res.json()
        assert data["detail"]["error"] == "UNKNOWN_SCENARIO"


@pytest.mark.asyncio
async def test_happy_scenario_success_and_replay_immunity(monkeypatch):
    """
    Happy scenario creates a fresh agent and task, passes Gate 1 as RECEIVED,
    and running it twice creates distinct salts preventing ARTIFACT_REPLAY.
    """
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", "sb-recipient@example.com")
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Run 1
        res1 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["status"] == "RECEIVED"
        assert data1["agent_id"].startswith("sim-happy-")
        assert data1["task_id"].startswith("sim-task-")
        assert data1["decision_reasons"] == []

        await drain()

        # Reset limiter for second run
        simulator_limiter.reset()

        # Run 2: Distinct salt, no ARTIFACT_REPLAY
        res2 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["status"] == "RECEIVED"
        assert data2["agent_id"] != data1["agent_id"]
        assert data2["task_id"] != data1["task_id"]

        await drain()


@pytest.mark.asyncio
async def test_rogue_scenario_blocks_at_gate1_and_never_calls_llm(monkeypatch):
    """
    Rogue scenario ($150 claim on $120 budget) is rejected at Gate 1 (OVER_TASK_BUDGET)
    and NEVER invokes the LLM.
    """
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", "sb-recipient@example.com")
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with patch("src.services.gate2_reviewer.review_proof_of_task") as mock_llm:
            res = await client.post("/api/v1/demo/scenarios/rogue", headers=auth_headers)
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "POLICY_REJECTED"
            assert "OVER_TASK_BUDGET" in data["decision_reasons"]
            assert data["agent_id"].startswith("sim-rogue-")

            # Assert LLM was never called
            mock_llm.assert_not_called()


@pytest.mark.asyncio
async def test_three_rogue_runs_leave_seeded_agents_active(monkeypatch):
    """
    Executing rogue 3 times uses isolated ephemeral sim agents, leaving
    agent-code-reviewer-01 and agent-data-labeler-02 ACTIVE (not quarantined).
    """
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", "sb-recipient@example.com")
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    # Ensure seeded agents exist in test DB
    async with async_session_factory() as db:
        for aid in ("agent-code-reviewer-01", "agent-data-labeler-02"):
            existing = (await db.execute(select(AgentModel).where(AgentModel.agent_id == aid))).scalar_one_or_none()
            if not existing:
                db.add(AgentModel(
                    agent_id=aid,
                    name=aid,
                    api_key_hash="dummy_hash",
                    status=AgentStatus.ACTIVE,
                    hourly_cap=200.00
                ))
        await db.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(3):
            simulator_limiter.reset()
            res = await client.post("/api/v1/demo/scenarios/rogue", headers=auth_headers)
            assert res.status_code == 200
            assert res.json()["status"] == "POLICY_REJECTED"

    # Verify seeded agents remained ACTIVE
    async with async_session_factory() as db:
        for aid in ("agent-code-reviewer-01", "agent-data-labeler-02"):
            agent = (await db.execute(select(AgentModel).where(AgentModel.agent_id == aid))).scalar_one()
            assert agent.status == AgentStatus.ACTIVE


@pytest.mark.asyncio
async def test_bigticket_scenario_reaches_pending_human_approval(monkeypatch):
    """
    Bigticket scenario ($120 claim > $50 auto-limit) triggers Gate 2 escalation
    to PENDING_HUMAN_APPROVAL with ABOVE_AUTO_LIMIT, which an operator can approve.
    """
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", "sb-recipient@example.com")
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/v1/demo/scenarios/bigticket", headers=auth_headers)
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "RECEIVED"
        payout_id = data["payout_request_id"]

        await drain()

        # Verify status in database moved to PENDING_HUMAN_APPROVAL
        async with async_session_factory() as db:
            payout = (await db.execute(
                select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_id)
            )).scalar_one()
            assert payout.status == PayoutRequestStatus.PENDING_HUMAN_APPROVAL
            assert "ABOVE_AUTO_LIMIT" in payout.decision_reasons

        # Operator approves the payout
        approve_res = await client.post(f"/api/v1/payout-requests/{payout_id}/approve", headers=auth_headers)
        assert approve_res.status_code == 200
        assert approve_res.json()["status"] == "PAID"


@pytest.mark.asyncio
async def test_demo_tasks_eligible_agents_and_rejections(monkeypatch):
    """
    POST /api/v1/demo/tasks creates OPEN tasks for eligible seeded agents
    and returns 422 for any other agent ID.
    """
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", "sb-recipient@example.com")
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Eligible 1: agent-code-reviewer-01
        res1 = await client.post("/api/v1/demo/tasks", json={"agent_id": "agent-code-reviewer-01"}, headers=auth_headers)
        assert res1.status_code == 200
        data1 = res1.json()
        assert data1["task_id"].startswith("demo-task-")
        assert data1["budget"] == "25.00"
        assert "deadline" in data1

        simulator_limiter.reset()

        # Eligible 2: agent-data-labeler-02
        res2 = await client.post("/api/v1/demo/tasks", json={"agent_id": "agent-data-labeler-02"}, headers=auth_headers)
        assert res2.status_code == 200
        data2 = res2.json()
        assert data2["task_id"].startswith("demo-task-")
        assert data2["budget"] == "25.00"

        simulator_limiter.reset()

        # Ineligible agent -> 422
        res3 = await client.post("/api/v1/demo/tasks", json={"agent_id": "agent-unauthorized-99"}, headers=auth_headers)
        assert res3.status_code == 422
        assert res3.json()["detail"]["error"] == "INVALID_AGENT"


@pytest.mark.asyncio
async def test_simulator_rate_limiter_cooldown_and_headers(monkeypatch):
    """
    Immediate consecutive calls trigger 429 SIMULATOR_COOLDOWN with
    Retry-After header and body matching cooldown seconds.
    """
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", "sb-recipient@example.com")
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Call 1: Success
        res1 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
        assert res1.status_code == 200

        # Call 2: Immediate -> 429 Cooldown
        res2 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
        assert res2.status_code == 429
        assert "Retry-After" in res2.headers
        data2 = res2.json()
        assert data2["error"] == "SIMULATOR_COOLDOWN"
        assert isinstance(data2["retry_after"], int)
        assert data2["retry_after"] > 0
        assert str(data2["retry_after"]) == res2.headers["Retry-After"]

        # Reset limiter -> Success again
        simulator_limiter.reset()
        res3 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
        assert res3.status_code == 200


@pytest.mark.asyncio
async def test_simulator_not_configured_returns_503(monkeypatch):
    """When SANDBOX_RECIPIENT_EMAIL is missing/blank, returns 503 SIMULATOR_NOT_CONFIGURED."""
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", None)
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res1 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
        assert res1.status_code == 503
        assert res1.json()["detail"]["error"] == "SIMULATOR_NOT_CONFIGURED"

        res2 = await client.post("/api/v1/demo/tasks", json={"agent_id": "agent-code-reviewer-01"}, headers=auth_headers)
        assert res2.status_code == 503
        assert res2.json()["detail"]["error"] == "SIMULATOR_NOT_CONFIGURED"


@pytest.mark.asyncio
async def test_no_recipient_email_or_api_key_in_responses_or_logs(monkeypatch, caplog):
    """
    Verifies that response bodies and logs across simulator endpoints
    leak neither recipient email addresses nor agent api keys.
    """
    recipient_secret = "secret-judge-recipient@sandbox.paypal.corp"
    monkeypatch.setattr(settings, "SIMULATOR_ENABLED", True)
    monkeypatch.setattr(settings, "SANDBOX_RECIPIENT_EMAIL", recipient_secret)
    auth_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        with caplog.at_level(logging.DEBUG):
            # 1. Scenario response
            res1 = await client.post("/api/v1/demo/scenarios/happy", headers=auth_headers)
            assert res1.status_code == 200
            text1 = res1.text
            assert recipient_secret not in text1
            assert "api_key" not in text1
            assert "pa_" not in text1

            simulator_limiter.reset()

            # 2. Demo task response
            res2 = await client.post("/api/v1/demo/tasks", json={"agent_id": "agent-code-reviewer-01"}, headers=auth_headers)
            assert res2.status_code == 200
            text2 = res2.text
            assert recipient_secret not in text2
            assert "api_key" not in text2
            assert "pa_" not in text2

        # 3. Captured application logs (excluding low-level DB driver parameter tracing)
        app_logs = "\n".join(
            rec.getMessage() for rec in caplog.records
            if not rec.name.startswith(("aiosqlite", "sqlalchemy"))
        )
        assert recipient_secret not in app_logs
        assert "api_key_hash" not in app_logs
