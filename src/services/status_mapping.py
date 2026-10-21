"""
Shared PayPal item and webhook status mapping table to ensure strict parity
between polling reconciliation and webhook ingestion.
"""
from src.schemas.enums import PayoutRequestStatus

PAYPAL_ITEM_TO_REQUEST_STATUS = {
    "SUCCESS": PayoutRequestStatus.PAID,
    "FAILED": PayoutRequestStatus.FAILED,
    "DENIED": PayoutRequestStatus.FAILED,
    "BLOCKED": PayoutRequestStatus.BLOCKED,
    "ONHOLD": PayoutRequestStatus.ONHOLD,
    "HELD": PayoutRequestStatus.ONHOLD,
    "UNCLAIMED": PayoutRequestStatus.UNCLAIMED,
    "CANCELED": PayoutRequestStatus.CANCELED,
    "CANCELLED": PayoutRequestStatus.CANCELED,
    "RETURNED": PayoutRequestStatus.RETURNED,
    "REFUNDED": PayoutRequestStatus.RETURNED,
    "REVERSED": PayoutRequestStatus.RETURNED,
}

PAYPAL_EVENT_TO_REQUEST_STATUS = {
    "PAYMENT.PAYOUTS-ITEM.SUCCEEDED": PayoutRequestStatus.PAID,
    "PAYMENT.PAYOUTS-ITEM.FAILED": PayoutRequestStatus.FAILED,
    "PAYMENT.PAYOUTS-ITEM.DENIED": PayoutRequestStatus.FAILED,
    "PAYMENT.PAYOUTS-ITEM.BLOCKED": PayoutRequestStatus.BLOCKED,
    "PAYMENT.PAYOUTS-ITEM.HELD": PayoutRequestStatus.ONHOLD,
    "PAYMENT.PAYOUTS-ITEM.UNCLAIMED": PayoutRequestStatus.UNCLAIMED,
    "PAYMENT.PAYOUTS-ITEM.CANCELED": PayoutRequestStatus.CANCELED,
    "PAYMENT.PAYOUTS-ITEM.RETURNED": PayoutRequestStatus.RETURNED,
    "PAYMENT.PAYOUTS-ITEM.REFUNDED": PayoutRequestStatus.RETURNED,
    "PAYMENT.PAYOUTS-ITEM.REVERSED": PayoutRequestStatus.RETURNED,
    "PAYMENT.PAYOUTSBATCH.DENIED": PayoutRequestStatus.FAILED,
}
