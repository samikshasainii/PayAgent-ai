from pydantic import BaseModel, Field

class LLMVerdict(BaseModel):
    passed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)
