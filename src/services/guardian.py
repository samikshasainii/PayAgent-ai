import hashlib
import json
import time
from datetime import datetime, timezone

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
from decimal import Decimal
from typing import Dict, Tuple, List, Optional
from sqlalchemy import select, update, text
from sqlalchemy.ext.asyncio import AsyncSession
from src.config import settings
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.models.budget import BudgetWindowModel
from src.models.incident import AgentIncidentModel
from src.schemas.enums import AgentStatus, TaskStatus, PayoutRequestStatus, DecisionReason
from src.schemas.payout import PayoutRequestIn
from src.schemas.llm import LLMVerdict

def compute_artifact_sha256(deliverables: Dict[str, str]) -> str:
    """Canonical JSON serialization sha256 computed server-side."""
    canonical_json = json.dumps(deliverables, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

def get_current_hour_window() -> str:
    return utc_now().strftime("%Y-%m-%d-%H")

async def record_incident_and_check_circuit_breaker(
    db: AsyncSession,
    agent: AgentModel,
    reason: str,
    payout_request_id: Optional[str] = None
):
    """Record incident and quarantine agent if >= 3 incidents occur within window."""
    now = utc_now()
    incident = AgentIncidentModel(
        agent_id=agent.agent_id,
        reason_code=reason,
        payout_request_id=payout_request_id,
        created_at=now
    )
    db.add(incident)
    await db.flush()

    cutoff = datetime.fromtimestamp(now.timestamp() - settings.CIRCUIT_BREAKER_WINDOW_SECONDS)
    stmt = select(AgentIncidentModel).where(
        AgentIncidentModel.agent_id == agent.agent_id,
        AgentIncidentModel.created_at >= cutoff
    )
    res = await db.execute(stmt)
    incidents = res.scalars().all()

    if len(incidents) >= settings.CIRCUIT_BREAKER_INCIDENT_THRESHOLD:
        agent.status = AgentStatus.QUARANTINED
        await db.flush()

async def evaluate_gate1(
    db: AsyncSession,
    agent: AgentModel,
    payload: PayoutRequestIn,
    artifact_sha256: str
) -> Tuple[bool, int, str, Optional[TaskModel], str]:
    """
    Evaluates Gate 1 canonical decision table rows 2-8.
    Returns: (passed: bool, http_status_code: int, reason_code: str, task: Optional[TaskModel], hour_bucket: str)
    """
    hour_bucket = get_current_hour_window()

    # Row 2: Agent Quarantined
    if agent.status == AgentStatus.QUARANTINED:
        return False, 403, DecisionReason.AGENT_QUARANTINED.value, None, hour_bucket

    # Fetch task
    stmt = select(TaskModel).where(TaskModel.task_id == payload.task_id)
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()

    # Row 3: Task not found / not OPEN / not assigned to this agent
    if not task or task.status != TaskStatus.OPEN or task.assigned_agent_id != agent.agent_id:
        return False, 403, DecisionReason.TASK_NOT_ASSIGNED.value, task, hour_bucket

    # Row 4: Past deadline
    if utc_now() > task.deadline:
        return False, 422, DecisionReason.TASK_EXPIRED.value, task, hour_bucket

    # Row 5: Amount > task.budget
    if payload.amount > task.budget:
        await record_incident_and_check_circuit_breaker(db, agent, DecisionReason.OVER_TASK_BUDGET.value)
        return False, 422, DecisionReason.OVER_TASK_BUDGET.value, task, hour_bucket

    # Row 6: Deliverable keys != required_deliverables
    expected_keys = set(task.required_deliverables)
    provided_keys = set(payload.deliverables.keys())
    if provided_keys != expected_keys:
        await record_incident_and_check_circuit_breaker(db, agent, DecisionReason.DELIVERABLES_INCOMPLETE.value)
        return False, 422, DecisionReason.DELIVERABLES_INCOMPLETE.value, task, hour_bucket

    # Row 7: artifact_sha256 already used in an active or completed request
    # Does NOT burn rejected or failed attempts (an agent can correct amount and resubmit)
    stmt = (
        select(PayoutRequestModel)
        .where(
            PayoutRequestModel.artifact_sha256 == artifact_sha256,
            PayoutRequestModel.status.in_([
                PayoutRequestStatus.APPROVED,
                PayoutRequestStatus.PENDING_HUMAN_APPROVAL,
                PayoutRequestStatus.SUBMITTED,
                PayoutRequestStatus.PAID,
                PayoutRequestStatus.UNCLAIMED,
            ])
        )
        .limit(1)
    )
    res = await db.execute(stmt)
    existing_artifact = res.scalars().first()
    if existing_artifact:
        await record_incident_and_check_circuit_breaker(db, agent, DecisionReason.ARTIFACT_REPLAY.value)
        return False, 409, DecisionReason.ARTIFACT_REPLAY.value, task, hour_bucket

    # Row 8: Atomic budget reservation update
    # Ensure budget window row exists for (agent_id, hour_bucket)
    stmt = select(BudgetWindowModel).where(
        BudgetWindowModel.agent_id == agent.agent_id,
        BudgetWindowModel.window_start == hour_bucket
    )
    res = await db.execute(stmt)
    window = res.scalar_one_or_none()
    if not window:
        window = BudgetWindowModel(
            agent_id=agent.agent_id,
            window_start=hour_bucket,
            reserved=Decimal("0.00"),
            spent=Decimal("0.00")
        )
        db.add(window)
        await db.flush()

    # Conditional atomic update
    hourly_cap = agent.hourly_cap
    update_stmt = (
        update(BudgetWindowModel)
        .where(
            BudgetWindowModel.agent_id == agent.agent_id,
            BudgetWindowModel.window_start == hour_bucket,
            (BudgetWindowModel.spent + BudgetWindowModel.reserved + payload.amount) <= hourly_cap
        )
        .values(reserved=BudgetWindowModel.reserved + payload.amount)
    )
    update_res = await db.execute(update_stmt)
    if update_res.rowcount == 0:
        await record_incident_and_check_circuit_breaker(db, agent, DecisionReason.VELOCITY_CAP.value)
        return False, 429, DecisionReason.VELOCITY_CAP.value, task, hour_bucket

    return True, 200, "OK", task, hour_bucket

def evaluate_gate2_and_decision(
    amount: Decimal,
    llm_verdict: Optional[LLMVerdict]
) -> Tuple[PayoutRequestStatus, List[str], Optional[float]]:
    """
    Evaluates Gate 2 canonical decision table rows 9-12.
    Returns: (target_status, reasons_list, risk_score_display)
    """
    reasons: List[str] = []
    escalate = False

    # Row 9: LLM error / unavailable / non-conforming
    if llm_verdict is None:
        reasons.append(DecisionReason.LLM_UNAVAILABLE.value)
        escalate = True
    else:
        # Row 10: passed == False or confidence < 0.75
        if not llm_verdict.passed or llm_verdict.confidence < settings.MIN_LLM_CONFIDENCE:
            reasons.append(DecisionReason.PROOF_DOUBTFUL.value)
            escalate = True

    # Row 11: amount > 50.00 (strictly greater; exactly $50.00 auto-approves)
    if amount > settings.MAX_SINGLE_TASK_AUTO_APPROVE:
        reasons.append(DecisionReason.ABOVE_AUTO_LIMIT.value)
        escalate = True

    # Display-only risk score calculation
    if llm_verdict:
        risk_score = round(1.0 - llm_verdict.confidence, 2)
        if not llm_verdict.passed:
            risk_score = max(risk_score, 0.90)
    else:
        risk_score = 0.80

    if escalate:
        return PayoutRequestStatus.PENDING_HUMAN_APPROVAL, reasons, risk_score
    else:
        reasons.append(DecisionReason.AUTO_APPROVED.value)
        return PayoutRequestStatus.APPROVED, reasons, risk_score
