import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest
import httpx
from sqlalchemy import select

from src.main import app
from src.database import async_session_factory
from src.config import settings
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import TaskStatus, PayoutRequestStatus, AgentStatus, DecisionReason
from src.schemas.llm import LLMVerdict
from src.services import gate2_reviewer
from src.services.guardian import get_current_hour_window
from conftest import settle, wait_until

OP = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}
PASS = LLMVerdict(passed=True, confidence=0.95, reasoning="Criteria met.")

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

class GatedLLM:
    """Stand-in for review_proof_of_task that blocks until released, to make Gate 2 'slow'."""
    def __init__(self, verdict=PASS):
        self.verdict = verdict
        self.release = asyncio.Event()
        self.calls = 0

    async def __call__(self, **kwargs):
        self.calls += 1
        await self.release.wait()
        return self.verdict

async def setup_agent_and_task(client, budget="100.00", hourly_cap="500.00", task_suffix=""):
    agent_id = f"ag-async-{uuid.uuid4().hex[:8]}"
    key = (await client.post("/api/v1/agents", json={"agent_id": agent_id, "name": "Async Agent", "hourly_cap": hourly_cap},
                             headers=OP)).json()["api_key"]
    task_id = await create_task(client, agent_id, budget, task_suffix)
    return agent_id, {"Authorization": f"Bearer {key}"}, task_id

async def create_task(client, agent_id, budget="100.00", suffix=""):
    task_id = f"t-async-{uuid.uuid4().hex[:8]}{suffix}"
    resp = await client.post("/api/v1/tasks", json={
        "task_id": task_id, "title": "Async Task", "acceptance_criteria": "Done well",
        "budget": budget, "allowed_recipient": "contractor@sandbox.paypal.com",
        "assigned_agent_id": agent_id, "required_deliverables": ["output"],
        "deadline": (utc_now() + timedelta(days=1)).isoformat()}, headers=OP)
    assert resp.status_code == 201
    return task_id

async def budget_window(agent_id):
    async with async_session_factory() as db:
        return (await db.execute(select(BudgetWindowModel).where(
            BudgetWindowModel.agent_id == agent_id,
            BudgetWindowModel.window_start == get_current_hour_window()))).scalar_one()

async def task_status(task_id):
    async with async_session_factory() as db:
        return (await db.execute(select(TaskModel).where(TaskModel.task_id == task_id))).scalar_one().status

def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

@pytest.mark.asyncio
async def test_submission_returns_202_before_slow_gate2(monkeypatch):
    """The agent gets 202 RECEIVED immediately; the slow LLM review finishes in the background."""
    llm = GatedLLM()
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", llm)
    async with client() as c:
        agent_id, ah, task_id = await setup_agent_and_task(c)
        t0 = time.monotonic()
        resp = await asyncio.wait_for(c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "25.00", "deliverables": {"output": "work"}}, headers=ah), timeout=5)
        assert time.monotonic() - t0 < 5
        assert resp.status_code == 202
        assert resp.json()["status"] == "RECEIVED"
        assert resp.json()["decided_by"] == "guardian_gate1"

        # While Gate 2 runs: budget is reserved and the task is locked
        waited = await wait_until(lambda: llm.calls >= 1)
        print(f"background review reached the LLM {waited * 1000:.0f} ms after the 202")
        assert llm.calls == 1
        w = await budget_window(agent_id)
        assert (w.reserved, w.spent) == (Decimal("25.00"), Decimal("0.00"))
        assert await task_status(task_id) == TaskStatus.PAYOUT_IN_PROGRESS
        polled = (await c.get(f"/api/v1/payout-requests/{resp.json()['payout_request_id']}", headers=ah)).json()
        assert polled["status"] == "RECEIVED"

        llm.release.set()
        data = await settle(c, resp, ah)
        assert data["status"] == "PAID"
        assert data["decision_reasons"] == [DecisionReason.AUTO_APPROVED.value]
        assert data["decided_by"] == "guardian_gate2"
        assert data["llm_verdict"]["passed"] is True
        w = await budget_window(agent_id)
        assert (w.reserved, w.spent) == (Decimal("0.00"), Decimal("25.00"))
        assert await task_status(task_id) == TaskStatus.PAID


@pytest.mark.asyncio
async def test_one_task_one_payout_while_first_request_pending(monkeypatch):
    """A second submission for the same task is rejected while the first is in review or awaiting a human."""
    llm = GatedLLM()
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", llm)
    async with client() as c:
        agent_id, ah, task_id = await setup_agent_and_task(c)
        first = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "75.00", "deliverables": {"output": "first proof"}}, headers=ah)
        assert first.status_code == 202

        # Still in review
        second = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "75.00", "deliverables": {"output": "second proof"}}, headers=ah)
        assert second.status_code == 403
        assert second.json()["detail"]["error"] == DecisionReason.TASK_NOT_ASSIGNED.value

        llm.release.set()
        data = await settle(c, first, ah)
        assert data["status"] == "PENDING_HUMAN_APPROVAL"

        # Awaiting the operator: still locked (this used to allow a second $75 for the same $100 task)
        third = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "75.00", "deliverables": {"output": "third proof"}}, headers=ah)
        assert third.status_code == 403
        w = await budget_window(agent_id)
        assert w.reserved == Decimal("75.00")

        # Operator rejects -> task reopens, reservation released, agent may resubmit
        rej = await c.post(f"/api/v1/payout-requests/{data['payout_request_id']}/reject", headers=OP)
        assert rej.json()["status"] == "HUMAN_REJECTED"
        assert await task_status(task_id) == TaskStatus.OPEN
        assert (await budget_window(agent_id)).reserved == Decimal("0.00")
        fourth = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "75.00", "deliverables": {"output": "fourth proof"}}, headers=ah)
        assert (await settle(c, fourth, ah))["status"] == "PENDING_HUMAN_APPROVAL"


@pytest.mark.asyncio
async def test_artifact_replay_rejected_while_original_in_review(monkeypatch):
    llm = GatedLLM()
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", llm)
    async with client() as c:
        agent_id, ah, task_a = await setup_agent_and_task(c)
        task_b = await create_task(c, agent_id)
        first = await c.post("/api/v1/payout-requests", json={
            "task_id": task_a, "amount": "20.00", "deliverables": {"output": "same artifact"}}, headers=ah)
        assert first.status_code == 202
        replay = await c.post("/api/v1/payout-requests", json={
            "task_id": task_b, "amount": "20.00", "deliverables": {"output": "same artifact"}}, headers=ah)
        assert replay.status_code == 409
        assert replay.json()["detail"]["error"] == DecisionReason.ARTIFACT_REPLAY.value
        llm.release.set()
        assert (await settle(c, first, ah))["status"] == "PAID"


@pytest.mark.asyncio
async def test_orphaned_received_request_resumes_after_restart(monkeypatch):
    """A RECEIVED row with no in-process review (e.g. the server restarted mid-review) is picked up by the sweeper."""
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", GatedLLM())
    async with client() as c:
        agent_id, ah, task_id = await setup_agent_and_task(c)
        resp = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "30.00", "deliverables": {"output": "work"}}, headers=ah)
        pid = resp.json()["payout_request_id"]

        # Simulate the process dying mid-review: in-flight task gone, in-memory bookkeeping lost
        for t in list(gate2_reviewer._inflight.values()):
            t.cancel()
        await gate2_reviewer.drain()
        gate2_reviewer._last_attempt.clear()
        async with async_session_factory() as db:
            row = (await db.execute(select(PayoutRequestModel).where(PayoutRequestModel.payout_request_id == pid))).scalar_one()
            assert row.status == PayoutRequestStatus.RECEIVED

        # "Restarted" process with a working LLM
        async def ok(**kwargs):
            return PASS
        monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", ok)
        assert await gate2_reviewer.resume_orphaned_reviews() == 1
        await gate2_reviewer.drain()
        data = (await c.get(f"/api/v1/payout-requests/{pid}", headers=ah)).json()
        assert data["status"] == "PAID"
        w = await budget_window(agent_id)
        assert (w.reserved, w.spent) == (Decimal("0.00"), Decimal("30.00"))


@pytest.mark.asyncio
async def test_failed_review_keeps_reservation_and_is_retried(monkeypatch):
    attempts = {"n": 0}
    async def flaky(**kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("simulated crash inside Gate 2")
        return PASS
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", flaky)
    async with client() as c:
        agent_id, ah, task_id = await setup_agent_and_task(c)
        resp = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "15.00", "deliverables": {"output": "work"}}, headers=ah)
        await gate2_reviewer.drain()
        pid = resp.json()["payout_request_id"]
        assert (await c.get(f"/api/v1/payout-requests/{pid}", headers=ah)).json()["status"] == "RECEIVED"
        assert (await budget_window(agent_id)).reserved == Decimal("15.00")

        # Within the retry interval the sweeper leaves it alone; after it, the review is retried
        assert await gate2_reviewer.resume_orphaned_reviews() == 0
        assert await gate2_reviewer.resume_orphaned_reviews(min_retry_seconds=0) == 1
        await gate2_reviewer.drain()
        assert (await c.get(f"/api/v1/payout-requests/{pid}", headers=ah)).json()["status"] == "PAID"


@pytest.mark.asyncio
async def test_quarantine_during_review_blocks_auto_approval(monkeypatch):
    """If the agent is quarantined while its request is in review, a passing verdict escalates instead of paying."""
    llm = GatedLLM()
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", llm)
    async with client() as c:
        agent_id, ah, task_id = await setup_agent_and_task(c)
        resp = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "20.00", "deliverables": {"output": "work"}}, headers=ah)
        async with async_session_factory() as db:
            ag = (await db.execute(select(AgentModel).where(AgentModel.agent_id == agent_id))).scalar_one()
            ag.status = AgentStatus.QUARANTINED
            await db.commit()
        llm.release.set()
        data = await settle(c, resp, ah)
        assert data["status"] == "PENDING_HUMAN_APPROVAL"
        assert DecisionReason.AGENT_QUARANTINED.value in data["decision_reasons"]
        assert DecisionReason.AUTO_APPROVED.value not in data["decision_reasons"]
        assert data["payout_batch_id"] is None


@pytest.mark.asyncio
async def test_llm_unavailable_escalates_in_background(monkeypatch):
    async def down(**kwargs):
        return None
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", down)
    async with client() as c:
        agent_id, ah, task_id = await setup_agent_and_task(c)
        resp = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "20.00", "deliverables": {"output": "work"}}, headers=ah)
        data = await settle(c, resp, ah)
        assert data["status"] == "PENDING_HUMAN_APPROVAL"
        assert data["decision_reasons"] == [DecisionReason.LLM_UNAVAILABLE.value]
        assert (await budget_window(agent_id)).reserved == Decimal("20.00")


@pytest.mark.asyncio
async def test_concurrent_reviews_of_same_request_pay_once(monkeypatch):
    """Two reviewers racing on one RECEIVED row (e.g. sweeper + original task): CAS lets exactly one decide."""
    async def ok(**kwargs):
        await asyncio.sleep(0.05)
        return PASS
    monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", GatedLLM())
    async with client() as c:
        agent_id, ah, task_id = await setup_agent_and_task(c)
        resp = await c.post("/api/v1/payout-requests", json={
            "task_id": task_id, "amount": "40.00", "deliverables": {"output": "work"}}, headers=ah)
        pid = resp.json()["payout_request_id"]
        for t in list(gate2_reviewer._inflight.values()):
            t.cancel()
        await gate2_reviewer.drain()

        monkeypatch.setattr(gate2_reviewer, "review_proof_of_task", ok)
        results = await asyncio.gather(
            gate2_reviewer.run_gate2_review(pid), gate2_reviewer.run_gate2_review(pid), return_exceptions=True)
        assert not [r for r in results if isinstance(r, Exception)], results
        data = (await c.get(f"/api/v1/payout-requests/{pid}", headers=ah)).json()
        assert data["status"] == "PAID"
        w = await budget_window(agent_id)
        assert (w.reserved, w.spent) == (Decimal("0.00"), Decimal("40.00"))
