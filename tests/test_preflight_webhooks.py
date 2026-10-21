import pytest
from unittest.mock import patch
from decimal import Decimal
import uuid

from datetime import datetime, timedelta, timezone
from sqlalchemy import select
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

@pytest.mark.asyncio
async def test_pf1_webhook_failed_transition_unblocks_retry(isolated_test_db):
    """
    PF1 Pre-flight Test:
    If a state transition fails on first webhook delivery (e.g. DB or budget inconsistency error),
    the webhook event must NOT be committed as verified=True.
    PayPal's subsequent retry of the same event_id must be accepted and successfully processed.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with async_session_factory() as db:
        agent = AgentModel(
            agent_id="agent-pf1-tester",
            name="PF1 Agent",
            api_key_hash="dummy_hash",
            status=AgentStatus.ACTIVE
        )
        task = TaskModel(
            task_id="task-pf1-001",
            title="PF1 Task",
            acceptance_criteria="Verify PF1",
            budget=Decimal("50.00"),
            currency="USD",
            allowed_recipient="sb-test@personal.example.com",
            assigned_agent_id="agent-pf1-tester",
            required_deliverables=["proof"],
            deadline=now + timedelta(days=1),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        req_id = str(uuid.uuid4())
        req = PayoutRequestModel(
            payout_request_id=req_id,
            task_id="task-pf1-001",
            agent_id="agent-pf1-tester",
            amount=Decimal("30.00"),
            deliverables={"proof": "ok"},
            artifact_sha256="dummy_sha256",
            status=PayoutRequestStatus.SUBMITTED,
            sender_batch_id=f"payagent_{uuid.uuid4().hex[:12]}",
            paypal_request_id=req_id,
            payout_batch_id="BATCH-PF1-999"
        )
        item = PayoutItemModel(
            payout_item_id="ITEM-PF1-888",
            payout_request_id=req_id,
            transaction_status="SUBMITTED"
        )
        bw = BudgetWindowModel(
            agent_id="agent-pf1-tester",
            window_start=get_current_hour_window(),
            reserved=Decimal("30.00"),
            spent=Decimal("0.00")
        )
        db.add_all([agent, task, req, item, bw])
        await db.commit()

    event_id = f"WH-EVENT-{uuid.uuid4().hex[:8]}"
    webhook_payload = {
        "id": event_id,
        "event_type": "PAYMENT.PAYOUTS-ITEM.SUCCEEDED",
        "resource": {
            "payout_item_id": "ITEM-PF1-888",
            "payout_batch_id": "BATCH-PF1-999",
            "transaction_status": "SUCCESS"
        }
    }
    headers = {"mock": "header"}

    # 1. First attempt: transition raises an unexpected error
    original_transition = webhooks_module.transition_payout_request_status

    async def _failing_transition(db, req, target_status, decided_by=None):
        raise RuntimeError("Simulated transient DB / budget failure during transition")

    with patch("src.services.webhooks.transition_payout_request_status", side_effect=_failing_transition):
        async with async_session_factory() as db:
            with pytest.raises(RuntimeError, match="Simulated transient DB / budget failure"):
                await process_webhook_event(db, webhook_payload, headers, source="simulated")

    # Verify event was NOT committed as verified=True
    async with async_session_factory() as db:
        stmt = select(WebhookEventModel).where(
            WebhookEventModel.event_id == event_id,
            WebhookEventModel.verified == True
        )
        rec = (await db.execute(stmt)).scalar_one_or_none()
        assert rec is None, "Failed webhook delivery must NOT be marked verified=True"

        # Payout request must still be SUBMITTED
        req_row = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == req_id))).scalar_one()
        assert req_row.status == PayoutRequestStatus.SUBMITTED

    # 2. Second attempt (PayPal retry with exact same event_id): transition succeeds
    async with async_session_factory() as db:
        res = await process_webhook_event(db, webhook_payload, headers, source="simulated")
        assert res["status"] == "processed"
        assert res["new_status"] == "PAID"

    # Verify event IS now marked verified=True and request is PAID
    async with async_session_factory() as db:
        stmt = select(WebhookEventModel).where(
            WebhookEventModel.event_id == event_id,
            WebhookEventModel.verified == True
        )
        rec = (await db.execute(stmt)).scalar_one_or_none()
        assert rec is not None, "Successful webhook delivery must be marked verified=True"

        req_row = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == req_id))).scalar_one()
        assert req_row.status == PayoutRequestStatus.PAID

    # 3. Third attempt (duplicate): must return duplicate
    async with async_session_factory() as db:
        res_dup = await process_webhook_event(db, webhook_payload, headers, source="simulated")
        assert res_dup["status"] == "duplicate"
