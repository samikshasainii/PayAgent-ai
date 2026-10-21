from datetime import datetime, timezone

def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)
from sqlalchemy import Column, String, Numeric, DateTime, JSON, Float, ForeignKey, Enum as SQLEnum
from sqlalchemy.orm import relationship
from src.database import Base
from src.schemas.enums import PayoutRequestStatus

class PayoutRequestModel(Base):
    __tablename__ = "payout_requests"

    payout_request_id = Column(String(64), primary_key=True)
    task_id = Column(String(64), nullable=False, index=True)
    agent_id = Column(String(64), nullable=False, index=True)
    amount = Column(Numeric(10, 2), nullable=False)
    deliverables = Column(JSON, nullable=False)
    artifact_sha256 = Column(String(64), nullable=False, index=True)
    status = Column(SQLEnum(PayoutRequestStatus), default=PayoutRequestStatus.RECEIVED, nullable=False)
    decision_reasons = Column(JSON, default=list, nullable=False)
    llm_verdict = Column(JSON, nullable=True)
    risk_score = Column(Float, nullable=True)
    sender_batch_id = Column(String(64), unique=True, nullable=False, index=True)
    paypal_request_id = Column(String(64), unique=True, nullable=False, index=True)
    payout_batch_id = Column(String(64), nullable=True, index=True)
    window_start = Column(String(32), nullable=True, index=True)
    created_at = Column(DateTime, default=utc_now, nullable=False)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)
    decided_by = Column(String(64), nullable=True)

    items = relationship("PayoutItemModel", back_populates="request", cascade="all, delete-orphan", lazy="selectin")

class PayoutItemModel(Base):
    __tablename__ = "payout_items"

    payout_item_id = Column(String(64), primary_key=True)
    payout_request_id = Column(String(64), ForeignKey("payout_requests.payout_request_id"), nullable=False, index=True)
    transaction_status = Column(String(64), nullable=False)
    transaction_id = Column(String(64), nullable=True)
    errors = Column(JSON, nullable=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now, nullable=False)

    request = relationship("PayoutRequestModel", back_populates="items")
