import uuid
from decimal import Decimal
from typing import Tuple, Dict, Any, Optional
import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.schemas.enums import PayoutRequestStatus, DecisionReason, TaskStatus, AgentStatus
from src.schemas.payout import PayoutRequestIn
from src.services.guardian import (
    compute_artifact_sha256,
    evaluate_gate1,
)
from src.services.state_machine import transition_payout_request_status
from src.services.paypal_client import paypal_client
from src.services.poller import extract_payout_batch_id_from_error
from src.services.events import broadcaster, utc_iso

async def dispatch_payout(
    db: AsyncSession,
    payout_req: PayoutRequestModel,
    task: TaskModel
) -> PayoutRequestModel:
    """
    Dispatches an approved payout to PayPal using stored IDs for idempotency.
    Uses Write-Ahead SUBMITTED pattern: commits SUBMITTED to disk before calling PayPal,
    and commits payout_batch_id immediately upon PayPal response.
    On definite 4xx rejections, transitions to FAILED and releases the budget reservation.
    On timeouts, network errors, or 5xx responses, keeps the request SUBMITTED and retains IDs for reconciliation.
    """
    # Write-ahead SUBMITTED: commit before calling PayPal so a crash leaves request SUBMITTED for the poller
    if payout_req.status != PayoutRequestStatus.SUBMITTED:
        await transition_payout_request_status(db, payout_req, PayoutRequestStatus.SUBMITTED)
        req_id = payout_req.payout_request_id
        req_status = payout_req.status.value
        req_updated_at = utc_iso(payout_req.updated_at)
        await db.commit()
        await broadcaster.publish_payout_request(req_id, req_status, req_updated_at)

    try:
        pp_resp = await paypal_client.create_single_payout(
            sender_batch_id=payout_req.sender_batch_id,
            paypal_request_id=payout_req.paypal_request_id,
            recipient_email=task.allowed_recipient,
            amount=payout_req.amount,
            currency=task.currency,
            note=f"Task payout for {task.title}"
        )
        # Commit payout_batch_id write-ahead immediately so poller has it even if subsequent item transitions fail
        payout_req.payout_batch_id = pp_resp["batch_header"]["payout_batch_id"]
        await db.commit()

        if "items" in pp_resp and len(pp_resp["items"]) > 0:
            item_data = pp_resp["items"][0]
            item_id = item_data["payout_item_id"]
            
            stmt = select(PayoutItemModel).where(PayoutItemModel.payout_item_id == item_id)
            existing_item = (await db.execute(stmt)).scalar_one_or_none()
            if not existing_item:
                item_model = PayoutItemModel(
                    payout_item_id=item_id,
                    payout_request_id=payout_req.payout_request_id,
                    transaction_status=item_data.get("transaction_status", "SUCCESS"),
                    errors=item_data.get("errors")
                )
                db.add(item_model)
            else:
                existing_item.transaction_status = item_data.get("transaction_status", "SUCCESS")
                existing_item.errors = item_data.get("errors")

            item_status = item_data.get("transaction_status")
            if item_status == "SUCCESS":
                await transition_payout_request_status(db, payout_req, PayoutRequestStatus.PAID)
            elif item_status in ("FAILED", "BLOCKED", "DENIED"):
                await transition_payout_request_status(db, payout_req, PayoutRequestStatus.FAILED)
            elif item_status == "UNCLAIMED":
                await transition_payout_request_status(db, payout_req, PayoutRequestStatus.UNCLAIMED)
            elif item_status == "CANCELED":
                await transition_payout_request_status(db, payout_req, PayoutRequestStatus.CANCELED)
            elif item_status == "RETURNED":
                await transition_payout_request_status(db, payout_req, PayoutRequestStatus.RETURNED)

        req_id = payout_req.payout_request_id
        req_status = payout_req.status.value
        req_updated_at = utc_iso(payout_req.updated_at)
        task_id = task.task_id if task else None
        task_status = task.status.value if task else None

        await db.commit()
        await broadcaster.publish_payout_request(req_id, req_status, req_updated_at)
        if task_id and task_status:
            await broadcaster.publish_task(task_id, task_status)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 429:
            # Stated on purpose: PayPal 429 rate limit exceeded after all retry attempts.
            # PayPal did not process or create the payout batch, so it is safe to mark FAILED,
            # release the budget reservation, and reopen the task to OPEN.
            payout_req.decision_reasons = (payout_req.decision_reasons or []) + [
                f"PAYPAL_RATE_LIMIT_EXCEEDED: PayPal 429 rate limit after retries: {exc.response.text}"
            ]
            await transition_payout_request_status(db, payout_req, PayoutRequestStatus.FAILED)
            req_id = payout_req.payout_request_id
            req_status = payout_req.status.value
            req_updated_at = utc_iso(payout_req.updated_at)
            task_id = task.task_id if task else None
            task_status = task.status.value if task else None

            await db.commit()
            await broadcaster.publish_payout_request(req_id, req_status, req_updated_at)
            if task_id and task_status:
                await broadcaster.publish_task(task_id, task_status)
        elif 400 <= exc.response.status_code < 500:
            # Check if this 4xx error is duplicate sender_batch_id (money was already sent!)
            batch_id = extract_payout_batch_id_from_error(exc.response.text)
            if batch_id:
                # Recover original batch_id, preserve SUBMITTED, and let the poller reconcile it
                payout_req.payout_batch_id = batch_id
                payout_req.decision_reasons = (payout_req.decision_reasons or []) + [
                    f"PAYPAL_DUPLICATE_BATCH_RECOVERED: Recovered original batch {batch_id}"
                ]
                await db.commit()
            else:
                # Definite PayPal rejection (4xx): route through state machine to release budget & reopen task
                payout_req.decision_reasons = (payout_req.decision_reasons or []) + [f"PAYPAL_ERROR: {exc.response.text}"]
                await transition_payout_request_status(db, payout_req, PayoutRequestStatus.FAILED)
                req_id = payout_req.payout_request_id
                req_status = payout_req.status.value
                req_updated_at = utc_iso(payout_req.updated_at)
                task_id = task.task_id if task else None
                task_status = task.status.value if task else None

                await db.commit()
                await broadcaster.publish_payout_request(req_id, req_status, req_updated_at)
                if task_id and task_status:
                    await broadcaster.publish_task(task_id, task_status)
        else:
            # 5xx error: transient/ambiguous failure; keep SUBMITTED for reconciliation
            payout_req.decision_reasons = (payout_req.decision_reasons or []) + [f"PAYPAL_AMBIGUOUS_ERROR: {exc.response.text}"]
            await db.commit()
    except (httpx.TimeoutException, httpx.RequestError) as exc:
        # Ambiguous network / timeout errors: keep SUBMITTED with stored IDs for reconciliation
        payout_req.decision_reasons = (payout_req.decision_reasons or []) + [f"PAYPAL_AMBIGUOUS_ERROR: {str(exc)}"]
        await db.commit()
    except RuntimeError as rerr:
        # Budget window or state machine consistency failure during item status transition:
        # Roll back uncommitted changes immediately. Note that SUBMITTED and payout_batch_id were ALREADY committed above,
        # so the payout record is preserved on disk for the poller to reconcile!
        await db.rollback()
        raise rerr
    except Exception as exc:
        await db.rollback()
        raise exc

    return payout_req

    return payout_req

async def process_payout_submission(
    db: AsyncSession,
    agent: AgentModel,
    request_in: PayoutRequestIn
) -> Tuple[bool, int, str, PayoutRequestModel]:
    """
    Unified submission path shared across REST and MCP interfaces.
    Runs Gate 1 synchronously. On pass, commits the request as RECEIVED (budget reserved, task locked)
    and schedules the Gate 2 LLM review in the background (src/services/gate2_reviewer.py).
    Returns: (passed: bool, http_status_code: int, message: str, payout_model: PayoutRequestModel)
    """
    agent_id = agent.agent_id
    artifact_sha256 = compute_artifact_sha256(request_in.deliverables)

    # Saved-ID pattern: generated once per request inception according to ARCHITECTURE.md §6.1
    payout_uuid = uuid.uuid4()
    payout_request_id = str(payout_uuid)
    sender_batch_id = f"payagent_{payout_uuid.hex}"
    paypal_request_id = payout_request_id

    # Gate 1 Deterministic Evaluation
    gate1_passed, http_code, reason_code, task, hour_bucket = await evaluate_gate1(
        db=db,
        agent=agent,
        payload=request_in,
        artifact_sha256=artifact_sha256
    )

    if gate1_passed:
        # Lock the task (Task FSM: OPEN -> PAYOUT_IN_PROGRESS) so a second submission for the same
        # task cannot pass Gate 1 while this one is in review or awaiting a human: one task, one payout.
        lock_res = await db.execute(
            update(TaskModel)
            .where(TaskModel.task_id == request_in.task_id, TaskModel.status == TaskStatus.OPEN)
            .values(status=TaskStatus.PAYOUT_IN_PROGRESS)
        )
        if lock_res.rowcount == 0:
            # Lost the race to another submission: undo this request's budget reservation
            await db.rollback()
            gate1_passed, http_code, reason_code = False, 403, DecisionReason.TASK_NOT_ASSIGNED.value
        else:
            task.status = TaskStatus.PAYOUT_IN_PROGRESS

    if not gate1_passed:
        rejected_req = PayoutRequestModel(
            payout_request_id=payout_request_id,
            task_id=request_in.task_id,
            agent_id=agent_id,
            amount=request_in.amount,
            deliverables=request_in.deliverables,
            artifact_sha256=artifact_sha256,
            status=PayoutRequestStatus.POLICY_REJECTED,
            decision_reasons=[reason_code],
            sender_batch_id=sender_batch_id,
            paypal_request_id=paypal_request_id,
            window_start=hour_bucket,
            decided_by="guardian_gate1"
        )
        db.add(rejected_req)
        req_id = rejected_req.payout_request_id
        req_status = rejected_req.status.value
        agent_id = agent.agent_id
        agent_status = agent.status.value
        await db.commit()
        await db.refresh(rejected_req)
        await broadcaster.publish_payout_request(
            req_id,
            req_status,
            utc_iso(rejected_req.updated_at)
        )
        if agent_status == AgentStatus.QUARANTINED.value:
            await broadcaster.publish_agent(agent_id, agent_status)
        return False, http_code, reason_code, rejected_req

    # Gate 1 passed: persist as RECEIVED and hand off to the asynchronous Gate 2 reviewer
    payout_req = PayoutRequestModel(
        payout_request_id=payout_request_id,
        task_id=request_in.task_id,
        agent_id=agent_id,
        amount=request_in.amount,
        deliverables=request_in.deliverables,
        artifact_sha256=artifact_sha256,
        status=PayoutRequestStatus.RECEIVED,
        decision_reasons=[],
        sender_batch_id=sender_batch_id,
        paypal_request_id=paypal_request_id,
        window_start=hour_bucket,
        decided_by="guardian_gate1"
    )
    db.add(payout_req)
    req_id = payout_req.payout_request_id
    req_status = payout_req.status.value
    task_id = task.task_id if task else None
    task_status = task.status.value if task else None
    await db.commit()
    await db.refresh(payout_req)
    if task_id and task_status:
        await broadcaster.publish_task(task_id, task_status)
    await broadcaster.publish_payout_request(
        req_id,
        req_status,
        utc_iso(payout_req.updated_at)
    )

    # Local import: gate2_reviewer imports dispatch_payout from this module
    from src.services.gate2_reviewer import schedule_review
    schedule_review(payout_request_id)

    return True, 202, "RECEIVED", payout_req
