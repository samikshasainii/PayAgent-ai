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
from src.services.state_machine import transition_payout_request_status

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

@pytest.mark.asyncio
async def test_cas_prevents_double_spend_race():
    """
    Direct reproduction of poller_race.py:
    Setup: two $30 requests in flight, so reserved = $60.
    The race: poller marks r1 PAID. Then dispatcher (holding stale r1 in SUBMITTED) also marks r1 PAID.
    With CAS, the second call sees rowcount == 0, does not double-convert funds, and leaves r2's hold intact:
    reserved = 30.00, spent = 30.00.
    """
    agent_id = f"agent-race-{int(time.time() * 1000)}"
    hour_window = get_current_hour_window()
    r1_id = f"req-race-1-{int(time.time())}"
    r2_id = f"req-race-2-{int(time.time())}"
    t1_id = f"task-race-1-{int(time.time())}"
    t2_id = f"task-race-2-{int(time.time())}"

    # Setup initial state
    async with async_session_factory() as db:
        agent = AgentModel(
            agent_id=agent_id,
            name="Race Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("200.00")
        )
        db.add(agent)

        # Budget window has $60 reserved (2x $30) and $0 spent
        window = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_window,
            reserved=Decimal("60.00"),
            spent=Decimal("0.00")
        )
        db.add(window)

        t1 = TaskModel(
            task_id=t1_id,
            title="Task 1",
            acceptance_criteria="Verify race 1",
            budget=Decimal("30.00"),
            currency="USD",
            allowed_recipient="c1@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=2),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        t2 = TaskModel(
            task_id=t2_id,
            title="Task 2",
            acceptance_criteria="Verify race 2",
            budget=Decimal("30.00"),
            currency="USD",
            allowed_recipient="c2@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=2),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        db.add_all([t1, t2])

        r1 = PayoutRequestModel(
            payout_request_id=r1_id,
            task_id=t1_id,
            agent_id=agent_id,
            amount=Decimal("30.00"),
            deliverables={"doc": "url1"},
            artifact_sha256="sha_race_1",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"batch_r1_{int(time.time())}",
            paypal_request_id=f"pp_r1_{int(time.time())}",
            payout_batch_id="PB_RACE_1",
            created_at=utc_now()
        )
        r2 = PayoutRequestModel(
            payout_request_id=r2_id,
            task_id=t2_id,
            agent_id=agent_id,
            amount=Decimal("30.00"),
            deliverables={"doc": "url2"},
            artifact_sha256="sha_race_2",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"batch_r2_{int(time.time())}",
            paypal_request_id=f"pp_r2_{int(time.time())}",
            payout_batch_id="PB_RACE_2",
            created_at=utc_now()
        )
        db.add_all([r1, r2])
        await db.commit()

    # Now open two distinct sessions simulating Poller and Dispatcher
    async with async_session_factory() as session_poller, async_session_factory() as session_dispatcher:
        # Both load r1 while it is still SUBMITTED in their local session
        r1_poller = (await session_poller.execute(
            select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == r1_id)
        )).scalar_one()

        r1_dispatcher = (await session_dispatcher.execute(
            select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == r1_id)
        )).scalar_one()

        assert r1_poller.status == PayoutRequestStatus.SUBMITTED
        assert r1_dispatcher.status == PayoutRequestStatus.SUBMITTED

        # Poller marks r1 PAID first and commits
        await transition_payout_request_status(session_poller, r1_poller, PayoutRequestStatus.PAID)
        await session_poller.commit()

        # Dispatcher, still holding its stale copy of r1 (status=SUBMITTED), attempts to mark PAID as well
        await transition_payout_request_status(
            session_dispatcher, 
            r1_dispatcher, 
            PayoutRequestStatus.PAID,
            expected_old_status=PayoutRequestStatus.SUBMITTED
        )
        await session_dispatcher.commit()

    # Verify final budget state: reserved must be $30.00 (protecting r2), spent must be $30.00 (for r1)
    async with async_session_factory() as verify_db:
        w = (await verify_db.execute(
            select(BudgetWindowModel).where(
                BudgetWindowModel.agent_id == agent_id,
                BudgetWindowModel.window_start == hour_window
            )
        )).scalar_one()

        assert w.spent == Decimal("30.00"), f"Expected spent=30.00, got {w.spent}"
        assert w.reserved == Decimal("30.00"), f"Expected reserved=30.00, got {w.reserved} (CAS prevented stealing r2 hold)"

@pytest.mark.asyncio
async def test_poller_per_row_savepoint_isolation():
    """
    Verifies that an error on one row during reconciliation does not abort the entire batch (P2).
    """
    agent_id = f"agent-iso-{int(time.time() * 1000)}"
    hour_window = get_current_hour_window()
    batch_ok = f"BATCH_OK_{int(time.time())}"
    batch_broken = f"BATCH_BROKEN_{int(time.time())}"

    # PayPal client returns success for OK batch
    paypal_client._mock_batches[batch_ok] = {
        "batch_header": {"payout_batch_id": batch_ok, "batch_status": "SUCCESS"},
        "items": [{"payout_item_id": f"ITEM_OK_{int(time.time())}", "transaction_status": "SUCCESS"}]
    }
    # Deliberately mock batch_broken to raise RuntimeError to simulate downstream failure
    paypal_client._mock_batches[batch_broken] = RuntimeError("PayPal downstream failure")

    async with async_session_factory() as db:
        agent = AgentModel(
            agent_id=agent_id,
            name="Iso Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.ACTIVE,
            hourly_cap=Decimal("200.00")
        )
        db.add(agent)

        window = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_window,
            reserved=Decimal("50.00"),
            spent=Decimal("0.00")
        )
        db.add(window)

        t_broken = TaskModel(
            task_id=f"t-broken-{int(time.time())}",
            title="Broken Task",
            acceptance_criteria="Crash",
            budget=Decimal("25.00"),
            currency="USD",
            allowed_recipient="broken@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=2),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        t_ok = TaskModel(
            task_id=f"t-ok-{int(time.time())}",
            title="OK Task",
            acceptance_criteria="Pass",
            budget=Decimal("25.00"),
            currency="USD",
            allowed_recipient="ok@sandbox.paypal.com",
            assigned_agent_id=agent_id,
            required_deliverables=["doc"],
            deadline=utc_now() + timedelta(days=2),
            status=TaskStatus.PAYOUT_IN_PROGRESS
        )
        db.add_all([t_broken, t_ok])

        r_broken = PayoutRequestModel(
            payout_request_id=f"req-broken-{int(time.time())}",
            task_id=t_broken.task_id,
            agent_id=agent_id,
            amount=Decimal("25.00"),
            deliverables={"doc": "url1"},
            artifact_sha256="sha_broken",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"batch_b_{int(time.time())}",
            paypal_request_id=f"pp_b_{int(time.time())}",
            payout_batch_id=batch_broken,
            created_at=utc_now()
        )
        r_ok = PayoutRequestModel(
            payout_request_id=f"req-ok-{int(time.time())}",
            task_id=t_ok.task_id,
            agent_id=agent_id,
            amount=Decimal("25.00"),
            deliverables={"doc": "url2"},
            artifact_sha256="sha_ok",
            status=PayoutRequestStatus.SUBMITTED,
            decision_reasons=["AUTO_APPROVED"],
            sender_batch_id=f"batch_ok_{int(time.time())}",
            paypal_request_id=f"pp_ok_{int(time.time())}",
            payout_batch_id=batch_ok,
            created_at=utc_now()
        )
        db.add_all([r_broken, r_ok])
        await db.commit()

        # Run poller. The broken batch must be caught and isolated; r_ok must succeed!
        updated = await reconcile_all_pending(db)
        assert updated == 1

        # Check DB state
        await db.refresh(r_ok)
        assert r_ok.status == PayoutRequestStatus.PAID
        await db.refresh(r_broken)
        assert r_broken.status == PayoutRequestStatus.SUBMITTED

