import hashlib
import secrets
from typing import Optional
from fastapi import Header, HTTPException, status, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.database import get_db
from src.models.agent import AgentModel
from src.schemas.enums import AgentStatus
from src.config import settings

def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()

def generate_api_key(agent_id: str) -> str:
    random_part = secrets.token_urlsafe(32)
    return f"pa_{agent_id}_{random_part}"

async def get_authenticated_agent(
    authorization: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
) -> AgentModel:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    elif x_agent_key:
        token = x_agent_key.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_INVALID", "message": "Missing Bearer token or X-Agent-Key header"}
        )

    key_hash = hash_api_key(token)
    stmt = select(AgentModel).where(AgentModel.api_key_hash == key_hash)
    result = await db.execute(stmt)
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_INVALID", "message": "Invalid agent API key"}
        )

    return agent

def operator_token_matches(token: Optional[str]) -> bool:
    # compare_digest raises TypeError on non-ASCII str; compare UTF-8 bytes instead
    return bool(token) and secrets.compare_digest(token.encode("utf-8"), settings.OPERATOR_TOKEN.encode("utf-8"))

def require_operator_token(
    authorization: Optional[str] = Header(None),
    x_operator_token: Optional[str] = Header(None)
):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    elif x_operator_token:
        token = x_operator_token.strip()

    if not operator_token_matches(token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "OPERATOR_AUTH_INVALID", "message": "Invalid or missing operator authorization"}
        )
    return True

async def get_authenticated_caller(
    authorization: Optional[str] = Header(None),
    x_agent_key: Optional[str] = Header(None),
    x_operator_token: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db)
):
    """Authenticates caller as either an Operator or an Agent."""
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    elif x_operator_token:
        token = x_operator_token.strip()
    elif x_agent_key:
        token = x_agent_key.strip()

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error": "AUTH_INVALID", "message": "Authentication required"}
        )

    # Check if operator
    if operator_token_matches(token):
        return {"role": "operator", "id": "operator"}

    # Check if agent
    key_hash = hash_api_key(token)
    stmt = select(AgentModel).where(AgentModel.api_key_hash == key_hash)
    result = await db.execute(stmt)
    agent = result.scalar_one_or_none()
    if agent:
        return {"role": "agent", "id": agent.agent_id, "agent": agent}

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail={"error": "AUTH_INVALID", "message": "Invalid credentials"}
    )

