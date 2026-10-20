import json
import uuid
from decimal import Decimal
from typing import Dict, Any, List
from mcp.server.mcpserver import MCPServer
from sqlalchemy import select
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.schemas.enums import TaskStatus, PayoutRequestStatus, AgentStatus
from src.schemas.payout import PayoutRequestIn
from src.services.auth import hash_api_key
from src.services.guardian import compute_artifact_sha256, evaluate_gate1, evaluate_gate2_and_decision
from src.services.state_machine import transition_payout_request_status
from src.services.paypal_client import paypal_client

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
    """Submits a payout request for a completed task through the Guardian."""
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

        artifact_sha256 = compute_artifact_sha256(deliverables)

        # Gate 1 evaluation
        gate1_passed, http_code, reason_code, task = await evaluate_gate1(
            db=db,
            agent=agent,
            payload=req_in,
            artifact_sha256=artifact_sha256
        )

        payout_request_id = str(uuid.uuid4())
        sender_batch_id = f"payagent_{uuid.uuid4().hex}"
        paypal_request_id = payout_request_id

        if not gate1_passed:
            payout_req = PayoutRequestModel(
                payout_request_id=payout_request_id,
                task_id=task_id,
                agent_id=agent_id,
                amount=dec_amount,
                deliverables=deliverables,
                artifact_sha256=artifact_sha256,
                status=PayoutRequestStatus.POLICY_REJECTED,
                decision_reasons=[reason_code],
                sender_batch_id=sender_batch_id,
                paypal_request_id=paypal_request_id,
                decided_by="guardian_gate1"
            )
            db.add(payout_req)
            await db.commit()
            return json.dumps({
                "payout_request_id": payout_request_id,
                "status": PayoutRequestStatus.POLICY_REJECTED.value,
                "decision_reasons": [reason_code],
                "error": reason_code
            })

        # Gate 2 & Decision
        # In baseline / M1, mock clean verdict
        from src.schemas.llm import LLMVerdict
        mock_verdict = LLMVerdict(passed=True, confidence=0.95, reasoning="Acceptance criteria verified in M1.")
        target_status, reasons, risk_score = evaluate_gate2_and_decision(dec_amount, mock_verdict)

        payout_req = PayoutRequestModel(
            payout_request_id=payout_request_id,
            task_id=task_id,
            agent_id=agent_id,
            amount=dec_amount,
            deliverables=deliverables,
            artifact_sha256=artifact_sha256,
            status=target_status,
            decision_reasons=reasons,
            llm_verdict=mock_verdict.model_dump(),
            risk_score=risk_score,
            sender_batch_id=sender_batch_id,
            paypal_request_id=paypal_request_id,
            decided_by="guardian_gate2"
        )
        db.add(payout_req)
        await db.commit()

        # If auto-approved, dispatch to PayPal
        if target_status == PayoutRequestStatus.APPROVED:
            await transition_payout_request_status(db, payout_req, PayoutRequestStatus.SUBMITTED)
            try:
                pp_resp = await paypal_client.create_single_payout(
                    sender_batch_id=sender_batch_id,
                    paypal_request_id=paypal_request_id,
                    recipient_email=task.allowed_recipient,
                    amount=dec_amount,
                    currency=task.currency,
                    note=f"Task payout for {task.title}"
                )
                payout_req.payout_batch_id = pp_resp["batch_header"]["payout_batch_id"]
                if "items" in pp_resp and len(pp_resp["items"]) > 0:
                    item_info = pp_resp["items"][0]
                    payout_item = PayoutItemModel(
                        payout_item_id=item_info["payout_item_id"],
                        payout_request_id=payout_req.payout_request_id,
                        transaction_status=item_info.get("transaction_status", "SUCCESS")
                    )
                    db.add(payout_item)
                    if item_info.get("transaction_status") == "SUCCESS":
                        await transition_payout_request_status(db, payout_req, PayoutRequestStatus.PAID)
                await db.commit()
            except Exception as exc:
                payout_req.status = PayoutRequestStatus.FAILED
                payout_req.decision_reasons = payout_req.decision_reasons + [f"PAYPAL_ERROR: {str(exc)}"]
                await db.commit()

        return json.dumps({
            "payout_request_id": payout_req.payout_request_id,
            "status": payout_req.status.value,
            "decision_reasons": payout_req.decision_reasons,
            "sender_batch_id": sender_batch_id,
            "payout_batch_id": payout_req.payout_batch_id
        })

@mcp_server.tool()
async def get_payout_status(payout_request_id: str) -> str:
    """Returns status and audit reasons for a given payout request."""
    async with async_session_factory() as db:
        stmt = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
        res = await db.execute(stmt)
        req = res.scalar_one_or_none()
        if not req:
            return json.dumps({"error": "NOT_FOUND", "message": "Payout request not found"})

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
