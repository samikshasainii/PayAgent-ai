from decimal import Decimal
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict
from src.schemas.enums import AgentStatus

class AgentCreate(BaseModel):
    agent_id: str
    name: str
    hourly_cap: Decimal = Field(default=Decimal("200.00"), gt=0)

class AgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    name: str
    status: AgentStatus
    hourly_cap: Decimal
    api_key: Optional[str] = None  # Populated only once upon creation

class AgentSummaryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    name: str
    status: AgentStatus
    hourly_cap: Decimal
    current_reserved: Decimal = Decimal("0.00")
    current_spent: Decimal = Decimal("0.00")
    current_burn: Decimal = Decimal("0.00")
