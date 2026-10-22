import asyncio
import logging
import time
from typing import Dict, Optional
from sqlalchemy import select, update

from src.config import settings
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.schemas.enums import AgentStatus, PayoutRequestStatus, DecisionReason
from src.services.guardian import evaluate_gate2_and_decision, record_incident_and_check_circuit_breaker
from src.services.proof_review import review_proof_of_task
from src.services.state_machine import ALLOWED_TRANSITIONS
from src.services.payout_service import dispatch_payout
from src.services.events import broadcaster, utc_iso

logger = logging.getLogger("payagent.gate2_reviewer")

# Asynchronous Gate 2 (ARCHITECTURE.md §5.1 rows 9-12, §5.6):
# A submission that passes Gate 1 is committed as RECEIVED (budget reserved, task locked) and the API
# returns 202 immediately. The LLM review runs here in the background with no DB transaction open,
# and its decision is applied with a CAS transition out of RECEIVED.
# Restart safety: the sweeper re-schedules RECEIVED rows (and APPROVED rows that never reached
# PayPal) that no in-process task owns. Single-process by design (render.yaml runs --workers 1).

_inflight: Dict[str, asyncio.Task] = {}
_last_attempt: Dict[str, float] = {}

def schedule_review(payout_request_id: str) -> None:
    """Starts the background Gate 2 review for a RECEIVED request (no-op if one is already running)."""
    if payout_request_id in _inflight:
        return
    _last_attempt[payout_request_id] = time.monotonic()
    task = asyncio.create_task(_run_safely(payout_request_id))
    _inflight[payout_request_id] = task
    task.add_done_callback(lambda _t: _inflight.pop(payout_request_id, None))

async def _run_safely(payout_request_id: str) -> None:
    try:
        await run_gate2_review(payout_request_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Row stays RECEIVED/APPROVED with its reservation intact; the sweeper retries it later
        logger.error(f"Gate 2 review failed for {payout_request_id}: {exc}", exc_info=True)

async def run_gate2_review(payout_request_id: str) -> Optional[PayoutRequestStatus]:
    """
    Reviews one RECEIVED request and applies the decision table rows 9-12.
    Also dispatches APPROVED requests that were decided but never submitted (crash recovery).
    Returns the request's resulting status, or None if it no longer exists.
    """
    async with async_session_factory() as db:
        req = (await db.execute(
            select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == payout_request_id)
        )).scalar_one_or_none()
        if not req:
            return None
        if req.status not in (PayoutRequestStatus.RECEIVED, PayoutRequestStatus.APPROVED):
            return req.status

        task = (await db.execute(select(TaskModel).where(TaskModel.task_id == req.task_id))).scalar_one()

        if req.status == PayoutRequestStatus.RECEIVED:
            # End the read transaction before the slow LLM call so no SQLite lock is held during it
            await db.commit()

            llm_verdict = await review_proof_of_task(
                task_title=task.title,
                acceptance_criteria=task.acceptance_criteria,
                required_deliverables=task.required_deliverables,
                deliverables=req.deliverables
            )
            target_status, reasons, risk_score = evaluate_gate2_and_decision(req.amount, llm_verdict)

            # Re-read the agent after the review: if it was quarantined meanwhile (§5.5), an
            # auto-approval is escalated to a human instead of paying out (escalate-only).
            agent = (await db.execute(select(AgentModel).where(AgentModel.agent_id == req.agent_id))).scalar_one()
            if agent.status == AgentStatus.QUARANTINED and target_status == PayoutRequestStatus.APPROVED:
                target_status = PayoutRequestStatus.PENDING_HUMAN_APPROVAL
                reasons = [r for r in reasons if r != DecisionReason.AUTO_APPROVED.value]
                reasons.append(DecisionReason.AGENT_QUARANTINED.value)

            # Atomic CAS out of RECEIVED, writing the decision in the same statement. Done here rather than
            # via transition_payout_request_status because a loser there refreshes to the winner's status
            # and cannot tell it lost. RECEIVED -> APPROVED / PENDING_HUMAN_APPROVAL has no budget or task
            # side effects (the reservation and task lock were taken at Gate 1).
            assert target_status in ALLOWED_TRANSITIONS[PayoutRequestStatus.RECEIVED]
            claim = await db.execute(
                update(PayoutRequestModel)
                .where(
                    PayoutRequestModel.payout_request_id == payout_request_id,
                    PayoutRequestModel.status == PayoutRequestStatus.RECEIVED
                )
                .values(
                    status=target_status,
                    decided_by="guardian_gate2",
                    decision_reasons=reasons,
                    llm_verdict=llm_verdict.model_dump() if llm_verdict else None,
                    risk_score=risk_score
                )
            )
            if claim.rowcount == 0:
                # CAS lost: another reviewer already decided this request
                await db.rollback()
                return None
            await db.refresh(req)

            # ARCHITECTURE.md §5.5: Record incident if Gate 2 detected PROOF_DOUBTFUL
            if DecisionReason.PROOF_DOUBTFUL.value in reasons:
                await record_incident_and_check_circuit_breaker(
                    db=db,
                    agent=agent,
                    reason=DecisionReason.PROOF_DOUBTFUL.value,
                    payout_request_id=payout_request_id
                )
            req_id = req.payout_request_id
            req_status = req.status.value
            req_updated_at = utc_iso(req.updated_at)
            agent_id = agent.agent_id
            agent_status = agent.status.value

            await db.commit()
            await broadcaster.publish_payout_request(
                req_id,
                req_status,
                req_updated_at
            )
            if agent_status == AgentStatus.QUARANTINED.value:
                await broadcaster.publish_agent(agent_id, agent_status)

        # If auto-approved, dispatch to PayPal (write-ahead SUBMITTED inside dispatch_payout)
        if req.status == PayoutRequestStatus.APPROVED:
            await dispatch_payout(db, req, task)

        return req.status

async def resume_orphaned_reviews(min_retry_seconds: Optional[float] = None) -> int:
    """
    Re-schedules RECEIVED/APPROVED requests that no in-process task owns, e.g. after a restart
    or a failed review. Each request is retried at most once per min_retry_seconds.
    """
    retry_after = min_retry_seconds if min_retry_seconds is not None else settings.LLM_TIMEOUT_SECONDS + 30
    async with async_session_factory() as db:
        ids = (await db.execute(
            select(PayoutRequestModel.payout_request_id).where(
                PayoutRequestModel.status.in_([PayoutRequestStatus.RECEIVED, PayoutRequestStatus.APPROVED])
            )
        )).scalars().all()

    now = time.monotonic()
    scheduled = 0
    for pid in ids:
        if pid in _inflight:
            continue
        last = _last_attempt.get(pid)
        if last is not None and now - last < retry_after:
            continue
        schedule_review(pid)
        scheduled += 1
    if scheduled:
        logger.info(f"Gate 2 sweeper re-scheduled {scheduled} orphaned request(s).")
    return scheduled

async def drain() -> None:
    """Waits until every in-flight review has finished (tests and graceful shutdown)."""
    while _inflight:
        await asyncio.gather(*list(_inflight.values()), return_exceptions=True)

class Gate2Sweeper:
    """Background loop that recovers orphaned Gate 2 reviews."""
    def __init__(self, interval_seconds: float = 10.0):
        self.interval = interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def _run_loop(self):
        while self._running:
            try:
                await resume_orphaned_reviews()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in Gate 2 sweeper: {exc}", exc_info=True)
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break

    def start(self):
        if not self._running:
            self._running = True
            self._task = asyncio.create_task(self._run_loop())
            logger.info("Gate 2 sweeper started.")

    async def stop(self):
        if self._running:
            self._running = False
            if self._task:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
        # Cancel in-flight reviews; their rows stay RECEIVED/APPROVED and resume on next start
        for t in list(_inflight.values()):
            t.cancel()
        await asyncio.gather(*list(_inflight.values()), return_exceptions=True)
        logger.info("Gate 2 sweeper stopped.")

sweeper = Gate2Sweeper(interval_seconds=10.0)
