import asyncio
import json
import uuid
import pytest
import httpx
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.main import app
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import AgentStatus, TaskStatus, PayoutRequestStatus
from src.services.auth import hash_api_key
from src.services.events import broadcaster, EventBroadcaster
from src.services.paypal_client import paypal_client
from src.services.guardian import get_current_hour_window
from src.services import gate2_reviewer
from conftest import wait_until

OP = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}


def _parse_sse_data(raw_msg: str) -> dict:
    """Extracts JSON payload from a raw SSE message (event: ...\ndata: ...\n\n)."""
    for line in raw_msg.strip().split("\n"):
        if line.startswith("data:"):
            return json.loads(line[len("data:"):].strip())
    raise ValueError(f"No data line found in SSE message: {raw_msg}")


@pytest.mark.asyncio
async def test_subscriber_receives_received_and_decision_events():
    """
    Test 1: A subscriber receives RECEIVED, APPROVED, SUBMITTED, and PAID events
    in order for a single successful submission.
    """
    async def run():
        q = broadcaster.subscribe()
        try:
            agent_id = "agent-sse-01"
            api_key = "test_key_sse_01"
            task_id = "task-sse-01"

            async with async_session_factory() as db:
                agent = AgentModel(
                    agent_id=agent_id,
                    name="SSE Agent",
                    api_key_hash=hash_api_key(api_key),
                    status=AgentStatus.ACTIVE,
                    hourly_cap=Decimal("200.00")
                )
                task = TaskModel(
                    task_id=task_id,
                    title="SSE Task",
                    acceptance_criteria="SSE Acceptance",
                    budget=Decimal("25.00"),
                    currency="USD",
                    allowed_recipient="contractor@example.com",
                    assigned_agent_id=agent_id,
                    required_deliverables=["report"],
                    deadline=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
                    status=TaskStatus.OPEN
                )
                db.add_all([agent, task])
                await db.commit()

            # Drain any task creation events
            while not q.empty():
                q.get_nowait()

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    "/api/v1/payout-requests",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"task_id": task_id, "amount": "25.00", "deliverables": {"report": "valid proof content"}}
                )
                assert resp.status_code == 202
                req_id = resp.json()["payout_request_id"]

                # 1. First event: wait for payout_request RECEIVED
                received_found = False
                while not received_found:
                    msg = await wait_until(lambda: q.get_nowait() if not q.empty() else None, timeout=5.0)
                    data = _parse_sse_data(msg)
                    if data.get("type") == "payout_request" and data.get("id") == req_id:
                        assert data["status"] == "RECEIVED"
                        assert data["updated_at"].endswith("Z")
                        received_found = True

                # 2. Drain Gate 2 background review & dispatch
                await gate2_reviewer.drain()

                # 3. Subsequent events: APPROVED -> SUBMITTED -> PAID
                statuses = []
                while not q.empty():
                    raw = q.get_nowait()
                    d = _parse_sse_data(raw)
                    if d.get("type") == "payout_request" and d.get("id") == req_id:
                        statuses.append(d.get("status"))

                assert "APPROVED" in statuses
                assert "SUBMITTED" in statuses
                assert "PAID" in statuses
                # Assert monotonic ordering
                assert statuses.index("APPROVED") < statuses.index("SUBMITTED") < statuses.index("PAID")
        finally:
            broadcaster.unsubscribe(q)

    await asyncio.wait_for(run(), timeout=10.0)


@pytest.mark.asyncio
async def test_no_event_published_when_commit_fails(monkeypatch):
    """
    Test 2: When database commit fails during an operation, no SSE event is published.
    Uses a real publishing endpoint (operator reject) and verifies the queue receives 0 items.
    """
    async def run():
        agent_id = "agent-fail-01"
        task_id = "task-fail-01"
        req_id = "req-fail-01"

        async with async_session_factory() as db:
            agent = AgentModel(
                agent_id=agent_id,
                name="Fail Agent",
                api_key_hash=hash_api_key("key"),
                status=AgentStatus.ACTIVE,
                hourly_cap=Decimal("100.00")
            )
            task = TaskModel(
                task_id=task_id,
                title="Fail Task",
                acceptance_criteria="Criteria",
                budget=Decimal("50.00"),
                currency="USD",
                allowed_recipient="c@example.com",
                assigned_agent_id=agent_id,
                required_deliverables=["report"],
                deadline=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
                status=TaskStatus.PAYOUT_IN_PROGRESS
            )
            current_window = get_current_hour_window()
            budget = BudgetWindowModel(
                agent_id=agent_id,
                window_start=current_window,
                reserved=Decimal("50.00"),
                spent=Decimal("0.00")
            )
            req = PayoutRequestModel(
                payout_request_id=req_id,
                task_id=task_id,
                agent_id=agent_id,
                amount=Decimal("50.00"),
                deliverables={"report": "doc"},
                artifact_sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                sender_batch_id=f"batch-{uuid.uuid4().hex}",
                paypal_request_id=f"req-{uuid.uuid4().hex}",
                window_start=current_window,
                status=PayoutRequestStatus.PENDING_HUMAN_APPROVAL,
                decision_reasons=["REVIEW_REQUIRED"]
            )
            db.add_all([agent, task, budget, req])
            await db.commit()

        q = broadcaster.subscribe()
        try:
            # Drain queue
            while not q.empty():
                q.get_nowait()

            # Monkeypatch AsyncSession.commit to raise an error
            original_commit = AsyncSession.commit

            async def failing_commit(self):
                raise RuntimeError("Simulated DB commit failure")

            monkeypatch.setattr(AsyncSession, "commit", failing_commit)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                with pytest.raises(RuntimeError, match="Simulated DB commit failure"):
                    await client.post(
                        f"/api/v1/payout-requests/{req_id}/reject",
                        headers=OP
                    )

            # Assert subscriber received absolutely NO events
            assert q.empty() is True
        finally:
            broadcaster.unsubscribe(q)

    await asyncio.wait_for(run(), timeout=10.0)


@pytest.mark.asyncio
async def test_slow_subscriber_dropped_and_does_not_block_others():
    """
    Test 3: A slow subscriber whose queue reaches capacity is dropped and does not block others.
    """
    test_broadcaster = EventBroadcaster(queue_maxsize=3)
    slow_q = test_broadcaster.subscribe()
    fast_q = test_broadcaster.subscribe()

    assert test_broadcaster.subscriber_count == 2

    received_fast = []
    # Publish 5 events. Fast subscriber drains as messages arrive; slow subscriber does not.
    for i in range(5):
        await test_broadcaster.publish("test_event", f"entity-{i}", "TEST_STATUS")
        if not fast_q.empty():
            received_fast.append(fast_q.get_nowait())

    while not fast_q.empty():
        received_fast.append(fast_q.get_nowait())
    assert len(received_fast) == 5

    # Slow subscriber was dropped because queue was full
    assert test_broadcaster.subscriber_count == 1
    assert slow_q not in test_broadcaster._subscribers

    # Slow subscriber's queue received items up to capacity and the termination sentinel (None)
    items_slow = []
    while not slow_q.empty():
        items_slow.append(slow_q.get_nowait())
    assert None in items_slow


@pytest.mark.asyncio
async def test_disconnect_subscriber_count_returns_to_zero():
    """
    Test 4: Drives the ASGI application directly with http.disconnect and verifies
    subscriber count returns to 0 without hanging.
    """
    async def run():
        assert broadcaster.subscriber_count == 0

        disconnect_event = asyncio.Event()
        first_call = True

        async def receive():
            nonlocal first_call
            if first_call:
                first_call = False
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect_event.wait()
            return {"type": "http.disconnect"}

        async def send(msg):
            pass

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": f"token={settings.OPERATOR_TOKEN}".encode("ascii"),
            "headers": [
                (b"host", b"testserver"),
                (b"accept", b"text/event-stream"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }

        app_task = asyncio.create_task(app(scope, receive, send))

        # Wait until subscriber registers
        await wait_until(lambda: broadcaster.subscriber_count == 1, timeout=5.0)
        assert broadcaster.subscriber_count == 1

        # Trigger client disconnect
        disconnect_event.set()
        await asyncio.wait_for(app_task, timeout=5.0)

        # Confirm subscriber count returned to 0
        assert broadcaster.subscriber_count == 0

    await asyncio.wait_for(run(), timeout=10.0)


@pytest.mark.asyncio
async def test_sse_auth_requires_operator_token():
    """
    Test 5: GET /api/v1/events requires ?token=<OPERATOR_TOKEN>; rejects unauthorized clients with 401.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Missing token
        resp1 = await client.get("/api/v1/events")
        assert resp1.status_code == 401
        assert resp1.json()["detail"]["error"] == "AUTH_INVALID"

        # Invalid token
        resp2 = await client.get("/api/v1/events?token=invalid-secret-token")
        assert resp2.status_code == 401
        assert resp2.json()["detail"]["error"] == "AUTH_INVALID"

        # No dangling subscribers registered
        assert broadcaster.subscriber_count == 0


@pytest.mark.asyncio
async def test_operator_approve_sequence_and_write_ahead_submitted(monkeypatch):
    """
    Test 6: Operator approve transitions PENDING_HUMAN_APPROVAL -> APPROVED, commits & publishes,
    then transitions APPROVED -> SUBMITTED (write-ahead commit) before PayPal call.
    While the mock PayPal call is paused, a separate session verifies the row on disk is SUBMITTED,
    and decided_by remains 'operator'.
    """
    async def run():
        agent_id = "agent-hitl-01"
        task_id = "task-hitl-01"
        req_id = "req-hitl-01"

        async with async_session_factory() as db:
            agent = AgentModel(
                agent_id=agent_id,
                name="HITL Agent",
                api_key_hash=hash_api_key("key"),
                status=AgentStatus.ACTIVE,
                hourly_cap=Decimal("100.00")
            )
            task = TaskModel(
                task_id=task_id,
                title="HITL Task",
                acceptance_criteria="Quality work",
                budget=Decimal("50.00"),
                currency="USD",
                allowed_recipient="contractor@sandbox.paypal.com",
                assigned_agent_id=agent_id,
                required_deliverables=["report"],
                deadline=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
                status=TaskStatus.PAYOUT_IN_PROGRESS
            )
            current_window = get_current_hour_window()
            budget = BudgetWindowModel(
                agent_id=agent_id,
                window_start=current_window,
                reserved=Decimal("50.00"),
                spent=Decimal("0.00")
            )
            req = PayoutRequestModel(
                payout_request_id=req_id,
                task_id=task_id,
                agent_id=agent_id,
                amount=Decimal("50.00"),
                deliverables={"report": "doc"},
                artifact_sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                sender_batch_id=f"batch-{uuid.uuid4().hex}",
                paypal_request_id=f"req-{uuid.uuid4().hex}",
                window_start=current_window,
                status=PayoutRequestStatus.PENDING_HUMAN_APPROVAL,
                decision_reasons=["MANUAL_REVIEW"]
            )
            db.add_all([agent, task, budget, req])
            await db.commit()

        q = broadcaster.subscribe()
        try:
            # Drain queue
            while not q.empty():
                q.get_nowait()

            paypal_entered = asyncio.Event()
            paypal_release = asyncio.Event()

            async def mock_create_payout(*args, **kwargs):
                paypal_entered.set()
                await paypal_release.wait()
                return {
                    "batch_header": {"payout_batch_id": "BATCH-PAUSED-01"},
                    "items": [{"payout_item_id": "ITEM-PAUSED-01", "transaction_status": "SUCCESS"}]
                }

            monkeypatch.setattr(paypal_client, "create_single_payout", mock_create_payout)

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                approve_task = asyncio.create_task(
                    client.post(f"/api/v1/payout-requests/{req_id}/approve", headers=OP)
                )

                # Wait until dispatch reaches PayPal call
                await asyncio.wait_for(paypal_entered.wait(), timeout=5.0)

                # Verify from a separate session while PayPal is paused
                async with async_session_factory() as separate_db:
                    db_req = (await separate_db.execute(
                        select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == req_id)
                    )).scalar_one()

                    assert db_req.status == PayoutRequestStatus.SUBMITTED
                    assert db_req.decided_by == "operator"

                # Check intermediate SSE events in queue: APPROVED followed by SUBMITTED
                events = []
                while not q.empty():
                    raw = q.get_nowait()
                    events.append(_parse_sse_data(raw))

                statuses = [e["status"] for e in events if e.get("type") == "payout_request" and e.get("id") == req_id]
                assert "APPROVED" in statuses
                assert "SUBMITTED" in statuses
                assert statuses.index("APPROVED") < statuses.index("SUBMITTED")

                # Release PayPal to allow settlement to complete
                paypal_release.set()

                resp = await asyncio.wait_for(approve_task, timeout=5.0)
                assert resp.status_code == 200
                assert resp.json()["status"] == "PAID"
                assert resp.json()["decided_by"] == "operator"

                # Final events: PAID
                while not q.empty():
                    raw = q.get_nowait()
                    events.append(_parse_sse_data(raw))

                final_statuses = [e["status"] for e in events if e.get("type") == "payout_request" and e.get("id") == req_id]
                assert "PAID" in final_statuses

                # Verify final DB state
                async with async_session_factory() as final_db:
                    final_req = (await final_db.execute(
                        select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == req_id)
                    )).scalar_one()
                    assert final_req.status == PayoutRequestStatus.PAID
                    assert final_req.decided_by == "operator"
        finally:
            broadcaster.unsubscribe(q)

    await asyncio.wait_for(run(), timeout=10.0)
