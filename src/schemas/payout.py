from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, ConfigDict, field_validator
from src.schemas.enums import PayoutRequestStatus
from src.schemas.task import validate_currency_precision
from src.schemas.llm import LLMVerdict

MAX_DELIVERABLE_SIZE = 4096  # 4 KB per deliverable value

class PayoutRequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    amount: Decimal
    deliverables: Dict[str, str]

    @field_validator("amount")
    @classmethod
    def check_amount_precision(cls, v: Decimal) -> Decimal:
        return validate_currency_precision(v)

    @field_validator("deliverables")
    @classmethod
    def check_deliverable_sizes(cls, v: Dict[str, str]) -> Dict[str, str]:
        for k, val in v.items():
            if len(val.encode("utf-8")) > MAX_DELIVERABLE_SIZE:
                raise ValueError(f"Deliverable value for '{k}' exceeds maximum allowed size of 4KB")
        return v

class PayoutItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payout_item_id: str
    payout_request_id: str
    transaction_status: str
    transaction_id: Optional[str] = None
    errors: Optional[Dict[str, Any]] = None
    updated_at: datetime

class PayoutRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payout_request_id: str
    task_id: str
    agent_id: str
    amount: Decimal
    deliverables: Dict[str, str]
    artifact_sha256: str
    status: PayoutRequestStatus
    decision_reasons: List[str]
    llm_verdict: Optional[LLMVerdict] = None
    risk_score: Optional[float] = None
    sender_batch_id: str
    paypal_request_id: str
    payout_batch_id: Optional[str] = None
    created_at: datetime
    decided_by: Optional[str] = None
    items: List[PayoutItemOut] = []
