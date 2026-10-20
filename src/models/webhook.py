from datetime import datetime, timezone
from sqlalchemy import Column, String, Boolean, JSON, DateTime
from src.database import Base

def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class WebhookEventModel(Base):
    __tablename__ = "webhook_events"

    event_id = Column(String(128), primary_key=True)
    event_type = Column(String(128), nullable=False)
    resource_id = Column(String(128), nullable=False)
    verified = Column(Boolean, default=False, nullable=False)
    source = Column(String(32), default="paypal", nullable=False)  # "paypal" or "simulated"
    raw = Column(JSON, nullable=False)
    received_at = Column(DateTime, default=utc_now, nullable=False)
