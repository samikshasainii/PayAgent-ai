import json
import time
import uuid
from datetime import datetime, timedelta, timezone

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

from decimal import Decimal
import pytest
import httpx
from httpx import AsyncClient, ASGITransport

from src.main import app
from src.config import settings
from src.database import async_session_factory, engine, Base
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import TaskStatus, PayoutRequestStatus, AgentStatus, DecisionReason
from src.schemas.task import TaskCreate, validate_currency_precision
from src.schemas.payout import PayoutRequestIn
from src.services.auth import hash_api_key
from src.services.guardian import compute_artifact_sha256, get_current_hour_window
from src.services.state_machine import (
    transition_payout_request_status,
    cancel_unclaimed_payout_request,
)
from src.services.paypal_client import paypal_client
from src.mcp_server.server import list_my_tasks, submit_task_payout, get_payout_status


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
    orig_mock = paypal_client.is_mock
    orig_client = paypal_client._http_client
    try:
        paypal_client.is_mock = False
        paypal_client._access_token = None
        paypal_client._token_expires_at = 0.0

        mock_token_response = {"access_token": "live-oauth-token-xyz", "expires_in": 120}

        def handler(request):
            return httpx.Response(200, json=mock_token_response)

        paypal_client._http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

        start_time = time.time()
        token = await paypal_client.get_access_token()
        assert token == "live-oauth-token-xyz"
        # Expiry is within 1s of now + 120
        assert abs(paypal_client._token_expires_at - (start_time + 120)) < 1.0

        # Cached call returns same token without re-fetching
        token2 = await paypal_client.get_access_token()
        assert token2 == token
    finally:
        paypal_client.is_mock = orig_mock
        paypal_client._http_client = orig_client


# --- Test 3: Saved-ID Pattern, Idempotency, and Corrected Resubmissions ---
@pytest.mark.asyncio
async def test_saved_id_pattern_and_replay_rejection():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        op_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

        # 1. Register Agent
        agent_id = f"agent-test-{int(time.time() * 1000)}"
        agent_resp = await client.post(
            "/api/v1/agents",
            json={"agent_id": agent_id, "name": "Test Worker Agent", "hourly_cap": "500.00"},
            headers=op_headers
        )
        assert agent_resp.status_code == 201
        agent_key = agent_resp.json()["api_key"]
        agent_headers = {"Authorization": f"Bearer {agent_key}"}

        # 2. Create Task with budget 40.00
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

        deliverables = {"data_report": "https://example.com/data_report.json"}

        # Submissions that fail Gate 1 (over-budget: $50 > $40)
        # Attempt 1 -> 422
        r1 = await client.post(
            "/api/v1/payout-requests",
            json={"task_id": task_id, "amount": "50.00", "deliverables": deliverables},
            headers=agent_headers
        )
        assert r1.status_code == 422

        # Attempt 2 -> 422 with same deliverables (must not crash with 500 or block retry)
        r2 = await client.post(
            "/api/v1/payout-requests",
            json={"task_id": task_id, "amount": "50.00", "deliverables": deliverables},
            headers=agent_headers
        )
        assert r2.status_code == 422

        # Attempt 3: Agent corrects the amount to $40.00 with the SAME deliverables
        # Must succeed and not be blocked by the previous POLICY_REJECTED attempts!
        r3 = await client.post(
            "/api/v1/payout-requests",
            json={"task_id": task_id, "amount": "40.00", "deliverables": deliverables},
            headers=agent_headers
        )
        assert r3.status_code == 201
        payout_data = r3.json()
        assert payout_data["status"] == "PAID"
        payout_req_id = payout_data["payout_request_id"]

        # Check saved-ID format per ARCHITECTURE.md §6.1
        expected_sender_batch_id = f"payagent_{uuid.UUID(payout_req_id).hex}"
        assert payout_data["sender_batch_id"] == expected_sender_batch_id
        assert payout_data["paypal_request_id"] == payout_req_id

        # 4. Attempt Replay with EXACT SAME artifact for another task
        # Since r3 was successfully PAID, replaying the artifact MUST raise 409 Conflict (ARTIFACT_REPLAY)
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
            json={"task_id": task_id_2, "amount": "40.00", "deliverables": deliverables},
            headers=agent_headers
        )
        assert replay_resp.status_code == 409
        err = replay_resp.json()["detail"]
        assert err["error"] == "ARTIFACT_REPLAY"
        assert err["status"] == "POLICY_REJECTED"


# --- Test 4: Decision Table Rules, Boundaries, and HITL 409 Guard ---
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

        # Case D: Attempting to re-approve an already approved/paid request returns 409
        re_approve = await client.post(
            f"/api/v1/payout-requests/{req_id}/approve",
            headers=op_headers
        )
        assert re_approve.status_code == 409

        # Case E: Attempting to reject an already approved/paid request returns 409
        re_reject = await client.post(
            f"/api/v1/payout-requests/{req_id}/reject",
            headers=op_headers
        )
        assert re_reject.status_code == 409


# --- Test 5: Budget Releases on CANCELED, RETURNED, and PAID->RETURNED ---
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

        req_uuid = uuid.uuid4()
        req = PayoutRequestModel(
            payout_request_id=str(req_uuid),
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("30.00"),
            deliverables={"doc": "http://doc"},
            artifact_sha256="dummy_sha256_cancel",
            status=PayoutRequestStatus.UNCLAIMED,
            decision_reasons=[],
            sender_batch_id=f"payagent_{req_uuid.hex}",
            paypal_request_id=str(req_uuid),
            window_start=hour_window,
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

        # Test PAID -> RETURNED takes money back out of SPENT, not reserved!
        window.reserved = Decimal("0.00")
        window.spent = Decimal("40.00")
        task.status = TaskStatus.PAID
        req_paid = PayoutRequestModel(
            payout_request_id=f"req-paid-{int(time.time())}",
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("40.00"),
            deliverables={"doc": "http://doc-paid"},
            artifact_sha256="dummy_sha256_paid",
            status=PayoutRequestStatus.PAID,
            decision_reasons=[],
            sender_batch_id=f"batch_paid_{int(time.time())}",
            paypal_request_id=f"pp_paid_{int(time.time())}",
            window_start=hour_window,
            created_at=utc_now()
        )
        db.add(req_paid)
        await db.commit()

        # Transition PAID -> RETURNED
        await transition_payout_request_status(db, req_paid, PayoutRequestStatus.RETURNED)
        await db.commit()
        await db.refresh(window)
        assert window.spent == Decimal("0.00")
        assert window.reserved == Decimal("0.00")
        await db.refresh(task)
        assert task.status == TaskStatus.OPEN

        # Test illegal transition guard raises 409
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await transition_payout_request_status(db, req_paid, PayoutRequestStatus.SUBMITTED)
        assert exc_info.value.status_code == 409


# --- Test 6: Strict HTTP 409 Conflict on Non-UNCLAIMED Cancellation and PayPal Refusal ---
@pytest.mark.asyncio
async def test_cancellation_unclaimed_and_paypal_guards():
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


# --- Test 7: Streamable HTTP MCP Tools Execution & HTTP Transport ---
@pytest.mark.asyncio
async def test_mcp_guardian_tools_and_http_transport():
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

    # 5. Test MCP Streamable HTTP endpoint over HTTP (initialize & tools/list)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            init_body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1.0"}
                }
            }
            hdrs = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
            init_resp = await client.post("/mcp", json=init_body, headers=hdrs)
            assert init_resp.status_code == 200
            session_id = init_resp.headers.get("mcp-session-id")
            if session_id:
                hdrs["mcp-session-id"] = session_id

            tools_body = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {}
            }
            tools_resp = await client.post("/mcp", json=tools_body, headers=hdrs)
            assert tools_resp.status_code == 200
            assert "submit_task_payout" in tools_resp.text


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

        # Violation 1: Amount exceeds task budget ($35 > $30) -> 422 OVER_TASK_BUDGET
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

        # Violation 3: Exceeds budget ($55 > $30) -> triggers circuit breaker
        resp_v3 = await client.post(
            "/api/v1/payout-requests",
            json={
                "task_id": task_id,
                "amount": "55.00",
                "deliverables": {"code_diff": "diff-v3", "test_report": "pass-v3"}
            },
            headers=agent_headers
        )
        assert resp_v3.status_code == 422

        # Agent is now QUARANTINED (3 incidents)
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


# --- Test 9: Payout Request Status Authorization ---
@pytest.mark.asyncio
async def test_payout_request_status_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        op_headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}

        # Register agent 1
        a1_resp = await client.post(
            "/api/v1/agents",
            json={"agent_id": f"a1-{int(time.time()*1000)}", "name": "Agent 1", "hourly_cap": "100.00"},
            headers=op_headers
        )
        a1_headers = {"Authorization": f"Bearer {a1_resp.json()['api_key']}"}

        # Register agent 2
        a2_resp = await client.post(
            "/api/v1/agents",
            json={"agent_id": f"a2-{int(time.time()*1000)}", "name": "Agent 2", "hourly_cap": "100.00"},
            headers=op_headers
        )
        a2_headers = {"Authorization": f"Bearer {a2_resp.json()['api_key']}"}

        # Create task for agent 1
        t1_id = f"task-auth-{int(time.time()*1000)}"
        await client.post(
            "/api/v1/tasks",
            json={
                "task_id": t1_id,
                "title": "Auth Task",
                "acceptance_criteria": "Done",
                "budget": "20.00",
                "allowed_recipient": "contractor@sandbox.paypal.com",
                "assigned_agent_id": a1_resp.json()["agent_id"],
                "required_deliverables": ["out"],
                "deadline": (utc_now() + timedelta(days=1)).isoformat()
            },
            headers=op_headers
        )

        # Agent 1 submits payout
        p_resp = await client.post(
            "/api/v1/payout-requests",
            json={"task_id": t1_id, "amount": "20.00", "deliverables": {"out": "res"}},
            headers=a1_headers
        )
        req_id = p_resp.json()["payout_request_id"]

        # 1. Unauthenticated lookup -> 401
        unauth_resp = await client.get(f"/api/v1/payout-requests/{req_id}")
        assert unauth_resp.status_code == 401

        # 2. Other agent lookup -> 403
        other_resp = await client.get(f"/api/v1/payout-requests/{req_id}", headers=a2_headers)
        assert other_resp.status_code == 403

        # 3. Owner agent lookup -> 200
        owner_resp = await client.get(f"/api/v1/payout-requests/{req_id}", headers=a1_headers)
        assert owner_resp.status_code == 200

        # 4. Operator lookup -> 200
        op_lookup = await client.get(f"/api/v1/payout-requests/{req_id}", headers=op_headers)
        assert op_lookup.status_code == 200


# --- Test 10: PayPal Cancel Refusal Maps to HTTP 409 ---
@pytest.mark.asyncio
async def test_paypal_cancel_refusal_returns_409():
    async with async_session_factory() as db:
        agent_id = f"agent-refuse-{int(time.time() * 1000)}"
        agent = AgentModel(
            agent_id=agent_id,
            name="Refuse Agent",
            api_key_hash=hash_api_key("test"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("100.00")
        )
        db.add(agent)

        task = TaskModel(
            task_id=f"t-refuse-{int(time.time())}",
            title="Refuse Task",
            acceptance_criteria="Check cancel refusal",
            budget=Decimal("20.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=1),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        db.add(task)

        req_uuid = uuid.uuid4()
        req = PayoutRequestModel(
            payout_request_id=str(req_uuid),
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("20.00"),
            deliverables={"doc": "http://doc"},
            artifact_sha256="dummy_sha256_refuse",
            status=PayoutRequestStatus.UNCLAIMED,
            decision_reasons=[],
            sender_batch_id=f"payagent_{req_uuid.hex}",
            paypal_request_id=str(req_uuid),
            created_at=utc_now()
        )
        db.add(req)

        item_id = f"item-refuse-{int(time.time())}"
        item = PayoutItemModel(
            payout_item_id=item_id,
            payout_request_id=str(req_uuid),
            transaction_status="SUCCESS"  # Already claimed by recipient at PayPal
        )
        db.add(item)
        await db.commit()

        # In PayPal mock, item is SUCCESS so PayPal refuses cancellation
        paypal_client._mock_items[item_id] = {"transaction_status": "SUCCESS"}

        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await cancel_unclaimed_payout_request(db, str(req_uuid))
        assert exc_info.value.status_code == 409
        assert exc_info.value.detail["error"] == "PAYPAL_CANCEL_REFUSED"


# --- Test 11: Ambiguous Network/Timeout Payout Failure Stays SUBMITTED ---
@pytest.mark.asyncio
async def test_ambiguous_payout_stays_submitted():
    from src.services.payout_service import dispatch_payout

    async with async_session_factory() as db:
        agent_id = f"agent-ambig-{int(time.time() * 1000)}"
        agent = AgentModel(
            agent_id=agent_id,
            name="Ambig Agent",
            api_key_hash=hash_api_key("test"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("100.00")
        )
        db.add(agent)

        hour_window = get_current_hour_window()
        window = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_window,
            reserved=Decimal("30.00"),
            spent=Decimal("0.00")
        )
        db.add(window)

        task = TaskModel(
            task_id=f"t-ambig-{int(time.time())}",
            title="Ambig Task",
            acceptance_criteria="Check ambiguous timeout handling",
            budget=Decimal("30.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=1),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        db.add(task)

        req_uuid = uuid.uuid4()
        req = PayoutRequestModel(
            payout_request_id=str(req_uuid),
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("30.00"),
            deliverables={"doc": "http://doc"},
            artifact_sha256="dummy_sha256_ambig",
            status=PayoutRequestStatus.APPROVED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"payagent_{req_uuid.hex}",
            paypal_request_id=str(req_uuid),
            window_start=hour_window,
            created_at=utc_now()
        )
        db.add(req)
        await db.commit()

        # Simulate PayPal network timeout
        orig_fn = paypal_client.create_single_payout
        async def mock_timeout(*args, **kwargs):
            raise httpx.ReadTimeout("Connection timed out waiting for PayPal response")
        paypal_client.create_single_payout = mock_timeout

        try:
            res_req = await dispatch_payout(db, req, task)
            # Under ARCHITECTURE.md §6.1, ambiguous timeout stays SUBMITTED
            assert res_req.status == PayoutRequestStatus.SUBMITTED
            assert any("PAYPAL_AMBIGUOUS_ERROR" in r for r in res_req.decision_reasons)
            # IDs are retained
            assert res_req.sender_batch_id == f"payagent_{req_uuid.hex}"
            assert res_req.paypal_request_id == str(req_uuid)
            # Budget reservation is NOT released
            await db.refresh(window)
            assert window.reserved == Decimal("30.00")
        finally:
            paypal_client.create_single_payout = orig_fn

