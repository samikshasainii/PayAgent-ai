import logging
from typing import Dict, Any, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.webhook import WebhookEventModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.schemas.enums import PayoutRequestStatus
from src.services.paypal_client import paypal_client
from src.services.state_machine import transition_payout_request_status, ALLOWED_TRANSITIONS

logger = logging.getLogger("payagent.webhooks")

EVENT_TO_STATUS = {
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED": PayoutRequestStatus.PAID,
    "PAYMENT.PAYOUTS-ITEM.FAILED": PayoutRequestStatus.FAILED,
    "PAYMENT.PAYOUTS-ITEM.DENIED": PayoutRequestStatus.FAILED,
    "PAYMENT.PAYOUTS-ITEM.BLOCKED": PayoutRequestStatus.BLOCKED,
    "PAYMENT.PAYOUTS-ITEM.HELD": PayoutRequestStatus.ONHOLD,
    "PAYMENT.PAYOUTS-ITEM.UNCLAIMED": PayoutRequestStatus.UNCLAIMED,
    "PAYMENT.PAYOUTS-ITEM.CANCELED": PayoutRequestStatus.CANCELED,
    "PAYMENT.PAYOUTS-ITEM.RETURNED": PayoutRequestStatus.RETURNED,
    "PAYMENT.PAYOUTSBATCH.DENIED": PayoutRequestStatus.FAILED,
}

async def process_webhook_event(
    db: AsyncSession,
    event_payload: Dict[str, Any],
    headers: Dict[str, str],
    source: str = "paypal"
) -> Dict[str, Any]:
    """
    Processes incoming PayPal webhook events with cryptographic signature verification,
    event deduplication, and monotonic state machine progression.
    """
    event_id = event_payload.get("id")
    event_type = event_payload.get("event_type", "UNKNOWN")
    resource = event_payload.get("resource", {})
    resource_id = resource.get("payout_item_id") or resource.get("payout_batch_id") or event_payload.get("resource_type", "UNKNOWN")

    if not event_id:
        return {"status": "error", "message": "Missing event id"}

    # 1. Deduplication on event_id
    stmt = select(WebhookEventModel).where(WebhookEventModel.event_id == event_id)
    existing_event = (await db.execute(stmt)).scalar_one_or_none()
    if existing_event:
        logger.info(f"Duplicate webhook event ignored: {event_id}")
        return {"status": "duplicate", "event_id": event_id}

    # 2. Cryptographic signature verification
    verified = False
    if source == "simulated":
        verified = True
    else:
        verified = await paypal_client.verify_webhook_signature(headers, event_payload)

    # Persist webhook event for audit trail
    webhook_record = WebhookEventModel(
        event_id=event_id,
        event_type=event_type,
        resource_id=str(resource_id),
        verified=verified,
        source=source,
        raw=event_payload
    )
    db.add(webhook_record)
    await db.commit()

    if not verified:
        logger.warning(f"Webhook event signature verification failed: {event_id}")
        return {"status": "unverified", "event_id": event_id}

    # 3. Monotonic state transition
    target_status = EVENT_TO_STATUS.get(event_type)
    if not target_status:
        # Informational event (e.g. PAYMENT.PAYOUTSBATCH.PROCESSING, SUCCESS)
        return {"status": "recorded", "event_id": event_id, "event_type": event_type}

    # Match request via payout_item_id or payout_batch_id or sender_batch_id
    payout_item_id = resource.get("payout_item_id")
    payout_batch_id = resource.get("payout_batch_id")
    sender_batch_id = None
    if "sender_batch_header" in resource:
        sender_batch_id = resource["sender_batch_header"].get("sender_batch_id")

    req: Optional[PayoutRequestModel] = None

    if payout_item_id:
        stmt_item = select(PayoutItemModel).where(PayoutItemModel.payout_item_id == payout_item_id)
        item_model = (await db.execute(stmt_item)).scalar_one_or_none()
        if item_model:
            stmt_req = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == item_model.payout_request_id)
            req = (await db.execute(stmt_req)).scalar_one_or_none()

    if not req and payout_batch_id:
        stmt_req = select(PayoutRequestModel).where(PayoutRequestModel.payout_batch_id == payout_batch_id)
        req = (await db.execute(stmt_req)).scalar_one_or_none()

    if not req and sender_batch_id:
        stmt_req = select(PayoutRequestModel).where(PayoutRequestModel.sender_batch_id == sender_batch_id)
        req = (await db.execute(stmt_req)).scalar_one_or_none()

    if not req:
        logger.info(f"No local payout request matched for webhook {event_id} ({event_type})")
        return {"status": "no_matching_request", "event_id": event_id}

    # Check if transition is valid/monotonic
    allowed = ALLOWED_TRANSITIONS.get(req.status, set())
    if target_status not in allowed:
        logger.info(
            f"Ignoring out-of-order webhook event {event_id}: cannot transition {req.payout_request_id} from {req.status} to {target_status}"
        )
        return {"status": "ignored_out_of_order", "current_status": req.status.value, "target_status": target_status.value}

    # Update item record if present
    if payout_item_id:
        stmt_item = select(PayoutItemModel).where(PayoutItemModel.payout_item_id == payout_item_id)
        item_model = (await db.execute(stmt_item)).scalar_one_or_none()
        raw_status = resource.get("transaction_status")
        errors = resource.get("errors")
        if item_model:
            item_model.transaction_status = raw_status or item_model.transaction_status
            item_model.errors = errors
        else:
            db.add(PayoutItemModel(
                payout_item_id=payout_item_id,
                payout_request_id=req.payout_request_id,
                transaction_status=raw_status or "PENDING",
                errors=errors
            ))

    await transition_payout_request_status(db, req, target_status, decided_by="paypal_webhook")
    await db.commit()
    await db.refresh(req)

    return {
        "status": "processed",
        "event_id": event_id,
        "payout_request_id": req.payout_request_id,
        "new_status": req.status.value
    }
