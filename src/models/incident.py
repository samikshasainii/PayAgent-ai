from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime
from src.database import Base

def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)

class AgentIncidentModel(Base):
    __tablename__ = "agent_incidents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    agent_id = Column(String(64), nullable=False, index=True)
    reason_code = Column(String(64), nullable=False)
    payout_request_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=utc_now, nullable=False, index=True)
