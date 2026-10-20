import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
from sqlalchemy import select

from src.database import async_session_factory, init_db, engine, Base
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import TaskStatus, PayoutRequestStatus, AgentStatus
from src.services.auth import hash_api_key
from src.services.guardian import get_current_hour_window
from src.services.paypal_client import paypal_client
from src.services.poller import reconcile_all_pending

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

@pytest.fixture(autouse=True)
async def setup_test_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

@pytest.mark.asyncio
async def test_poller_reconciles_submitted_to_paid():
    async with async_session_factory() as db:
        agent_id = f"agent-poller-{int(time.time() * 1000)}"
        agent = AgentModel(
            agent_id=agent_id,
            name="Poller Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("200.00")
        )
        db.add(agent)

        hour_window = get_current_hour_window()
        window = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_window,
            reserved=Decimal("35.00"),
            spent=Decimal("0.00")
        )
        db.add(window)

        task = TaskModel(
            task_id=f"task-poller-{int(time.time() * 1000)}",
            title="Poller Task",
            acceptance_criteria="Verify poller",
            budget=Decimal("35.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=2),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        db.add(task)

        # Setup mock batch on PayPal client
        batch_id = f"BATCH_TEST_{int(time.time())}"
        item_id = f"ITEM_TEST_{int(time.time())}"
        paypal_client._mock_batches[batch_id] = {
            "batch_header": {
                "payout_batch_id": batch_id,
                "batch_status": "SUCCESS"
            },
            "items": [
                {
                    "payout_item_id": item_id,
                    "transaction_status": "SUCCESS"
                }
            ]
        }

        # Create SUBMITTED request with batch_id
        req = PayoutRequestModel(
            payout_request_id=f"req-poll-{int(time.time())}",
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("35.00"),
            deliverables={"doc": "url"},
            artifact_sha256="sha_poll_1",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"batch_{int(time.time())}",
            paypal_request_id=f"pp_req_{int(time.time())}",
            payout_batch_id=batch_id,
            created_at=utc_now()
        )
        db.add(req)
        await db.commit()

        # Run poller reconciliation
        updated = await reconcile_all_pending(db)
        assert updated == 1

        # Check DB state
        await db.refresh(req)
        assert req.status == PayoutRequestStatus.PAID
        await db.refresh(task)
        assert task.status == TaskStatus.PAID
        await db.refresh(window)
        assert window.reserved == Decimal("0.00")
        assert window.spent == Decimal("35.00")

@pytest.mark.asyncio
async def test_poller_reconciles_unclaimed_after_cancel_timeout():
    async with async_session_factory() as db:
        agent_id = f"agent-unclaimed-{int(time.time() * 1000)}"
        agent = AgentModel(
            agent_id=agent_id,
            name="Unclaimed Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("100.00")
        )
        db.add(agent)

        hour_window = get_current_hour_window()
        window = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_window,
            reserved=Decimal("20.00"),
            spent=Decimal("0.00")
        )
        db.add(window)

        task = TaskModel(
            task_id=f"task-unclaimed-{int(time.time() * 1000)}",
            title="Unclaimed Task",
            acceptance_criteria="Verify cancel recovery",
            budget=Decimal("20.00"),
            currency="USD",
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=2),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        db.add(task)

        batch_id = f"BATCH_TIMEOUT_{int(time.time())}"
        item_id = f"ITEM_TIMEOUT_{int(time.time())}"

        # Suppose after a 504 timeout, PayPal actually cancelled the item on their end
        paypal_client._mock_batches[batch_id] = {
            "batch_header": {
                "payout_batch_id": batch_id,
                "batch_status": "SUCCESS"
            },
            "items": [
                {
                    "payout_item_id": item_id,
                    "transaction_status": "CANCELED"
                }
            ]
        }

        # Local DB still says UNCLAIMED
        req = PayoutRequestModel(
            payout_request_id=f"req-unclaimed-{int(time.time())}",
            task_id=task.task_id,
            agent_id=agent_id,
            amount=Decimal("20.00"),
            deliverables={"doc": "url2"},
            artifact_sha256="sha_poll_2",
            status=PayoutRequestStatus.UNCLAIMED,
            decision_reasons=[],
            sender_batch_id=f"batch_to_{int(time.time())}",
            paypal_request_id=f"pp_to_{int(time.time())}",
            payout_batch_id=batch_id,
            created_at=utc_now()
        )
        db.add(req)
        await db.commit()

        # Run poller
        updated = await reconcile_all_pending(db)
        assert updated == 1

        # Check DB state: poller picked up CANCELED, transitioned request, returned task to OPEN, released budget!
        await db.refresh(req)
        assert req.status == PayoutRequestStatus.CANCELED
        await db.refresh(task)
        assert task.status == TaskStatus.OPEN
        await db.refresh(window)
        assert window.reserved == Decimal("0.00")
