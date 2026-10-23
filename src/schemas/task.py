from datetime import datetime
from decimal import Decimal
from typing import List
from pydantic import BaseModel, EmailStr, ConfigDict, field_validator, Field
from src.schemas.enums import TaskStatus

def validate_currency_precision(val: Decimal) -> Decimal:
    if val <= 0:
        raise ValueError("Budget/amount must be strictly greater than 0")
    # Verify at most 2 decimal places without rounding
    exponent = val.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -2:
        raise ValueError("Amount has more than 2 decimal places; precision exceeding cents is rejected")
    return val

class TaskBase(BaseModel):
    task_id: str = Field(..., pattern=r"^[A-Za-z0-9._:-]{1,64}$")
    title: str
    acceptance_criteria: str
    budget: Decimal
    allowed_recipient: EmailStr
    assigned_agent_id: str
    required_deliverables: List[str]
    deadline: datetime

    @field_validator("budget")
    @classmethod
    def check_budget_precision(cls, v: Decimal) -> Decimal:
        return validate_currency_precision(v)

class TaskCreate(TaskBase):
    pass

class TaskOut(TaskBase):
    model_config = ConfigDict(from_attributes=True)
    currency: str = "USD"
    status: TaskStatus
