from decimal import Decimal
from typing import Optional
from fastapi import HTTPException, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.task import TaskModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import PayoutRequestStatus, TaskStatus
from src.services.guardian import get_current_hour_window

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
    await db.execute(stmt)

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
    await db.execute(stmt)

async def transition_payout_request_status(
    db: AsyncSession,
    request: PayoutRequestModel,
    new_status: PayoutRequestStatus,
    decided_by: Optional[str] = None
) -> PayoutRequestModel:
    """Executes state transition, maintaining task status and budget holds."""
    old_status = request.status
    request.status = new_status
    if decided_by:
        request.decided_by = decided_by

    # Fetch associated task
    stmt = select(TaskModel).where(TaskModel.task_id == request.task_id)
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()

    window_bucket = request.created_at.strftime("%Y-%m-%d-%H")

    # Handle transitions
    if new_status == PayoutRequestStatus.PAID:
        if task:
            task.status = TaskStatus.PAID
        await convert_reservation_to_spent(db, request.agent_id, request.amount, window_bucket)

    elif new_status in (PayoutRequestStatus.HUMAN_REJECTED, PayoutRequestStatus.FAILED, PayoutRequestStatus.RETURNED, PayoutRequestStatus.CANCELED):
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
    Recalls an UNCLAIMED payout request.
    Strictly raises HTTP 409 Conflict if request is in any state other than UNCLAIMED.
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

    # Execute transition
    return await transition_payout_request_status(
        db=db,
        request=request,
        new_status=PayoutRequestStatus.CANCELED,
        decided_by=operator_id
    )
