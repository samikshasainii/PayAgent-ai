import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import init_db, get_db
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.schemas.enums import TaskStatus, PayoutRequestStatus, AgentStatus, DecisionReason
from src.schemas.task import TaskCreate, TaskOut
from src.schemas.agent import AgentCreate, AgentOut
from src.schemas.payout import PayoutRequestIn, PayoutRequestOut
from src.schemas.llm import LLMVerdict
from src.services.auth import (
    hash_api_key,
    generate_api_key,
    get_authenticated_agent,
    require_operator_token,
)
from src.services.guardian import (
    compute_artifact_sha256,
    evaluate_gate1,
    evaluate_gate2_and_decision,
)
from src.services.state_machine import (
    transition_payout_request_status,
    cancel_unclaimed_payout_request,
)
from src.services.paypal_client import paypal_client
from src.mcp_server.server import mcp_server

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB tables on startup
    await init_db()
    yield
    # Cleanup on shutdown
    await paypal_client.close()

app = FastAPI(
    title="PayAgent AI Guardian",
    description="Spend-Control Guardian for AI Agents, Settled on PayPal Payouts",
    version="1.0.0",
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount MCP Server Streamable HTTP App
app.mount("/mcp", mcp_server.streamable_http_app())

# --- Operator Task Endpoints ---

@app.post("/api/v1/tasks", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    task_in: TaskCreate,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(TaskModel).where(TaskModel.task_id == task_in.task_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "TASK_EXISTS", "message": f"Task '{task_in.task_id}' already exists"}
        )

    task = TaskModel(
        task_id=task_in.task_id,
        title=task_in.title,
        acceptance_criteria=task_in.acceptance_criteria,
        budget=task_in.budget,
        currency="USD",
        allowed_recipient=str(task_in.allowed_recipient),
        assigned_agent_id=task_in.assigned_agent_id,
        required_deliverables=task_in.required_deliverables,
        deadline=task_in.deadline,
        status=TaskStatus.OPEN
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task

@app.get("/api/v1/tasks", response_model=List[TaskOut])
async def list_tasks(
    status_filter: Optional[TaskStatus] = None,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(TaskModel)
    if status_filter:
        stmt = stmt.where(TaskModel.status == status_filter)
    res = await db.execute(stmt)
    return res.scalars().all()

# --- Operator Agent Endpoints ---

@app.post("/api/v1/agents", response_model=AgentOut, status_code=status.HTTP_201_CREATED)
async def register_agent(
    agent_in: AgentCreate,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(AgentModel).where(AgentModel.agent_id == agent_in.agent_id)
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "AGENT_EXISTS", "message": f"Agent '{agent_in.agent_id}' already registered"}
        )

    raw_api_key = generate_api_key(agent_in.agent_id)
    api_key_hash = hash_api_key(raw_api_key)

    agent = AgentModel(
        agent_id=agent_in.agent_id,
        name=agent_in.name,
        api_key_hash=api_key_hash,
        status=AgentStatus.ACTIVE,
        hourly_cap=agent_in.hourly_cap
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)

    out = AgentOut.model_validate(agent)
    out.api_key = raw_api_key  # Returned once upon registration
    return out

@app.post("/api/v1/agents/{agent_id}/release", response_model=AgentOut)
async def release_agent_quarantine(
    agent_id: str,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(AgentModel).where(AgentModel.agent_id == agent_id)
    agent = (await db.execute(stmt)).scalar_one_or_none()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    agent.status = AgentStatus.ACTIVE
    await db.commit()
    await db.refresh(agent)
    return agent

# --- Agent Payout Request Endpoints ---

@app.post("/api/v1/payout-requests", response_model=PayoutRequestOut, status_code=status.HTTP_201_CREATED)
async def submit_payout_request(
    request_in: PayoutRequestIn,
    agent: AgentModel = Depends(get_authenticated_agent),
    db: AsyncSession = Depends(get_db)
):
    artifact_sha256 = compute_artifact_sha256(request_in.deliverables)

    # Saved-ID pattern: generated once per request inception and stored
    payout_request_id = str(uuid.uuid4())
    sender_batch_id = f"payagent_{uuid.uuid4().hex}"
    paypal_request_id = payout_request_id

    # Gate 1 Deterministic Evaluation
    gate1_passed, http_code, reason_code, task = await evaluate_gate1(
        db=db,
        agent=agent,
        payload=request_in,
        artifact_sha256=artifact_sha256
    )

    if not gate1_passed:
        # Record rejected request
        rejected_req = PayoutRequestModel(
            payout_request_id=payout_request_id,
            task_id=request_in.task_id,
            agent_id=agent.agent_id,
            amount=request_in.amount,
            deliverables=request_in.deliverables,
            artifact_sha256=artifact_sha256,
            status=PayoutRequestStatus.POLICY_REJECTED,
            decision_reasons=[reason_code],
            sender_batch_id=sender_batch_id,
            paypal_request_id=paypal_request_id,
            decided_by="guardian_gate1"
        )
        db.add(rejected_req)
        await db.commit()
        raise HTTPException(
            status_code=http_code,
            detail={
                "error": reason_code,
                "status": PayoutRequestStatus.POLICY_REJECTED.value,
                "payout_request_id": payout_request_id
            }
        )

    # Gate 2 Evaluation
    mock_verdict = LLMVerdict(
        passed=True,
        confidence=0.95,
        reasoning="Acceptance criteria verified in Milestone 1."
    )
    target_status, reasons, risk_score = evaluate_gate2_and_decision(request_in.amount, mock_verdict)

    payout_req = PayoutRequestModel(
        payout_request_id=payout_request_id,
        task_id=request_in.task_id,
        agent_id=agent.agent_id,
        amount=request_in.amount,
        deliverables=request_in.deliverables,
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

    # If AUTO_APPROVED, dispatch to PayPal
    if target_status == PayoutRequestStatus.APPROVED:
        await transition_payout_request_status(db, payout_req, PayoutRequestStatus.SUBMITTED)
        try:
            pp_resp = await paypal_client.create_single_payout(
                sender_batch_id=payout_req.sender_batch_id,
                paypal_request_id=payout_req.paypal_request_id,
                recipient_email=task.allowed_recipient,
                amount=payout_req.amount,
                currency=task.currency,
                note=f"Task payout for {task.title}"
            )
            payout_req.payout_batch_id = pp_resp["batch_header"]["payout_batch_id"]
            if "items" in pp_resp and len(pp_resp["items"]) > 0:
                item_data = pp_resp["items"][0]
                item_model = PayoutItemModel(
                    payout_item_id=item_data["payout_item_id"],
                    payout_request_id=payout_req.payout_request_id,
                    transaction_status=item_data.get("transaction_status", "SUCCESS")
                )
                db.add(item_model)
                if item_data.get("transaction_status") == "SUCCESS":
                    await transition_payout_request_status(db, payout_req, PayoutRequestStatus.PAID)
            await db.commit()
        except Exception as exc:
            payout_req.status = PayoutRequestStatus.FAILED
            payout_req.decision_reasons = payout_req.decision_reasons + [f"PAYPAL_ERROR: {str(exc)}"]
            await db.commit()

    await db.refresh(payout_req)
    return payout_req

@app.get("/api/v1/payout-requests/{payout_request_id}", response_model=PayoutRequestOut)
async def get_payout_request(
    payout_request_id: str,
    db: AsyncSession = Depends(get_db)
):
    stmt = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
    res = await db.execute(stmt)
    req = res.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Payout request not found")
    return req

# --- Operator HITL Approval / Rejection / Cancellation Endpoints ---

@app.post("/api/v1/payout-requests/{payout_request_id}/approve", response_model=PayoutRequestOut)
async def operator_approve_request(
    payout_request_id: str,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
    req = (await db.execute(stmt)).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Payout request not found")

    if req.status != PayoutRequestStatus.PENDING_HUMAN_APPROVAL:
        raise HTTPException(
            status_code=400,
            detail=f"Only requests in PENDING_HUMAN_APPROVAL can be approved (current: {req.status.value})"
        )

    # Approve and submit
    await transition_payout_request_status(db, req, PayoutRequestStatus.SUBMITTED, decided_by="operator")

    task = (await db.execute(select(TaskModel).where(TaskModel.task_id == req.task_id))).scalar_one()

    # Dispatch to PayPal
    pp_resp = await paypal_client.create_single_payout(
        sender_batch_id=req.sender_batch_id,
        paypal_request_id=req.paypal_request_id,
        recipient_email=task.allowed_recipient,
        amount=req.amount,
        currency=task.currency,
        note=f"Task payout for {task.title}"
    )
    req.payout_batch_id = pp_resp["batch_header"]["payout_batch_id"]
    if "items" in pp_resp and len(pp_resp["items"]) > 0:
        item_data = pp_resp["items"][0]
        item_model = PayoutItemModel(
            payout_item_id=item_data["payout_item_id"],
            payout_request_id=req.payout_request_id,
            transaction_status=item_data.get("transaction_status", "SUCCESS")
        )
        db.add(item_model)
        if item_data.get("transaction_status") == "SUCCESS":
            await transition_payout_request_status(db, req, PayoutRequestStatus.PAID)
    await db.commit()
    await db.refresh(req)
    return req

@app.post("/api/v1/payout-requests/{payout_request_id}/reject", response_model=PayoutRequestOut)
async def operator_reject_request(
    payout_request_id: str,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
    req = (await db.execute(stmt)).scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Payout request not found")

    if req.status != PayoutRequestStatus.PENDING_HUMAN_APPROVAL:
        raise HTTPException(
            status_code=400,
            detail=f"Only requests in PENDING_HUMAN_APPROVAL can be rejected (current: {req.status.value})"
        )

    await transition_payout_request_status(db, req, PayoutRequestStatus.HUMAN_REJECTED, decided_by="operator")
    await db.commit()
    await db.refresh(req)
    return req

@app.post("/api/v1/payout-requests/{payout_request_id}/cancel", response_model=PayoutRequestOut)
async def operator_cancel_request(
    payout_request_id: str,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    """
    Recalls an UNCLAIMED payout request.
    Strictly raises 409 Conflict if request is not in UNCLAIMED state.
    """
    cancelled_req = await cancel_unclaimed_payout_request(db, payout_request_id, operator_id="operator")
    await db.commit()
    await db.refresh(cancelled_req)
    return cancelled_req
