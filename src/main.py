import os
import uuid
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import List, Optional
from fastapi import FastAPI, Depends, HTTPException, status, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.routing import Route
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.database import init_db, get_db
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.schemas.enums import TaskStatus, PayoutRequestStatus, AgentStatus
from src.schemas.task import TaskCreate, TaskOut
from src.schemas.agent import AgentCreate, AgentOut
from src.schemas.payout import PayoutRequestIn, PayoutRequestOut
from src.services.auth import (
    hash_api_key,
    generate_api_key,
    get_authenticated_agent,
    get_authenticated_caller,
    require_operator_token,
)
from src.services.state_machine import (
    transition_payout_request_status,
    cancel_unclaimed_payout_request,
)
from src.services.paypal_client import paypal_client
from src.services.payout_service import process_payout_submission, dispatch_payout
from src.services.poller import poller, reconcile_all_pending
from src.services.webhooks import process_webhook_event
from src.mcp_server.server import mcp_server
from mcp.server.transport_security import TransportSecuritySettings

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Production security guard
    if settings.APP_ENV == "production":
        insecure_tokens = {"payagent-operator-demo-secret", "change-me-demo-operator-token"}
        if not settings.OPERATOR_TOKEN or settings.OPERATOR_TOKEN in insecure_tokens:
            raise ValueError("OPERATOR_TOKEN must be securely configured in production environment.")

    # Initialize DB tables on startup
    await init_db()

    # Start reconciliation poller
    poller.start()

    # Run Streamable HTTP MCP session manager using public property
    mgr = mcp_server.session_manager
    async with mgr.run():
        yield

    # Cleanup on shutdown
    await poller.stop()
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

# Register MCP Server Streamable HTTP Routes with DNS rebinding protection and explicit host configuration
mcp_allowed_hosts = [
    "127.0.0.1:*",
    "localhost:*",
    "localhost",
    "127.0.0.1",
    "[::1]:*",
    "test",
    "test:*",
    "0.0.0.0:*",
    "0.0.0.0",
    f"{settings.APP_HOST}:*",
    f"{settings.APP_HOST}",
]
mcp_allowed_origins = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://[::1]:*",
    "http://test",
    "http://0.0.0.0:*",
]
if settings.PUBLIC_HOST:
    clean_host = settings.PUBLIC_HOST.strip().replace("https://", "").replace("http://", "").split("/")[0]
    mcp_allowed_hosts.extend([clean_host, f"{clean_host}:*"])
    mcp_allowed_origins.extend([f"https://{clean_host}", f"https://{clean_host}:*", f"http://{clean_host}"])

mcp_sec = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=mcp_allowed_hosts,
    allowed_origins=mcp_allowed_origins
)
mcp_http_app = mcp_server.streamable_http_app(streamable_http_path="/mcp", transport_security=mcp_sec)
for route in mcp_http_app.routes:
    app.routes.append(route)
    if isinstance(route, Route) and route.path == "/mcp":
        app.routes.append(Route("/mcp/", endpoint=route.endpoint))

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
    passed, http_code, reason_code, payout_req = await process_payout_submission(
        db=db,
        agent=agent,
        request_in=request_in
    )

    if not passed:
        raise HTTPException(
            status_code=http_code,
            detail={
                "error": reason_code,
                "status": PayoutRequestStatus.POLICY_REJECTED.value,
                "payout_request_id": payout_req.payout_request_id
            }
        )

    return payout_req

@app.get("/api/v1/payout-requests", response_model=List[PayoutRequestOut])
async def list_payout_requests(
    status_filter: Optional[PayoutRequestStatus] = None,
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(PayoutRequestModel).order_by(PayoutRequestModel.created_at.desc())
    if status_filter:
        stmt = stmt.where(PayoutRequestModel.status == status_filter)
    res = await db.execute(stmt)
    return res.scalars().all()

@app.get("/api/v1/payout-requests/{payout_request_id}", response_model=PayoutRequestOut)
async def get_payout_request(
    payout_request_id: str,
    caller: dict = Depends(get_authenticated_caller),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
    res = await db.execute(stmt)
    req = res.scalar_one_or_none()
    if not req:
        raise HTTPException(status_code=404, detail="Payout request not found")

    if caller["role"] == "agent" and req.agent_id != caller["id"]:
        raise HTTPException(status_code=403, detail="Not authorized to view this payout request")

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
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "INVALID_STATE", "message": f"Only requests in PENDING_HUMAN_APPROVAL can be approved (current: {req.status.value})"}
        )

    # Approve and submit
    await transition_payout_request_status(db, req, PayoutRequestStatus.SUBMITTED, decided_by="operator")

    task = (await db.execute(select(TaskModel).where(TaskModel.task_id == req.task_id))).scalar_one()

    # Dispatch to PayPal via unified dispatcher
    await dispatch_payout(db, req, task)

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
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "INVALID_STATE", "message": f"Only requests in PENDING_HUMAN_APPROVAL can be rejected (current: {req.status.value})"}
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
    Recalls an UNCLAIMED payout request by calling PayPal's cancel endpoint first,
    then transitioning the state and releasing budget reservation.
    Strictly raises 409 Conflict if request is not in UNCLAIMED state or PayPal refuses.
    """
    cancelled_req = await cancel_unclaimed_payout_request(db, payout_request_id, operator_id="operator")
    await db.commit()
    await db.refresh(cancelled_req)
    return cancelled_req

@app.post("/api/v1/reconciliation/poll")
async def trigger_reconciliation(
    _: bool = Depends(require_operator_token),
    db: AsyncSession = Depends(get_db)
):
    """
    Manually triggers a reconciliation cycle against PayPal Payouts.
    Scans for SUBMITTED and UNCLAIMED requests and updates their terminal states.
    """
    updated_count = await reconcile_all_pending(db)
    return {
        "status": "success",
        "reconciled_count": updated_count
    }

# --- PayPal Webhooks ---

@app.post("/api/v1/webhooks/paypal")
async def paypal_webhook_receiver(
    request: Request,
    db: AsyncSession = Depends(get_db)
):
    """
    Receives live PayPal webhooks, verifies SHA256withRSA signature,
    and transitions payout request FSM monotonically.
    """
    headers = dict(request.headers)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    res = await process_webhook_event(db, body, headers, source="paypal")
    return res

if settings.DEMO_MODE and settings.APP_ENV != "production":
    @app.post("/api/v1/webhooks/simulate")
    async def simulate_webhook_event(
        request: Request,
        _: bool = Depends(require_operator_token),
        db: AsyncSession = Depends(get_db)
    ):
        """
        Simulates a webhook event without PayPal signature verification.
        Available only when DEMO_MODE=True and APP_ENV != production.
        """
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid JSON payload")

        res = await process_webhook_event(db, body, headers={}, source="simulated")
        return res

# --- Operator AG Grid Static Console ---
os.makedirs("src/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="src/static"), name="static")

@app.get("/")
async def serve_dashboard():
    dashboard_path = os.path.join("src", "static", "index.html")
    if os.path.exists(dashboard_path):
        return FileResponse(dashboard_path)
    return {"message": "PayAgent Guardian API is running. Visit /docs for Swagger UI."}
