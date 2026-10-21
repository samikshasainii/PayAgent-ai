from decimal import Decimal
from typing import Optional, Dict, Set
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.task import TaskModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import PayoutRequestStatus, TaskStatus
from src.services.guardian import get_current_hour_window
from src.services.paypal_client import paypal_client

# Allowed transitions according to ARCHITECTURE.md §4.3
ALLOWED_TRANSITIONS: Dict[PayoutRequestStatus, Set[PayoutRequestStatus]] = {
    PayoutRequestStatus.RECEIVED: {
        PayoutRequestStatus.POLICY_REJECTED,
        PayoutRequestStatus.REVIEWED,
        PayoutRequestStatus.APPROVED,
        PayoutRequestStatus.PENDING_HUMAN_APPROVAL,
    },
    PayoutRequestStatus.REVIEWED: {
        PayoutRequestStatus.APPROVED,
        PayoutRequestStatus.PENDING_HUMAN_APPROVAL,
        PayoutRequestStatus.POLICY_REJECTED,
    },
    PayoutRequestStatus.PENDING_HUMAN_APPROVAL: {
        PayoutRequestStatus.APPROVED,
        PayoutRequestStatus.SUBMITTED,
        PayoutRequestStatus.HUMAN_REJECTED,
    },
    PayoutRequestStatus.APPROVED: {
        PayoutRequestStatus.SUBMITTED,
    },
    PayoutRequestStatus.SUBMITTED: {
        PayoutRequestStatus.PAID,
        PayoutRequestStatus.UNCLAIMED,
        PayoutRequestStatus.FAILED,
        PayoutRequestStatus.RETURNED,
        PayoutRequestStatus.BLOCKED,
        PayoutRequestStatus.ONHOLD,
    },
    PayoutRequestStatus.UNCLAIMED: {
        PayoutRequestStatus.CANCELED,
        PayoutRequestStatus.RETURNED,
        PayoutRequestStatus.PAID,
        PayoutRequestStatus.FAILED,
        PayoutRequestStatus.BLOCKED,
    },
    PayoutRequestStatus.ONHOLD: {
        PayoutRequestStatus.PAID,
        PayoutRequestStatus.UNCLAIMED,
        PayoutRequestStatus.FAILED,
        PayoutRequestStatus.RETURNED,
        PayoutRequestStatus.BLOCKED,
        PayoutRequestStatus.CANCELED,
    },
    PayoutRequestStatus.BLOCKED: {
        PayoutRequestStatus.PAID,
        PayoutRequestStatus.UNCLAIMED,
        PayoutRequestStatus.FAILED,
        PayoutRequestStatus.RETURNED,
        PayoutRequestStatus.ONHOLD,
        PayoutRequestStatus.CANCELED,
    },
    PayoutRequestStatus.PAID: {
        # e.g. Subsequent PayPal ACH reversal / 30-day return
        PayoutRequestStatus.RETURNED,
    },
    PayoutRequestStatus.POLICY_REJECTED: set(),
    PayoutRequestStatus.HUMAN_REJECTED: set(),
    PayoutRequestStatus.FAILED: set(),
    PayoutRequestStatus.CANCELED: set(),
    PayoutRequestStatus.RETURNED: set(),
}

async def release_budget_reservation(
    db: AsyncSession,
    agent_id: str,
    amount: Decimal,
    created_at_window: Optional[str] = None
):
    """Releases reserved budget back to the agent's available limit."""
    window_start = created_at_window or get_current_hour_window()
    stmt = (
        update(BudgetWindowModel)
        .where(
            BudgetWindowModel.agent_id == agent_id,
            BudgetWindowModel.window_start == window_start,
            BudgetWindowModel.reserved >= amount
        )
        .values(reserved=BudgetWindowModel.reserved - amount)
    )
    res = await db.execute(stmt)
    if res.rowcount == 0:
        raise RuntimeError(f"Failed to release budget reservation of {amount} for agent {agent_id} in window {window_start}")

async def convert_reservation_to_spent(
    db: AsyncSession,
    agent_id: str,
    amount: Decimal,
    created_at_window: Optional[str] = None
):
    """Moves reserved funds to spent upon confirmed settlement."""
    window_start = created_at_window or get_current_hour_window()
    stmt = (
        update(BudgetWindowModel)
        .where(
            BudgetWindowModel.agent_id == agent_id,
            BudgetWindowModel.window_start == window_start,
            BudgetWindowModel.reserved >= amount
        )
        .values(
            reserved=BudgetWindowModel.reserved - amount,
            spent=BudgetWindowModel.spent + amount
        )
    )
    res = await db.execute(stmt)
    if res.rowcount == 0:
        raise RuntimeError(f"Failed to convert reservation of {amount} to spent for agent {agent_id} in window {window_start}")

async def release_spent_budget(
    db: AsyncSession,
    agent_id: str,
    amount: Decimal,
    created_at_window: Optional[str] = None
):
    """Releases spent funds (e.g. on RETURNED after PAID)."""
    window_start = created_at_window or get_current_hour_window()
    stmt = (
        update(BudgetWindowModel)
        .where(
            BudgetWindowModel.agent_id == agent_id,
            BudgetWindowModel.window_start == window_start,
            BudgetWindowModel.spent >= amount
        )
        .values(spent=BudgetWindowModel.spent - amount)
    )
    res = await db.execute(stmt)
    if res.rowcount == 0:
        raise RuntimeError(f"Failed to release spent budget of {amount} for agent {agent_id} in window {window_start}")

class TransitionResult:
    """
    Result object returned by transition_payout_request_status indicating
    the updated PayoutRequestModel and whether this call won the atomic CAS.
    """
    def __init__(self, request: PayoutRequestModel, won: bool):
        self.request = request
        self.won = won

    def __bool__(self) -> bool:
        return self.won

    def __iter__(self):
        yield self.request
        yield self.won

    def __getattr__(self, name):
        return getattr(self.request, name)

    def __repr__(self):
        return f"<TransitionResult request={self.request.payout_request_id} status={self.request.status} won={self.won}>"


async def transition_payout_request_status(
    db: AsyncSession,
    request: PayoutRequestModel,
    new_status: PayoutRequestStatus,
    decided_by: Optional[str] = None,
    expected_old_status: Optional[PayoutRequestStatus] = None
) -> TransitionResult:
    """
    Executes state transition with atomic Compare-and-Set (CAS).
    Only if exactly 1 row changed in the database will budget and task updates be applied.
    If 0 rows changed (another concurrent writer won the transition), treats as already transitioned
    and returns TransitionResult(request, won=False) without touching budget holds.
    """
    old_status = expected_old_status or request.status
    if old_status == new_status:
        return TransitionResult(request, False)

    allowed = ALLOWED_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "ILLEGAL_STATE_TRANSITION",
                "message": f"Illegal state transition from {old_status.value} to {new_status.value}"
            }
        )

    # Atomic Compare-and-Set in database
    cas_values = {"status": new_status}
    if decided_by:
        cas_values["decided_by"] = decided_by

    cas_stmt = (
        update(PayoutRequestModel)
        .where(
            PayoutRequestModel.payout_request_id == request.payout_request_id,
            PayoutRequestModel.status == old_status
        )
        .values(**cas_values)
    )
    cas_res = await db.execute(cas_stmt)
    if cas_res.rowcount == 0:
        # Race lost: another writer already updated the status in DB
        await db.refresh(request)
        return TransitionResult(request, False)

    # Update in-memory object attributes
    request.status = new_status
    if decided_by:
        request.decided_by = decided_by

    # Fetch associated task
    stmt = select(TaskModel).where(TaskModel.task_id == request.task_id)
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()

    window_bucket = getattr(request, "window_start", None) or (
        request.created_at.strftime("%Y-%m-%d-%H") if request.created_at else get_current_hour_window()
    )

    # Handle budget and task transitions only for the CAS winner
    if new_status == PayoutRequestStatus.PAID:
        if task:
            task.status = TaskStatus.PAID
        await convert_reservation_to_spent(db, request.agent_id, request.amount, window_bucket)

    elif new_status == PayoutRequestStatus.RETURNED:
        if task:
            task.status = TaskStatus.OPEN
        if old_status == PayoutRequestStatus.PAID:
            # Funds were already converted to spent; take back out of spent
            await release_spent_budget(db, request.agent_id, request.amount, window_bucket)
        else:
            # Funds were still reserved
            await release_budget_reservation(db, request.agent_id, request.amount, window_bucket)

    elif new_status in (PayoutRequestStatus.HUMAN_REJECTED, PayoutRequestStatus.FAILED, PayoutRequestStatus.CANCELED):
        if task and task.status == TaskStatus.PAYOUT_IN_PROGRESS:
            task.status = TaskStatus.OPEN
        await release_budget_reservation(db, request.agent_id, request.amount, window_bucket)

    elif new_status in (PayoutRequestStatus.SUBMITTED, PayoutRequestStatus.BLOCKED, PayoutRequestStatus.ONHOLD, PayoutRequestStatus.UNCLAIMED):
        if task:
            task.status = TaskStatus.PAYOUT_IN_PROGRESS

    await db.flush()
    return TransitionResult(request, True)

async def cancel_unclaimed_payout_request(
    db: AsyncSession,
    payout_request_id: str,
    operator_id: str = "operator"
) -> PayoutRequestModel:
    """
    Recalls an UNCLAIMED payout request by calling PayPal's cancel endpoint first,
    then transitioning the local state and releasing the budget reservation.
    Strictly raises HTTP 409 Conflict if request is in any state other than UNCLAIMED,
    if the associated payout item is missing, or if PayPal refuses cancellation.
    Raises HTTP 504 on network timeout to keep request safely in UNCLAIMED.
    """
    stmt = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
    res = await db.execute(stmt)
    request = res.scalar_one_or_none()

    if not request:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "REQUEST_NOT_FOUND", "message": f"Payout request {payout_request_id} not found"}
        )

    if request.status != PayoutRequestStatus.UNCLAIMED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "INVALID_STATE_FOR_CANCELLATION",
                "message": f"Cannot cancel payout request in state '{request.status.value}'. Cancellation is only permitted for UNCLAIMED requests."
            }
        )

    # Find associated payout item
    stmt = select(PayoutItemModel).where(PayoutItemModel.payout_request_id == payout_request_id)
    item_res = await db.execute(stmt)
    item = item_res.scalars().first()

    if not item or not item.payout_item_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PAYPAL_ITEM_NOT_FOUND",
                "message": f"Cannot cancel payout request {payout_request_id}: missing associated PayPal payout item ID."
            }
        )

    import httpx
    # Call PayPal first to cancel unclaimed item
    try:
        await paypal_client.cancel_unclaimed_item(item.payout_item_id)
        item.transaction_status = "CANCELED"
    except (httpx.TimeoutException, httpx.RequestError) as net_err:
        # Ambiguous network timeout: PayPal may have processed cancel; do not treat as refusal, keep UNCLAIMED
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail={
                "error": "PAYPAL_CANCEL_TIMEOUT",
                "message": f"Timeout or network error communicating with PayPal during cancellation: {str(net_err)}. Request remains UNCLAIMED."
            }
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PAYPAL_CANCEL_REFUSED",
                "message": f"PayPal rejected cancellation of item {item.payout_item_id}: {exc.response.text}"
            }
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "PAYPAL_CANCEL_REFUSED",
                "message": f"PayPal rejected cancellation of item {item.payout_item_id}: {str(exc)}"
            }
        )

    # Now execute state machine transition locally
    res = await transition_payout_request_status(
        db=db,
        request=request,
        new_status=PayoutRequestStatus.CANCELED,
        decided_by=operator_id
    )
    return res.request
