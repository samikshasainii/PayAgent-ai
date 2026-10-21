from pydantic import BaseModel, Field, field_validator

class LLMVerdict(BaseModel):
    passed: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=500)

    @field_validator("reasoning", mode="before")
    @classmethod
    def sanitize_reasoning(cls, v: str) -> str:
        if isinstance(v, str) and len(v) > 500:
            return v[:497] + "..."
        return v
