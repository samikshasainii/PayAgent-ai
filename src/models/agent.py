from decimal import Decimal
from sqlalchemy import Column, String, Numeric, Enum as SQLEnum
from src.database import Base
from src.schemas.enums import AgentStatus

class AgentModel(Base):
    __tablename__ = "agents"

    agent_id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    api_key_hash = Column(String(64), nullable=False, index=True)
    status = Column(SQLEnum(AgentStatus), default=AgentStatus.ACTIVE, nullable=False)
    hourly_cap = Column(Numeric(10, 2), default=Decimal("200.00"), nullable=False)
