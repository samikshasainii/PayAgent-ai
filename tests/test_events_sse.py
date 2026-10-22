import asyncio
import json
import uuid
import pytest
import httpx
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.main import app
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
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
    Also tests non-ASCII query token and non-ASCII Bearer header on operator endpoint return 401 instead of 500.
    Wrapped in asyncio.wait_for(timeout=10) with client timeout=5 so it never hangs if auth check is omitted.
    """
    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Missing token
            resp1 = await client.get("/api/v1/events", timeout=5.0)
            assert resp1.status_code == 401
            assert resp1.json()["detail"]["error"] == "AUTH_INVALID"

            # Invalid token
            resp2 = await client.get("/api/v1/events?token=invalid-secret-token", timeout=5.0)
            assert resp2.status_code == 401
            assert resp2.json()["detail"]["error"] == "AUTH_INVALID"

            # Non-ASCII query token (?token=%C3%A9t%C3%A9) -> 401, not 500
            resp3 = await client.get("/api/v1/events?token=%C3%A9t%C3%A9", timeout=5.0)
            assert resp3.status_code == 401
            assert resp3.json()["detail"]["error"] == "AUTH_INVALID"

            # Non-ASCII Bearer token on operator endpoint -> 401, not 500
            resp4 = await client.get("/api/v1/tasks", headers=[(b"authorization", b"Bearer " + "\u00e9t\u00e9".encode("utf-8"))], timeout=5.0)
            assert resp4.status_code == 401

            # No dangling subscribers registered
            assert broadcaster.subscriber_count == 0

    await asyncio.wait_for(run(), timeout=10.0)


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


@pytest.mark.asyncio
async def test_poller_emits_after_commit_and_no_transaction_during_paypal_calls(monkeypatch):
    """
    Test 7: Poller reconciles SUBMITTED -> PAID for two requests.
    Verifies that no DB transaction is open during PayPal get_payout_batch calls,
    events are published only after commit (db.in_transaction() is False),
    and db.in_transaction() is False when reconciliation returns.
    """
    async with async_session_factory() as db:
        agent_id = f"ag-poll-{uuid.uuid4().hex[:6]}"
        agent = AgentModel(
            agent_id=agent_id,
            name="Poller Test Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("500.00")
        )
        current_window = get_current_hour_window()
        budget = BudgetWindowModel(
            agent_id=agent_id,
            window_start=current_window,
            reserved=Decimal("60.00"),
            spent=Decimal("0.00")
        )
        db.add_all([agent, budget])

        req_ids = []
        task_ids = []
        for i in range(2):
            tid = f"task-poll-{i}-{uuid.uuid4().hex[:6]}"
            rid = f"req-poll-{i}-{uuid.uuid4().hex[:6]}"
            bid = f"BATCH-MOCK-{i}-{uuid.uuid4().hex[:6]}"
            item_id = f"ITEM-MOCK-{i}-{uuid.uuid4().hex[:6]}"
            task = TaskModel(
                task_id=tid,
                title=f"Task {i}",
                acceptance_criteria="Criteria",
                budget=Decimal("30.00"),
                currency="USD",
                allowed_recipient=f"contractor{i}@sandbox.paypal.com",
                assigned_agent_id=agent_id,
                required_deliverables=["report"],
                deadline=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
                status=TaskStatus.PAYOUT_IN_PROGRESS
            )
            req = PayoutRequestModel(
                payout_request_id=rid,
                task_id=tid,
                agent_id=agent_id,
                amount=Decimal("30.00"),
                deliverables={"report": "doc"},
                artifact_sha256=f"sha_{i}_{uuid.uuid4().hex}",
                status=PayoutRequestStatus.SUBMITTED,
                decision_reasons=["AUTO_APPROVED"],
                sender_batch_id=f"sb_{i}_{uuid.uuid4().hex}",
                paypal_request_id=f"pr_{i}_{uuid.uuid4().hex}",
                payout_batch_id=bid,
                window_start=current_window,
            )
            item = PayoutItemModel(
                payout_item_id=item_id,
                payout_request_id=rid,
                transaction_status="PENDING"
            )
            paypal_client._mock_batches[bid] = {
                "batch_header": {"payout_batch_id": bid, "batch_status": "SUCCESS"},
                "items": [{"payout_item_id": item_id, "transaction_status": "SUCCESS"}]
            }
            db.add_all([task, req, item])
            req_ids.append(rid)
            task_ids.append(tid)
        await db.commit()

        # Spies
        fetch_tx_states = []
        orig_get_batch = paypal_client.get_payout_batch

        async def spy_get_batch(batch_id):
            fetch_tx_states.append(db.in_transaction())
            return await orig_get_batch(batch_id)

        monkeypatch.setattr(paypal_client, "get_payout_batch", spy_get_batch)

        publish_records = []
        orig_publish = broadcaster.publish

        async def spy_publish(event_type, entity_id, status, updated_at=None):
            publish_records.append((event_type, entity_id, status, db.in_transaction()))
            return await orig_publish(event_type, entity_id, status, updated_at)

        monkeypatch.setattr(broadcaster, "publish", spy_publish)

        # Run reconciliation
        from src.services.poller import reconcile_all_pending
        updated = await reconcile_all_pending(db)
        assert updated == 2

        # Assert: every PayPal fetch sees False (outside any DB transaction)
        assert len(fetch_tx_states) == 2
        assert fetch_tx_states == [False, False]

        # Assert: events are payout_request PAID and task PAID for both rows, all published with in_transaction == False
        for rid in req_ids:
            assert ("payout_request", rid, "PAID", False) in publish_records
        for tid in task_ids:
            assert ("task", tid, "PAID", False) in publish_records

        # Assert: db.in_transaction() is False when it returns
        assert db.in_transaction() is False


@pytest.mark.asyncio
async def test_verified_webhook_emits_and_deduplicates(monkeypatch):
    """
    Test 8: A verified webhook publishes payout_request PAID and task PAID.
    A duplicate delivery (already_applied) publishes nothing.
    """
    from src.services.webhooks import process_webhook_event

    monkeypatch.setattr(paypal_client, "verify_webhook_signature", AsyncMock(return_value=True))

    agent_id = f"ag-wh-{uuid.uuid4().hex[:6]}"
    task_id = f"task-wh-{uuid.uuid4().hex[:6]}"
    req_id = f"req-wh-{uuid.uuid4().hex[:6]}"
    item_id = f"ITEM-WH-{uuid.uuid4().hex[:6]}"
    batch_id = f"BATCH-WH-{uuid.uuid4().hex[:6]}"
    current_window = get_current_hour_window()

    async with async_session_factory() as db:
        agent = AgentModel(
            agent_id=agent_id,
            name="WH Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("200.00")
        )
        task = TaskModel(
            task_id=task_id,
            title="WH Task",
            acceptance_criteria="Criteria",
            budget=Decimal("40.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["report"],
            deadline=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        budget = BudgetWindowModel(
            agent_id=agent_id,
            window_start=current_window,
            reserved=Decimal("40.00"),
            spent=Decimal("0.00")
        )
        req = PayoutRequestModel(
            payout_request_id=req_id,
            task_id=task_id,
            agent_id=agent_id,
            amount=Decimal("40.00"),
            deliverables={"report": "doc"},
            artifact_sha256=f"sha_{uuid.uuid4().hex}",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"sb_{uuid.uuid4().hex}",
            paypal_request_id=f"pr_{uuid.uuid4().hex}",
            payout_batch_id=batch_id,
            window_start=current_window
        )
        item = PayoutItemModel(
            payout_item_id=item_id,
            payout_request_id=req_id,
            transaction_status="PENDING"
        )
        db.add_all([agent, task, budget, req, item])
        await db.commit()

    q = broadcaster.subscribe()
    try:
        while not q.empty():
            q.get_nowait()

        payload = {
            "id": f"WH-EV-{uuid.uuid4().hex}",
            "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
            "resource": {
                "payout_item_id": item_id,
                "payout_batch_id": batch_id,
                "transaction_status": "SUCCESS"
            }
        }
        headers = {"paypal-transmission-id": "fake-header"}

        async with async_session_factory() as db:
            res1 = await process_webhook_event(db, payload, headers, source="paypal")
            assert res1.get("status") == "processed"

        # Check published events: payout_request PAID and task PAID
        events = []
        while not q.empty():
            events.append(_parse_sse_data(q.get_nowait()))

        payout_events = [e for e in events if e.get("type") == "payout_request" and e.get("id") == req_id]
        task_events = [e for e in events if e.get("type") == "task" and e.get("id") == task_id]
        assert len(payout_events) == 1
        assert payout_events[0]["status"] == "PAID"
        assert len(task_events) == 1
        assert task_events[0]["status"] == "PAID"

        # Duplicate delivery: must publish NOTHING
        while not q.empty():
            q.get_nowait()

        payload_dup = dict(payload)
        payload_dup["id"] = f"WH-EV-DUP-{uuid.uuid4().hex}"
        async with async_session_factory() as db:
            res2 = await process_webhook_event(db, payload_dup, headers, source="paypal")
            assert res2.get("status") == "already_applied"

        assert q.empty() is True
    finally:
        broadcaster.unsubscribe(q)


@pytest.mark.asyncio
async def test_webhook_commit_failure_emits_nothing(monkeypatch):
    """
    Test 9: When a database commit raises inside process_webhook_event,
    no SSE event is published and the subscriber queue stays empty.
    """
    from src.services.webhooks import process_webhook_event

    monkeypatch.setattr(paypal_client, "verify_webhook_signature", AsyncMock(return_value=True))

    agent_id = f"ag-wh-fail-{uuid.uuid4().hex[:6]}"
    task_id = f"task-wh-fail-{uuid.uuid4().hex[:6]}"
    req_id = f"req-wh-fail-{uuid.uuid4().hex[:6]}"
    item_id = f"ITEM-WH-FAIL-{uuid.uuid4().hex[:6]}"
    batch_id = f"BATCH-WH-FAIL-{uuid.uuid4().hex[:6]}"
    current_window = get_current_hour_window()

    async with async_session_factory() as db:
        agent = AgentModel(
            agent_id=agent_id,
            name="WH Fail Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("200.00")
        )
        task = TaskModel(
            task_id=task_id,
            title="WH Fail Task",
            acceptance_criteria="Criteria",
            budget=Decimal("40.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["report"],
            deadline=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        budget = BudgetWindowModel(
            agent_id=agent_id,
            window_start=current_window,
            reserved=Decimal("40.00"),
            spent=Decimal("0.00")
        )
        req = PayoutRequestModel(
            payout_request_id=req_id,
            task_id=task_id,
            agent_id=agent_id,
            amount=Decimal("40.00"),
            deliverables={"report": "doc"},
            artifact_sha256=f"sha_{uuid.uuid4().hex}",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"sb_{uuid.uuid4().hex}",
            paypal_request_id=f"pr_{uuid.uuid4().hex}",
            payout_batch_id=batch_id,
            window_start=current_window
        )
        item = PayoutItemModel(
            payout_item_id=item_id,
            payout_request_id=req_id,
            transaction_status="PENDING"
        )
        db.add_all([agent, task, budget, req, item])
        await db.commit()

    q = broadcaster.subscribe()
    try:
        while not q.empty():
            q.get_nowait()

        payload = {
            "id": f"WH-FAIL-{uuid.uuid4().hex}",
            "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
            "resource": {
                "payout_item_id": item_id,
                "payout_batch_id": batch_id,
                "transaction_status": "SUCCESS"
            }
        }
        headers = {"paypal-transmission-id": "fake-header"}

        async with async_session_factory() as db:
            async def failing_commit():
                raise RuntimeError("Simulated webhook DB commit failure")

            monkeypatch.setattr(db, "commit", failing_commit)

            with pytest.raises(RuntimeError, match="Simulated webhook DB commit failure"):
                await process_webhook_event(db, payload, headers, source="paypal")

        assert q.empty() is True
    finally:
        broadcaster.unsubscribe(q)
