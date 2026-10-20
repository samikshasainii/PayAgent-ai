from sqlalchemy import Column, String, Text, Numeric, DateTime, JSON, Enum as SQLEnum
from src.database import Base
from src.schemas.enums import TaskStatus

class TaskModel(Base):
    __tablename__ = "tasks"

    task_id = Column(String(64), primary_key=True)
    title = Column(String(256), nullable=False)
    acceptance_criteria = Column(Text, nullable=False)
    budget = Column(Numeric(10, 2), nullable=False)
    currency = Column(String(3), default="USD", nullable=False)
    allowed_recipient = Column(String(256), nullable=False)
    assigned_agent_id = Column(String(64), nullable=False, index=True)
    required_deliverables = Column(JSON, nullable=False)
    deadline = Column(DateTime, nullable=False)
    status = Column(SQLEnum(TaskStatus), default=TaskStatus.OPEN, nullable=False)
