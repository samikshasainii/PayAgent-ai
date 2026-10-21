import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any
import httpx
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from src.database import async_session_factory
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.task import TaskModel
from src.schemas.enums import PayoutRequestStatus
from src.services.paypal_client import paypal_client
from src.services.state_machine import transition_payout_request_status

logger = logging.getLogger("payagent.poller")

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def extract_payout_batch_id_from_error(error_text: str) -> Optional[str]:
    """
    Parses PayPal error text to extract the original payout_batch_id on duplicate sender_batch_id errors.
    PayPal returns HATEOAS links: e.g. [{"href": ".../v1/payments/payouts/{batch_id}", "rel": "self"}]
    """
    try:
        data = json.loads(error_text)
        links = data.get("links", [])
        for link in links:
            href = link.get("href", "")
            if "/v1/payments/payouts/" in href:
                parts = href.split("/v1/payments/payouts/")
                if len(parts) > 1:
                    batch_id = parts[1].split("?")[0].split("/")[0].strip()
                    if batch_id:
                        return batch_id
        if "batch_id" in data:
            return data["batch_id"]
    except Exception:
        pass
    return None

# In-memory tracking for P4 unclaimed poll backoff
_unclaimed_poll_timestamps: Dict[str, float] = {}

async def apply_batch_reconciliation(
    db: AsyncSession,
    req: PayoutRequestModel,
    batch_data: Dict[str, Any]
) -> bool:
    """
    Applies PayPal batch/item status updates into the local database inside an active transaction.
    Returns True if a status transition occurred, False otherwise.
    """
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
        await db.flush()

    return transitioned

async def reconcile_request(
    db: AsyncSession,
    req: PayoutRequestModel
) -> bool:
    """
    Reconciles a single payout request against PayPal using its stored payout_batch_id.
    Returns True if a status transition occurred, False otherwise.
    """
    if not req.payout_batch_id:
        return False

    try:
        batch_data = await paypal_client.get_payout_batch(req.payout_batch_id)
    except Exception as exc:
        logger.warning(f"Poller failed to fetch batch {req.payout_batch_id}: {exc}")
        return False

    return await apply_batch_reconciliation(db, req, batch_data)

async def reconcile_all_pending(db: AsyncSession) -> int:
    """
    Finds all requests in SUBMITTED or UNCLAIMED status and reconciles against PayPal.
    Milestone 2 optimization: Fetches batch data from PayPal outside any DB transaction,
    then opens a short savepoint write transaction to apply status and item updates.
    """
    now = utc_now()
    stmt = (
        select(PayoutRequestModel)
        .where(
            or_(
                PayoutRequestModel.status == PayoutRequestStatus.SUBMITTED,
                PayoutRequestModel.status == PayoutRequestStatus.UNCLAIMED
            )
        )
    )
    result = await db.execute(stmt)
    pending_requests = result.scalars().all()

    updated_count = 0
    now_ts = time.time()

    for req in pending_requests:
        # P4: Backoff for unclaimed requests older than 10 minutes (600 seconds)
        if req.status == PayoutRequestStatus.UNCLAIMED:
            age = (now - req.created_at).total_seconds() if req.created_at else 0
            if age > 600:
                last_poll = _unclaimed_poll_timestamps.get(req.payout_request_id, 0.0)
                if (now_ts - last_poll) < 3600.0:
                    continue  # Hourly sweep backoff for long-running unclaimed items
                _unclaimed_poll_timestamps[req.payout_request_id] = now_ts

        # P3: Handle stuck SUBMITTED row with missing payout_batch_id
        if req.status == PayoutRequestStatus.SUBMITTED and not req.payout_batch_id:
            submitted_time = getattr(req, "updated_at", None) or req.created_at
            age_seconds = (now - submitted_time).total_seconds() if submitted_time else 0
            if age_seconds >= 60:
                stmt_task = select(TaskModel).where(TaskModel.task_id == req.task_id)
                task = (await db.execute(stmt_task)).scalar_one_or_none()
                if task:
                    try:
                        pp_resp = await paypal_client.create_single_payout(
                            sender_batch_id=req.sender_batch_id,
                            paypal_request_id=req.paypal_request_id,
                            recipient_email=task.allowed_recipient,
                            amount=req.amount,
                            currency=task.currency,
                            note=f"Task payout for {task.title}"
                        )
                        req.payout_batch_id = pp_resp["batch_header"]["payout_batch_id"]
                        await db.commit()
                    except httpx.HTTPStatusError as exc:
                        batch_id = extract_payout_batch_id_from_error(exc.response.text)
                        if batch_id:
                            req.payout_batch_id = batch_id
                            await db.commit()
                    except Exception as exc:
                        logger.warning(f"Error attempting recovery of stuck request {req.payout_request_id}: {exc}")

        if not req.payout_batch_id:
            continue

        # M2: Fetch from PayPal FIRST, outside any DB transaction or savepoint
        try:
            batch_data = await paypal_client.get_payout_batch(req.payout_batch_id)
        except Exception as exc:
            logger.error(f"Poller failed to fetch batch {req.payout_batch_id}: {exc}", exc_info=True)
            continue

        # Open short write transaction only for DB mutations
        try:
            async with db.begin_nested():
                if await apply_batch_reconciliation(db, req, batch_data):
                    updated_count += 1
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.error(f"Poller failed to reconcile request {req.payout_request_id}: {exc}", exc_info=True)
            continue

    return updated_count

class ReconciliationPoller:
    """
    Background worker running periodic reconciliation against PayPal Payouts.
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
