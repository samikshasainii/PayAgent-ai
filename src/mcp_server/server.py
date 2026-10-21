import json
from decimal import Decimal
from typing import Dict, Any, List, Optional
from mcp.server.mcpserver import MCPServer
from sqlalchemy import select
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.schemas.enums import TaskStatus, AgentStatus
from src.schemas.payout import PayoutRequestIn
from src.services.auth import hash_api_key
from src.services.payout_service import process_payout_submission

mcp_server = MCPServer("PayAgent Guardian MCP")

@mcp_server.tool()
async def list_my_tasks(agent_id: str, api_key: str) -> str:
    """Lists OPEN tasks assigned to the authenticated calling agent."""
    key_hash = hash_api_key(api_key)
    async with async_session_factory() as db:
        # Authenticate
        stmt = select(AgentModel).where(AgentModel.agent_id == agent_id, AgentModel.api_key_hash == key_hash)
        res = await db.execute(stmt)
        agent = res.scalar_one_or_none()
        if not agent:
            return json.dumps({"error": "AUTH_INVALID", "message": "Invalid agent credentials"})

        if agent.status == AgentStatus.QUARANTINED:
            return json.dumps({"error": "AGENT_QUARANTINED", "message": "Agent is currently quarantined"})

        # Query open tasks
        stmt = select(TaskModel).where(
            TaskModel.assigned_agent_id == agent_id,
            TaskModel.status == TaskStatus.OPEN
        )
        res = await db.execute(stmt)
        tasks = res.scalars().all()

        output = [
            {
                "task_id": t.task_id,
                "title": t.title,
                "acceptance_criteria": t.acceptance_criteria,
                "budget": str(t.budget),
                "currency": t.currency,
                "required_deliverables": t.required_deliverables,
                "deadline": t.deadline.isoformat(),
            }
            for t in tasks
        ]
        return json.dumps({"tasks": output})

@mcp_server.tool()
async def submit_task_payout(agent_id: str, api_key: str, task_id: str, amount: str, deliverables: Dict[str, str]) -> str:
    """
    Submits a payout request for a completed task through the Guardian.
    Returns immediately with status RECEIVED once Gate 1 passes; the Gate 2 proof review runs in the
    background. Poll get_payout_status until the status is no longer RECEIVED.
    """
    key_hash = hash_api_key(api_key)
    try:
        dec_amount = Decimal(amount)
        req_in = PayoutRequestIn(task_id=task_id, amount=dec_amount, deliverables=deliverables)
    except Exception as e:
        return json.dumps({"error": "INVALID_INPUT", "message": str(e)})

    async with async_session_factory() as db:
        stmt = select(AgentModel).where(AgentModel.agent_id == agent_id, AgentModel.api_key_hash == key_hash)
        res = await db.execute(stmt)
        agent = res.scalar_one_or_none()
        if not agent:
            return json.dumps({"error": "AUTH_INVALID", "message": "Invalid agent credentials"})

        passed, http_code, msg, payout_req = await process_payout_submission(db, agent, req_in)
        if not passed:
            return json.dumps({
                "payout_request_id": payout_req.payout_request_id,
                "status": payout_req.status.value,
                "decision_reasons": payout_req.decision_reasons,
                "error": msg
            })

        return json.dumps({
            "payout_request_id": payout_req.payout_request_id,
            "status": payout_req.status.value,
            "decision_reasons": payout_req.decision_reasons,
            "sender_batch_id": payout_req.sender_batch_id,
            "payout_batch_id": payout_req.payout_batch_id,
            "next": "Gate 2 review in progress; poll get_payout_status with this payout_request_id"
        })

@mcp_server.tool()
async def get_payout_status(payout_request_id: str, agent_id: str, api_key: str) -> str:
    """Returns status and audit reasons for a given payout request. Requires valid agent credentials."""
    if not agent_id or not api_key:
        return json.dumps({"error": "AUTH_INVALID", "message": "agent_id and api_key are required"})

    key_hash = hash_api_key(api_key)
    async with async_session_factory() as db:
        stmt = select(AgentModel).where(AgentModel.agent_id == agent_id, AgentModel.api_key_hash == key_hash)
        res = await db.execute(stmt)
        agent = res.scalar_one_or_none()
        if not agent:
            return json.dumps({"error": "AUTH_INVALID", "message": "Invalid agent credentials"})

        stmt = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
        res = await db.execute(stmt)
        req = res.scalar_one_or_none()
        if not req:
            return json.dumps({"error": "NOT_FOUND", "message": "Payout request not found"})

        if req.agent_id != agent_id:
            return json.dumps({"error": "AUTH_UNAUTHORIZED", "message": "Not authorized to view this payout request"})

        return json.dumps({
            "payout_request_id": req.payout_request_id,
            "task_id": req.task_id,
            "agent_id": req.agent_id,
            "amount": str(req.amount),
            "status": req.status.value,
            "decision_reasons": req.decision_reasons,
            "sender_batch_id": req.sender_batch_id,
            "payout_batch_id": req.payout_batch_id
        })
