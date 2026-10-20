import asyncio
import logging
from typing import Optional, List
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_factory
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.task import TaskModel
from src.schemas.enums import PayoutRequestStatus
from src.services.paypal_client import paypal_client
from src.services.state_machine import transition_payout_request_status

logger = logging.getLogger("payagent.poller")

async def reconcile_request(
    db: AsyncSession,
    req: PayoutRequestModel
) -> bool:
    """
    Reconciles a single payout request against PayPal.
    Returns True if a status transition occurred, False otherwise.
    """
    if not req.payout_batch_id:
        # If batch_id was lost due to network drop during dispatch,
        # we can re-POST or skip until manual intervention.
        return False

    try:
        batch_data = await paypal_client.get_payout_batch(req.payout_batch_id)
    except Exception as exc:
        logger.warning(f"Poller failed to fetch batch {req.payout_batch_id}: {exc}")
        return False

    items = batch_data.get("items", [])
    if not items:
        # If no item list yet (batch in PROCESSING state), wait for next cycle
        return False

    item_data = items[0]
    raw_item_status = item_data.get("transaction_status")
    item_id = item_data.get("payout_item_id")
    errors = item_data.get("errors")

    # Update or insert PayoutItemModel
    stmt = select(PayoutItemModel).where(PayoutItemModel.payout_item_id == item_id)
    existing_item = (await db.execute(stmt)).scalar_one_or_none()
    if existing_item:
        existing_item.transaction_status = raw_item_status or existing_item.transaction_status
        existing_item.errors = errors
    else:
        new_item = PayoutItemModel(
            payout_item_id=item_id,
            payout_request_id=req.payout_request_id,
            transaction_status=raw_item_status or "PENDING",
            errors=errors
        )
        db.add(new_item)

    transitioned = False

    # Map raw PayPal item status to PayoutRequestStatus
    if raw_item_status == "SUCCESS":
        if req.status != PayoutRequestStatus.PAID:
            await transition_payout_request_status(db, req, PayoutRequestStatus.PAID)
            transitioned = True
    elif raw_item_status in ("FAILED", "BLOCKED", "DENIED"):
        if req.status != PayoutRequestStatus.FAILED:
            await transition_payout_request_status(db, req, PayoutRequestStatus.FAILED)
            transitioned = True
    elif raw_item_status == "UNCLAIMED":
        if req.status == PayoutRequestStatus.SUBMITTED:
            await transition_payout_request_status(db, req, PayoutRequestStatus.UNCLAIMED)
            transitioned = True
    elif raw_item_status == "CANCELED":
        # Handles cancel timeouts (e.g. after a 504 where PayPal cancelled but local DB is UNCLAIMED)
        if req.status != PayoutRequestStatus.CANCELED:
            await transition_payout_request_status(db, req, PayoutRequestStatus.CANCELED)
            transitioned = True
    elif raw_item_status == "RETURNED":
        # Handles automatic 30-day PayPal returns of unclaimed funds
        if req.status != PayoutRequestStatus.RETURNED:
            await transition_payout_request_status(db, req, PayoutRequestStatus.RETURNED)
            transitioned = True

    if transitioned:
        await db.commit()
        await db.refresh(req)

    return transitioned

async def reconcile_all_pending(db: AsyncSession) -> int:
    """
    Finds all requests in SUBMITTED or UNCLAIMED status with a payout_batch_id
    and reconciles their status against PayPal.
    """
    stmt = (
        select(PayoutRequestModel)
        .where(
            or_(
                PayoutRequestModel.status == PayoutRequestStatus.SUBMITTED,
                PayoutRequestModel.status == PayoutRequestStatus.UNCLAIMED
            ),
            PayoutRequestModel.payout_batch_id.isnot(None)
        )
    )
    result = await db.execute(stmt)
    pending_requests = result.scalars().all()

    updated_count = 0
    for req in pending_requests:
        if await reconcile_request(db, req):
            updated_count += 1

    return updated_count

class ReconciliationPoller:
    """
    Background worker that runs periodic reconciliation against PayPal Payouts.
    Essential for local sandbox judging and handling ambiguous states.
    """
    def __init__(self, interval_seconds: float = 5.0):
        self.interval = interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def _run_loop(self):
        while self._running:
            try:
                async with async_session_factory() as db:
                    count = await reconcile_all_pending(db)
                    if count > 0:
                        logger.info(f"Reconciliation poller updated {count} pending requests.")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in reconciliation poller: {exc}", exc_info=True)

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Reconciliation poller started.")

    async def stop(self):
        if self._running:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            logger.info("Reconciliation poller stopped.")

poller = ReconciliationPoller(interval_seconds=5.0)
