import json
import time
from datetime import datetime, timedelta, timezone

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
from decimal import Decimal
import pytest
from httpx import AsyncClient, ASGITransport

from src.main import app
from src.config import settings
from src.database import async_session_factory, init_db, engine, Base
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import TaskStatus, PayoutRequestStatus, AgentStatus, DecisionReason
from src.schemas.task import TaskCreate, validate_currency_precision
from src.schemas.payout import PayoutRequestIn
from src.services.auth import hash_api_key
from src.services.guardian import compute_artifact_sha256, get_current_hour_window
from src.services.state_machine import transition_payout_request_status, cancel_unclaimed_payout_request
from src.services.paypal_client import paypal_client
from src.mcp_server.server import list_my_tasks, submit_task_payout, get_payout_status

@pytest.fixture(autouse=True)
async def setup_test_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

# --- Test 1: Currency Precision & Validation ---
def test_currency_precision_strict():
    # Valid amounts
    assert validate_currency_precision(Decimal("50.00")) == Decimal("50.00")
    assert validate_currency_precision(Decimal("0.50")) == Decimal("0.50")
    assert validate_currency_precision(Decimal("15")) == Decimal("15")

    # Invalid: > 2 decimal places (must be rejected, never rounded)
    with pytest.raises(ValueError, match="more than 2 decimal places"):
        validate_currency_precision(Decimal("10.505"))

    with pytest.raises(ValueError, match="more than 2 decimal places"):
        validate_currency_precision(Decimal("0.1234"))

    # Invalid: <= 0
    with pytest.raises(ValueError, match="strictly greater than 0"):
        validate_currency_precision(Decimal("0.00"))

    with pytest.raises(ValueError, match="strictly greater than 0"):
        validate_currency_precision(Decimal("-10.00"))

# --- Test 2: Dynamic OAuth2 Token TTL Refresh ---
@pytest.mark.asyncio
async def test_dynamic_token_ttl_refresh():
    paypal_client._access_token = None
    paypal_client._token_expires_at = 0.0

    # First fetch
    token1 = await paypal_client.get_access_token()
    assert token1 is not None
    assert paypal_client._token_expires_at > time.time()

    # Cached fetch (should return same token without recalculating)
    token2 = await paypal_client.get_access_token()
    assert token1 == token2

    # Advance time near expiration (within 60s buffer)
    paypal_client._token_expires_at = time.time() + 30
    token3 = await paypal_client.get_access_token()
    assert token3 is not None
    assert paypal_client._token_expires_at > time.time() + 1000

# --- Test 3: Saved-ID Pattern & Idempotency Key Handling ---
@pytest.mark.asyncio
async def test_saved_id_pattern_and_replay_rejection():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        op_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}
        
        # 1. Register Agent
        agent_id = f"agent-test-{int(time.time() * 1000)}"
        agent_resp = await client.post(
            "/api/v1/agents",
            json={"agent_id": agent_id, "name": "Test Worker Agent", "hourly_cap": "200.00"},
            headers=op_headers
        )
        assert agent_resp.status_code == 201
        agent_data = agent_resp.json()
        agent_key = agent_data["api_key"]
        agent_headers = {"Authorization": f"Bearer {agent_key}"}

        # 2. Create Task
        task_id = f"task-test-{int(time.time() * 1000)}"
        task_resp = await client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_id,
                "title": "Data Labeling Batch 1",
                "acceptance_criteria": "Verify 50 entries labeled",
                "budget": "40.00",
                "allowed_recipient": "contractor@sandbox.paypal.com",
                "assigned_agent_id": agent_id,
                "required_deliverables": ["data_report"],
                "deadline": (utc_now() + timedelta(days=2)).isoformat()
            },
            headers=op_headers
        )
        assert task_resp.status_code == 201

        # 3. Submit Payout Request (Auto-approved)
        payout_resp = await client.post(
            "/api/v1/payout-requests",
            json={
                "task_id": task_id,
                "amount": "40.00",
                "deliverables": {"data_report": "https://example.com/data_report.json"}
            },
            headers=agent_headers
        )
        assert payout_resp.status_code == 201
        payout_data = payout_resp.json()
        assert payout_data["status"] == "PAID"
        assert payout_data["sender_batch_id"].startswith("payagent_")
        assert payout_data["paypal_request_id"] == payout_data["payout_request_id"]

        # 4. Attempt Replay with EXACT SAME artifact (Deliverables)
        # Row 7 Canonical Decision Table: ARTIFACT_REPLAY -> 409 Conflict
        # Create second task for replay test
        task_id_2 = f"task-replay-{int(time.time() * 1000)}"
        await client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_id_2,
                "title": "Data Labeling Batch 2",
                "acceptance_criteria": "Verify 50 entries",
                "budget": "40.00",
                "allowed_recipient": "contractor@sandbox.paypal.com",
                "assigned_agent_id": agent_id,
                "required_deliverables": ["data_report"],
                "deadline": (utc_now() + timedelta(days=2)).isoformat()
            },
            headers=op_headers
        )

        replay_resp = await client.post(
            "/api/v1/payout-requests",
            json={
                "task_id": task_id_2,
                "amount": "40.00",
                "deliverables": {"data_report": "https://example.com/data_report.json"}
            },
            headers=agent_headers
        )
        assert replay_resp.status_code == 409
        err = replay_resp.json()["detail"]
        assert err["error"] == "ARTIFACT_REPLAY"
        assert err["status"] == "POLICY_REJECTED"

# --- Test 4: Decision Table Rules & Boundaries ---
@pytest.mark.asyncio
async def test_decision_table_boundaries():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        op_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}
        
        agent_id = f"agent-dt-{int(time.time() * 1000)}"
        agent_resp = await client.post(
            "/api/v1/agents",
            json={"agent_id": agent_id, "name": "Boundary Test Agent", "hourly_cap": "500.00"},
            headers=op_headers
        )
        agent_headers = {"Authorization": f"Bearer {agent_resp.json()['api_key']}"}

        # Case A: Amount == 50.00 (Exactly $50.00 auto-approves)
        task_50 = f"task-50-{int(time.time() * 1000)}"
        await client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_50,
                "title": "Exact 50 Task",
                "acceptance_criteria": "Done",
                "budget": "50.00",
                "allowed_recipient": "contractor@sandbox.paypal.com",
                "assigned_agent_id": agent_id,
                "required_deliverables": ["output"],
                "deadline": (utc_now() + timedelta(days=1)).isoformat()
            },
            headers=op_headers
        )

        resp_50 = await client.post(
            "/api/v1/payout-requests",
            json={"task_id": task_50, "amount": "50.00", "deliverables": {"output": "result-50"}},
            headers=agent_headers
        )
        assert resp_50.status_code == 201
        assert resp_50.json()["status"] == "PAID"
        assert "AUTO_APPROVED" in resp_50.json()["decision_reasons"]

        # Case B: Amount == 50.01 (Strictly greater than $50.00 -> PENDING_HUMAN_APPROVAL)
        task_51 = f"task-51-{int(time.time() * 1000)}"
        await client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_51,
                "title": "50.01 Task",
                "acceptance_criteria": "Done",
                "budget": "60.00",
                "allowed_recipient": "contractor@sandbox.paypal.com",
                "assigned_agent_id": agent_id,
                "required_deliverables": ["output"],
                "deadline": (utc_now() + timedelta(days=1)).isoformat()
            },
            headers=op_headers
        )

        resp_51 = await client.post(
            "/api/v1/payout-requests",
            json={"task_id": task_51, "amount": "50.01", "deliverables": {"output": "result-51"}},
            headers=agent_headers
        )
        assert resp_51.status_code == 201
        assert resp_51.json()["status"] == "PENDING_HUMAN_APPROVAL"
        assert "ABOVE_AUTO_LIMIT" in resp_51.json()["decision_reasons"]

        # Case C: Operator Approves the escalated request
        req_id = resp_51.json()["payout_request_id"]
        approve_resp = await client.post(
            f"/api/v1/payout-requests/{req_id}/approve",
            headers=op_headers
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "PAID"

# --- Test 5: Budget Releases on CANCELED and RETURNED ---
@pytest.mark.asyncio
async def test_budget_reservation_and_releases():
    async with async_session_factory() as db:
        agent_id = f"agent-budget-{int(time.time() * 1000)}"
        agent = AgentModel(
            agent_id=agent_id,
            name="Budget Agent",
            api_key_hash=hash_api_key("test"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("100.00")
        )
        db.add(agent)
        await db.commit()

        hour_window = get_current_hour_window()
        window = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_window,
            reserved=Decimal("30.00"),
            spent=Decimal("0.00")
        )
        db.add(window)

        task = TaskModel(
            task_id=f"t-budget-{int(time.time())}",
            title="Budget Task",
            acceptance_criteria="Check budget release",
            budget=Decimal("30.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=1),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        db.add(task)

        req = PayoutRequestModel(
            payout_request_id=f"req-cancel-{int(time.time())}",
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("30.00"),
            deliverables={"doc": "http://doc"},
            artifact_sha256="dummy_sha256_cancel",
            status=PayoutRequestStatus.UNCLAIMED,
            decision_reasons=[],
            sender_batch_id=f"batch_{int(time.time())}",
            paypal_request_id=f"pp_req_{int(time.time())}",
            created_at=utc_now()
        )
        db.add(req)
        await db.commit()

        # Execute cancellation on UNCLAIMED
        await cancel_unclaimed_payout_request(db, req.payout_request_id)
        await db.commit()

        # Verify budget was released (reserved back to 0.00)
        await db.refresh(window)
        assert window.reserved == Decimal("0.00")
        assert window.spent == Decimal("0.00")

        # Verify task returned to OPEN
        await db.refresh(task)
        assert task.status == TaskStatus.OPEN

        # Test RETURNED transition releases budget too
        window.reserved = Decimal("25.00")
        req2 = PayoutRequestModel(
            payout_request_id=f"req-return-{int(time.time())}",
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("25.00"),
            deliverables={"doc": "http://doc2"},
            artifact_sha256="dummy_sha256_return",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=[],
            sender_batch_id=f"batch_ret_{int(time.time())}",
            paypal_request_id=f"pp_ret_{int(time.time())}",
            created_at=utc_now()
        )
        db.add(req2)
        await db.commit()

        await transition_payout_request_status(db, req2, PayoutRequestStatus.RETURNED)
        await db.commit()
        await db.refresh(window)
        assert window.reserved == Decimal("0.00")

# --- Test 6: Strict HTTP 409 Conflict on Non-UNCLAIMED Cancellation ---
@pytest.mark.asyncio
async def test_cancellation_requires_unclaimed_status():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        op_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}
        
        agent_id = f"agent-cancel-{int(time.time() * 1000)}"
        agent_resp = await client.post(
            "/api/v1/agents",
            json={"agent_id": agent_id, "name": "Cancel Test Agent", "hourly_cap": "100.00"},
            headers=op_headers
        )
        agent_headers = {"Authorization": f"Bearer {agent_resp.json()['api_key']}"}

        task_id = f"task-cancel-{int(time.time() * 1000)}"
        await client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_id,
                "title": "Cancel Test Task",
                "acceptance_criteria": "Done",
                "budget": "20.00",
                "allowed_recipient": "contractor@sandbox.paypal.com",
                "assigned_agent_id": agent_id,
                "required_deliverables": ["output"],
                "deadline": (utc_now() + timedelta(days=1)).isoformat()
            },
            headers=op_headers
        )

        payout_resp = await client.post(
            "/api/v1/payout-requests",
            json={"task_id": task_id, "amount": "20.00", "deliverables": {"output": "result"}},
            headers=agent_headers
        )
        req_id = payout_resp.json()["payout_request_id"]
        assert payout_resp.json()["status"] == "PAID"

        # Attempting to cancel a PAID request MUST raise HTTP 409 Conflict
        cancel_resp = await client.post(
            f"/api/v1/payout-requests/{req_id}/cancel",
            headers=op_headers
        )
        assert cancel_resp.status_code == 409
        err = cancel_resp.json()["detail"]
        assert err["error"] == "INVALID_STATE_FOR_CANCELLATION"

# --- Test 7: Streamable HTTP MCP Tools Execution ---
@pytest.mark.asyncio
async def test_mcp_guardian_tools():
    # 1. Register agent & task directly
    async with async_session_factory() as db:
        agent_id = f"agent-mcp-{int(time.time() * 1000)}"
        raw_key = f"key-{int(time.time() * 1000)}"
        agent = AgentModel(
            agent_id=agent_id,
            name="MCP Worker",
            api_key_hash=hash_api_key(raw_key),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("150.00")
        )
        db.add(agent)

        task_id = f"task-mcp-{int(time.time() * 1000)}"
        task = TaskModel(
            task_id=task_id,
            title="MCP Task",
            acceptance_criteria="Verify MCP tool calls",
            budget=Decimal("25.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["artifact"],
            deadline=utc_now() + timedelta(days=1),
            status=TaskStatus.OPEN
        )
        db.add(task)
        await db.commit()

    # 2. Test list_my_tasks tool
    tasks_json = await list_my_tasks(agent_id=agent_id, api_key=raw_key)
    tasks_res = json.loads(tasks_json)
    assert "tasks" in tasks_res
    assert len(tasks_res["tasks"]) == 1
    assert tasks_res["tasks"][0]["task_id"] == task_id

    # 3. Test submit_task_payout tool
    submit_json = await submit_task_payout(
        agent_id=agent_id,
        api_key=raw_key,
        task_id=task_id,
        amount="25.00",
        deliverables={"artifact": "https://github.com/org/repo/pull/1"}
    )
    submit_res = json.loads(submit_json)
    assert submit_res["status"] == "PAID"
    assert "payout_request_id" in submit_res
    payout_req_id = submit_res["payout_request_id"]

    # 4. Test get_payout_status tool
    status_json = await get_payout_status(payout_request_id=payout_req_id)
    status_res = json.loads(status_json)
    assert status_res["status"] == "PAID"
    assert status_res["amount"] == "25.00"

# --- Test 8: Gate 1 Rejections & Circuit Breaker ---
@pytest.mark.asyncio
async def test_gate1_rejections_and_circuit_breaker():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        op_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

        # 1. Register agent with hourly cap of $50
        agent_id = f"agent-cb-{int(time.time() * 1000)}"
        agent_resp = await client.post(
            "/api/v1/agents",
            json={"agent_id": agent_id, "name": "Circuit Breaker Agent", "hourly_cap": "50.00"},
            headers=op_headers
        )
        agent_key = agent_resp.json()["api_key"]
        agent_headers = {"Authorization": f"Bearer {agent_key}"}

        # 2. Create Task with budget of $30
        task_id = f"task-cb-{int(time.time() * 1000)}"
        await client.post(
            "/api/v1/tasks",
            json={
                "task_id": task_id,
                "title": "Task Gate 1 Testing",
                "acceptance_criteria": "Verify Gate 1 violations",
                "budget": "30.00",
                "allowed_recipient": "contractor@sandbox.paypal.com",
                "assigned_agent_id": agent_id,
                "required_deliverables": ["code_diff", "test_report"],
                "deadline": (utc_now() + timedelta(days=1)).isoformat()
            },
            headers=op_headers
        )

        # Violation 1: Amount exceeds task budget (amount $35 > budget $30) -> 422 OVER_TASK_BUDGET
        resp_v1 = await client.post(
            "/api/v1/payout-requests",
            json={
                "task_id": task_id,
                "amount": "35.00",
                "deliverables": {"code_diff": "diff-content", "test_report": "pass"}
            },
            headers=agent_headers
        )
        assert resp_v1.status_code == 422
        assert resp_v1.json()["detail"]["error"] == "OVER_TASK_BUDGET"

        # Violation 2: Incomplete deliverables (missing test_report) -> 422 DELIVERABLES_INCOMPLETE
        resp_v2 = await client.post(
            "/api/v1/payout-requests",
            json={
                "task_id": task_id,
                "amount": "20.00",
                "deliverables": {"code_diff": "diff-content"}
            },
            headers=agent_headers
        )
        assert resp_v2.status_code == 422
        assert resp_v2.json()["detail"]["error"] == "DELIVERABLES_INCOMPLETE"

        # Violation 3: Hourly velocity cap exceeded (cap is $50, request is $20, but previous passes or request exceeds cap)
        # 3rd incident triggers circuit breaker!
        resp_v3 = await client.post(
            "/api/v1/payout-requests",
            json={
                "task_id": task_id,
                "amount": "55.00",  # Exceeds agent's hourly_cap ($50)
                "deliverables": {"code_diff": "diff-v3", "test_report": "pass-v3"}
            },
            headers=agent_headers
        )
        assert resp_v3.status_code == 422  # Fails amount > budget first ($55 > $30)
        assert resp_v3.json()["detail"]["error"] == "OVER_TASK_BUDGET"

        # Now agent has 3 incidents: agent must be QUARANTINED!
        # Next request must be rejected with 403 AGENT_QUARANTINED
        resp_quarantined = await client.post(
            "/api/v1/payout-requests",
            json={
                "task_id": task_id,
                "amount": "10.00",
                "deliverables": {"code_diff": "diff-q", "test_report": "pass-q"}
            },
            headers=agent_headers
        )
        assert resp_quarantined.status_code == 403
        assert resp_quarantined.json()["detail"]["error"] == "AGENT_QUARANTINED"

        # Operator releases quarantine: POST /api/v1/agents/{id}/release
        release_resp = await client.post(
            f"/api/v1/agents/{agent_id}/release",
            headers=op_headers
        )
        assert release_resp.status_code == 200
        assert release_resp.json()["status"] == "ACTIVE"

