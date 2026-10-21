import asyncio
import uuid
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, AsyncMock
import httpx
import pytest
from sqlalchemy import select

from src.main import app
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.webhook import WebhookEventModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import AgentStatus, TaskStatus, PayoutRequestStatus
from src.services.guardian import get_current_hour_window
from src.services.webhooks import process_webhook_event
import src.services.webhooks as webhooks_module
from src.services.poller import reconcile_all_pending, apply_batch_reconciliation
from src.services.status_mapping import PAYPAL_ITEM_TO_REQUEST_STATUS, PAYPAL_EVENT_TO_REQUEST_STATUS


async def _create_test_pipeline(
    prefix: str,
    amount: Decimal = Decimal("30.00"),
    initial_status: PayoutRequestStatus = PayoutRequestStatus.SUBMITTED,
    reserved_amount: Decimal = Decimal("30.00"),
    spent_amount: Decimal = Decimal("0.00"),
    initial_task_status: TaskStatus = TaskStatus.PAYOUT_IN_PROGRESS,
    initial_item_status: str = "PENDING"
):
    """Helper to provision a complete isolated test pipeline in the database."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    agent_id = f"agent-{prefix}-{uuid.uuid4().hex[:6]}"
    task_id = f"task-{prefix}-{uuid.uuid4().hex[:6]}"
    req_id = str(uuid.uuid4())
    item_id = f"ITEM-{prefix}-{uuid.uuid4().hex[:8]}"
    batch_id = f"BATCH-{prefix}-{uuid.uuid4().hex[:8]}"
    sender_batch_id = f"payagent_{uuid.uuid4().hex[:12]}"
    hour_window = get_current_hour_window()

    async with async_session_factory() as db:
        agent = AgentModel(
            agent_id=agent_id,
            name=f"Agent {prefix}",
            api_key_hash="dummy_hash",
            status=AgentStatus.ACTIVE
        )
        task = TaskModel(
            task_id=task_id,
            title=f"Task {prefix}",
            acceptance_criteria="Verify criteria",
            budget=Decimal("100.00"),
            currency="USD",
            allowed_recipient="sb-test@personal.example.com",
            assigned_agent_id=agent_id,
            required_deliverables=["proof"],
            deadline=now + timedelta(days=1),
            status=initial_task_status
        )
        req = PayoutRequestModel(
            payout_request_id=req_id,
            task_id=task_id,
            agent_id=agent_id,
            amount=amount,
            deliverables={"proof": "ok"},
            artifact_sha256=f"dummy_{prefix}_{uuid.uuid4().hex[:8]}",
            status=initial_status,
            sender_batch_id=sender_batch_id,
            paypal_request_id=req_id,
            payout_batch_id=batch_id
        )
        item = PayoutItemModel(
            payout_item_id=item_id,
            payout_request_id=req_id,
            transaction_status=initial_item_status
        )
        bw = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_window,
            reserved=reserved_amount,
            spent=spent_amount
        )
        db.add_all([agent, task, req, item, bw])
        await db.commit()

    return {
        "agent_id": agent_id,
        "task_id": task_id,
        "req_id": req_id,
        "item_id": item_id,
        "batch_id": batch_id,
        "sender_batch_id": sender_batch_id,
        "hour_window": hour_window
    }


@pytest.mark.asyncio
async def test_counter1_partial_write_failure_and_retry(isolated_test_db):
    """
    Counter-Test 1 (Partial-write failure):
    Setup reserved=0.00 in budget window, deliver ITEM.SUCCEEDED.
    Verify request is SUBMITTED, item is PENDING, task is PAYOUT_IN_PROGRESS,
    no verified webhook event row exists, /api/v1/webhooks/simulate returns 500.
    Then set reserved=30.00, retry delivery: verify PAID, spent=30.00, and verified=True.
    """
    pipe = await _create_test_pipeline("ct1", amount=Decimal("30.00"), reserved_amount=Decimal("0.00"))
    event_id = f"WH-CT1-{uuid.uuid4().hex[:8]}"
    payload = {
        "id": event_id,
        "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
        "resource": {
            "payout_item_id": pipe["item_id"],
            "payout_batch_id": pipe["batch_id"],
            "transaction_status": "SUCCESS"
        }
    }
    headers = {"Authorization": "Bearer test-operator-secret-token"}

    # 1. Delivery with reserved=0.00: conversion to spent raises RuntimeError -> 500
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test"
    ) as client:
        resp = await client.post("/api/v1/webhooks/simulate", json=payload, headers=headers)
        assert resp.status_code == 500

    # Verify atomic rollback: request still SUBMITTED, item PENDING, task PAYOUT_IN_PROGRESS, no verified event
    async with async_session_factory() as db:
        req = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe["req_id"]))).scalar_one()
        assert req.status == PayoutRequestStatus.SUBMITTED

        item = (await db.execute(select(PayoutItemModel).where(PayoutItemModel.payout_item_id == pipe["item_id"]))).scalar_one()
        assert item.transaction_status == "PENDING"

        task = (await db.execute(select(TaskModel).where(TaskModel.task_id == pipe["task_id"]))).scalar_one()
        assert task.status == TaskStatus.PAYOUT_IN_PROGRESS

        wh_ev = (await db.execute(select(WebhookEventModel).where(
            WebhookEventModel.event_id == event_id,
            WebhookEventModel.verified == True
        ))).scalar_one_or_none()
        assert wh_ev is None, "Failed partial-write must roll back verified event record"

        # 2. Fix budget reservation to 30.00
        bw = (await db.execute(select(BudgetWindowModel).where(
            BudgetWindowModel.agent_id == pipe["agent_id"],
            BudgetWindowModel.window_start == pipe["hour_window"]
        ))).scalar_one()
        bw.reserved = Decimal("30.00")
        await db.commit()

    # 3. Retry delivery with exact same event_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp2 = await client.post("/api/v1/webhooks/simulate", json=payload, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "processed"

    # Verify state settled to PAID, spent=30.00, verified=True
    async with async_session_factory() as db:
        req = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe["req_id"]))).scalar_one()
        assert req.status == PayoutRequestStatus.PAID

        task = (await db.execute(select(TaskModel).where(TaskModel.task_id == pipe["task_id"]))).scalar_one()
        assert task.status == TaskStatus.PAID

        bw = (await db.execute(select(BudgetWindowModel).where(
            BudgetWindowModel.agent_id == pipe["agent_id"],
            BudgetWindowModel.window_start == pipe["hour_window"]
        ))).scalar_one()
        assert bw.spent == Decimal("30.00")
        assert bw.reserved == Decimal("0.00")

        wh_ev = (await db.execute(select(WebhookEventModel).where(
            WebhookEventModel.event_id == event_id,
            WebhookEventModel.verified == True
        ))).scalar_one_or_none()
        assert wh_ev is not None
        assert wh_ev.verified is True


@pytest.mark.asyncio
async def test_counter2_unverified_first_delivery_retryability(isolated_test_db):
    """
    Counter-Test 2 (Unverified first):
    Deliver unverified event (verified=False stored),
    then deliver verified event with a failing transition (verify it rolls back / stays verified=False),
    then deliver third verified event with valid transition (verify processed and verified=True).
    """
    pipe = await _create_test_pipeline("ct2", amount=Decimal("30.00"), reserved_amount=Decimal("0.00"))
    event_id = f"WH-CT2-{uuid.uuid4().hex[:8]}"
    payload = {
        "id": event_id,
        "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
        "resource": {
            "payout_item_id": pipe["item_id"],
            "payout_batch_id": pipe["batch_id"],
            "transaction_status": "SUCCESS"
        }
    }
    headers = {"paypal-transmission-id": "mock_id"}

    # 1. Deliver unverified event
    with patch("src.services.paypal_client.paypal_client.verify_webhook_signature", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = False
        async with async_session_factory() as db:
            res1 = await process_webhook_event(db, payload, headers, source="paypal")
            assert res1["status"] == "unverified"

    async with async_session_factory() as db:
        ev = (await db.execute(select(WebhookEventModel).where(WebhookEventModel.event_id == event_id))).scalar_one()
        assert ev.verified is False

    # 2. Deliver verified event with failing transition (reserved=0.00)
    with patch("src.services.paypal_client.paypal_client.verify_webhook_signature", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = True
        async with async_session_factory() as db:
            with pytest.raises(RuntimeError):
                await process_webhook_event(db, payload, headers, source="paypal")

    # Verify rollback: record still exists with verified=False
    async with async_session_factory() as db:
        ev = (await db.execute(select(WebhookEventModel).where(WebhookEventModel.event_id == event_id))).scalar_one()
        assert ev.verified is False

        # 3. Restore budget reservation
        bw = (await db.execute(select(BudgetWindowModel).where(
            BudgetWindowModel.agent_id == pipe["agent_id"],
            BudgetWindowModel.window_start == pipe["hour_window"]
        ))).scalar_one()
        bw.reserved = Decimal("30.00")
        await db.commit()

    # 4. Third delivery with valid transition
    with patch("src.services.paypal_client.paypal_client.verify_webhook_signature", new_callable=AsyncMock) as mock_verify:
        mock_verify.return_value = True
        async with async_session_factory() as db:
            res3 = await process_webhook_event(db, payload, headers, source="paypal")
            assert res3["status"] == "processed"

    async with async_session_factory() as db:
        ev = (await db.execute(select(WebhookEventModel).where(WebhookEventModel.event_id == event_id))).scalar_one()
        assert ev.verified is True
        req = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe["req_id"]))).scalar_one()
        assert req.status == PayoutRequestStatus.PAID


@pytest.mark.asyncio
async def test_counter3_concurrent_duplicates_race_safety(isolated_test_db):
    """
    Counter-Test 3 (Concurrent duplicates):
    Run two concurrent deliveries of the same event with asyncio.gather 5 times.
    Assert exactly 1 state change, spent=30.00, 1 verified event row,
    and exactly 1 result is processed while the other is already_applied or duplicate.
    """
    for i in range(5):
        pipe = await _create_test_pipeline(f"ct3_{i}", amount=Decimal("30.00"), reserved_amount=Decimal("30.00"))
        event_id = f"WH-CONC-{i}-{uuid.uuid4().hex[:8]}"
        payload = {
            "id": event_id,
            "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
            "resource": {
                "payout_item_id": pipe["item_id"],
                "payout_batch_id": pipe["batch_id"],
                "transaction_status": "SUCCESS"
            }
        }
        headers = {"mock": "header"}

        async def _deliver():
            async with async_session_factory() as db:
                return await process_webhook_event(db, payload, headers, source="simulated")

        res1, res2 = await asyncio.gather(_deliver(), _deliver())
        results = [res1.get("status"), res2.get("status")]

        assert "processed" in results, f"Iteration {i}: At least one concurrent delivery must be processed, got {results}"
        assert set(results).issubset({"processed", "already_applied", "duplicate"}), f"Iteration {i}: Unexpected statuses {results}"
        assert results.count("processed") == 1, f"Iteration {i}: Exactly one delivery must be processed, got {results}"

        async with async_session_factory() as db:
            # Exactly 1 state change to PAID
            req = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe["req_id"]))).scalar_one()
            assert req.status == PayoutRequestStatus.PAID

            # Budget spent=30.00, reserved=0.00
            bw = (await db.execute(select(BudgetWindowModel).where(
                BudgetWindowModel.agent_id == pipe["agent_id"],
                BudgetWindowModel.window_start == pipe["hour_window"]
            ))).scalar_one()
            assert bw.spent == Decimal("30.00")
            assert bw.reserved == Decimal("0.00")

            # Exactly 1 verified event row
            evs = (await db.execute(select(WebhookEventModel).where(
                WebhookEventModel.event_id == event_id,
                WebhookEventModel.verified == True
            ))).scalars().all()
            assert len(evs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("item_status, expected_req_status, expected_task_status, expected_reserved, expected_spent", [
    ("SUCCESS", PayoutRequestStatus.PAID, TaskStatus.PAID, Decimal("0.00"), Decimal("30.00")),
    ("FAILED", PayoutRequestStatus.FAILED, TaskStatus.OPEN, Decimal("0.00"), Decimal("0.00")),
    ("DENIED", PayoutRequestStatus.FAILED, TaskStatus.OPEN, Decimal("0.00"), Decimal("0.00")),
    ("BLOCKED", PayoutRequestStatus.BLOCKED, TaskStatus.PAYOUT_IN_PROGRESS, Decimal("30.00"), Decimal("0.00")),
    ("ONHOLD", PayoutRequestStatus.ONHOLD, TaskStatus.PAYOUT_IN_PROGRESS, Decimal("30.00"), Decimal("0.00")),
    ("UNCLAIMED", PayoutRequestStatus.UNCLAIMED, TaskStatus.PAYOUT_IN_PROGRESS, Decimal("30.00"), Decimal("0.00")),
    ("RETURNED", PayoutRequestStatus.RETURNED, TaskStatus.OPEN, Decimal("0.00"), Decimal("0.00")),
    ("REFUNDED", PayoutRequestStatus.RETURNED, TaskStatus.OPEN, Decimal("0.00"), Decimal("0.00")),
    ("REVERSED", PayoutRequestStatus.RETURNED, TaskStatus.OPEN, Decimal("0.00"), Decimal("0.00")),
])
async def test_counter4_poller_and_webhook_status_parity(
    isolated_test_db,
    item_status,
    expected_req_status,
    expected_task_status,
    expected_reserved,
    expected_spent
):
    """
    Counter-Test 4: Parametrized test over all 9 PayPal item statuses verifying that both
    the poller and webhook reach the exact same request status, budget figures, and task status.
    """
    # 1. Test via Poller path
    pipe_poller = await _create_test_pipeline(f"poller_{item_status}")
    batch_data = {
        "batch_header": {"payout_batch_id": pipe_poller["batch_id"]},
        "items": [{
            "payout_item_id": pipe_poller["item_id"],
            "transaction_status": item_status
        }]
    }
    async with async_session_factory() as db:
        req_poller = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe_poller["req_id"]))).scalar_one()
        await apply_batch_reconciliation(db, req_poller, batch_data)
        await db.commit()

    # 2. Test via Webhook path
    pipe_wh = await _create_test_pipeline(f"wh_{item_status}")
    event_payload = {
        "id": f"WH-PARITY-{item_status}-{uuid.uuid4().hex[:6]}",
        "event_type": "PAYMENT.PAYOUTS-ITEM." + ("SUCCEEDED" if item_status == "SUCCESS" else ("HELD" if item_status == "ONHOLD" else item_status)),
        "resource": {
            "payout_item_id": pipe_wh["item_id"],
            "payout_batch_id": pipe_wh["batch_id"],
            "transaction_status": item_status
        }
    }
    async with async_session_factory() as db:
        await process_webhook_event(db, event_payload, {}, source="simulated")

    # 3. Assert Parity between Poller and Webhook outcomes
    async with async_session_factory() as db:
        # Poller outcome
        rp = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe_poller["req_id"]))).scalar_one()
        tp = (await db.execute(select(TaskModel).where(TaskModel.task_id == pipe_poller["task_id"]))).scalar_one()
        bp = (await db.execute(select(BudgetWindowModel).where(BudgetWindowModel.agent_id == pipe_poller["agent_id"]))).scalar_one()

        # Webhook outcome
        rw = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe_wh["req_id"]))).scalar_one()
        tw = (await db.execute(select(TaskModel).where(TaskModel.task_id == pipe_wh["task_id"]))).scalar_one()
        bw = (await db.execute(select(BudgetWindowModel).where(BudgetWindowModel.agent_id == pipe_wh["agent_id"]))).scalar_one()

        # Assert exact agreement with expected invariants
        assert rp.status == expected_req_status
        assert rw.status == expected_req_status
        assert tp.status == expected_task_status
        assert tw.status == expected_task_status
        assert bp.reserved == expected_reserved
        assert bw.reserved == expected_reserved
        assert bp.spent == expected_spent
        assert bw.spent == expected_spent


@pytest.mark.asyncio
async def test_counter5_poller_sweeps_onhold_and_blocked(isolated_test_db):
    """
    Counter-Test 5: Verify that ONHOLD and BLOCKED requests are re-polled by reconcile_all_pending
    and settled when PayPal returns SUCCESS.
    """
    pipe_onhold = await _create_test_pipeline("sweep_hold", initial_status=PayoutRequestStatus.ONHOLD, initial_item_status="ONHOLD")
    pipe_blocked = await _create_test_pipeline("sweep_blk", initial_status=PayoutRequestStatus.BLOCKED, initial_item_status="BLOCKED")

    def _mock_get_payout_batch(batch_id):
        if batch_id == pipe_onhold["batch_id"]:
            item_id = pipe_onhold["item_id"]
        else:
            item_id = pipe_blocked["item_id"]
        return {
            "batch_header": {"payout_batch_id": batch_id},
            "items": [{
                "payout_item_id": item_id,
                "transaction_status": "SUCCESS"
            }]
        }

    with patch("src.services.poller.paypal_client.get_payout_batch", side_effect=_mock_get_payout_batch):
        async with async_session_factory() as db:
            updated = await reconcile_all_pending(db)
            assert updated == 2

    async with async_session_factory() as db:
        r_hold = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe_onhold["req_id"]))).scalar_one()
        r_blk = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe_blocked["req_id"]))).scalar_one()
        assert r_hold.status == PayoutRequestStatus.PAID
        assert r_blk.status == PayoutRequestStatus.PAID

        t_hold = (await db.execute(select(TaskModel).where(TaskModel.task_id == pipe_onhold["task_id"]))).scalar_one()
        t_blk = (await db.execute(select(TaskModel).where(TaskModel.task_id == pipe_blocked["task_id"]))).scalar_one()
        assert t_hold.status == TaskStatus.PAID
        assert t_blk.status == TaskStatus.PAID


@pytest.mark.asyncio
async def test_e2_payout_items_collision_raises_500_with_no_state_change(isolated_test_db):
    """
    E2 Entry Fix Test:
    Forces an IntegrityError on the payout_items table during webhook processing.
    Asserts that the webhook receiver re-raises the error (HTTP 500), does NOT treat it
    as already_applied, and preserves the payout request in SUBMITTED status with no budget changes.
    """
    pipe = await _create_test_pipeline("e2_coll", amount=Decimal("30.00"), reserved_amount=Decimal("30.00"))
    event_id = f"WH-E2-{uuid.uuid4().hex[:8]}"
    existing_item_id = f"ITEM-EXISTING-{uuid.uuid4().hex[:6]}"

    # Seed an existing payout_item row to collide with
    async with async_session_factory() as db:
        db.add(PayoutItemModel(
            payout_item_id=existing_item_id,
            payout_request_id=pipe["req_id"],
            transaction_status="PENDING"
        ))
        await db.commit()

    payload = {
        "id": event_id,
        "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
        "resource": {
            "payout_item_id": pipe["item_id"],
            "payout_batch_id": pipe["batch_id"],
            "transaction_status": "SUCCESS"
        }
    }
    headers = {"Authorization": "Bearer test-operator-secret-token"}

    # Intercept process_webhook_event to inject a conflicting payout_items row into the session before commit
    orig_transition = webhooks_module.transition_payout_request_status

    async def _transition_with_payout_items_collision(db, req, target_status, decided_by=None):
        res = await orig_transition(db, req, target_status, decided_by=decided_by)
        # Inject duplicate payout_item to force IntegrityError on payout_items PK
        db.add(PayoutItemModel(
            payout_item_id=existing_item_id,
            payout_request_id=req.payout_request_id,
            transaction_status="DUPLICATE"
        ))
        return res

    with patch("src.services.webhooks.transition_payout_request_status", side_effect=_transition_with_payout_items_collision):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test"
        ) as client:
            resp = await client.post("/api/v1/webhooks/simulate", json=payload, headers=headers)
            assert resp.status_code == 500, f"Expected 500 on payout_items collision, got {resp.status_code}: {resp.text}"

    # Verify atomic rollback: request still SUBMITTED, budget hold kept, no verified event
    async with async_session_factory() as db:
        req = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pipe["req_id"]))).scalar_one()
        assert req.status == PayoutRequestStatus.SUBMITTED

        task = (await db.execute(select(TaskModel).where(TaskModel.task_id == pipe["task_id"]))).scalar_one()
        assert task.status == TaskStatus.PAYOUT_IN_PROGRESS

        bw = (await db.execute(select(BudgetWindowModel).where(
            BudgetWindowModel.agent_id == pipe["agent_id"],
            BudgetWindowModel.window_start == pipe["hour_window"]
        ))).scalar_one()
        assert bw.reserved == Decimal("30.00")
        assert bw.spent == Decimal("0.00")

        wh_ev = (await db.execute(select(WebhookEventModel).where(
            WebhookEventModel.event_id == event_id,
            WebhookEventModel.verified == True
        ))).scalar_one_or_none()
        assert wh_ev is None, "Payout items collision must NOT mark webhook event verified=True"
