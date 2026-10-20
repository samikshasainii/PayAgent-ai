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
    },
    PayoutRequestStatus.BLOCKED: {
        PayoutRequestStatus.PAID,
        PayoutRequestStatus.UNCLAIMED,
        PayoutRequestStatus.FAILED,
        PayoutRequestStatus.RETURNED,
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

async def transition_payout_request_status(
    db: AsyncSession,
    request: PayoutRequestModel,
    new_status: PayoutRequestStatus,
    decided_by: Optional[str] = None
) -> PayoutRequestModel:
    """Executes state transition with strict transition guards, maintaining task status and budget holds."""
    old_status = request.status
    if old_status == new_status:
        return request

    allowed = ALLOWED_TRANSITIONS.get(old_status, set())
    if new_status not in allowed:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "ILLEGAL_STATE_TRANSITION",
                "message": f"Illegal state transition from {old_status.value} to {new_status.value}"
            }
        )

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

    # Handle transitions
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

    elif new_status == PayoutRequestStatus.SUBMITTED:
        if task:
            task.status = TaskStatus.PAYOUT_IN_PROGRESS

    await db.flush()
    return request

async def cancel_unclaimed_payout_request(
    db: AsyncSession,
    payout_request_id: str,
    operator_id: str = "operator"
) -> PayoutRequestModel:
    """
    Recalls an UNCLAIMED payout request by calling PayPal's cancel endpoint first,
    then transitioning the local state and releasing the budget reservation.
    Strictly raises HTTP 409 Conflict if request is in any state other than UNCLAIMED
    or if PayPal refuses cancellation.
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

    # Find item
    stmt = select(PayoutItemModel).where(PayoutItemModel.payout_request_id == payout_request_id)
    item_res = await db.execute(stmt)
    item = item_res.scalars().first()

    # Call PayPal first to cancel unclaimed item
    if item and item.payout_item_id:
        try:
            await paypal_client.cancel_unclaimed_item(item.payout_item_id)
            item.transaction_status = "CANCELED"
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "PAYPAL_CANCEL_REFUSED",
                    "message": f"PayPal rejected cancellation of item {item.payout_item_id}: {str(exc)}"
                }
            )

    # Now execute state machine transition locally
    return await transition_payout_request_status(
        db=db,
        request=request,
        new_status=PayoutRequestStatus.CANCELED,
        decided_by=operator_id
    )
