from decimal import Decimal
from sqlalchemy import Column, String, Numeric
from src.database import Base

class BudgetWindowModel(Base):
    __tablename__ = "budget_windows"

    # Composite primary key (agent_id, window_start)
    # window_start is formatted as "YYYY-MM-DD-HH" (hour bucket)
    agent_id = Column(String(64), primary_key=True)
    window_start = Column(String(16), primary_key=True)
    reserved = Column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
    spent = Column(Numeric(10, 2), default=Decimal("0.00"), nullable=False)
