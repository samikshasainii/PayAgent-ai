from decimal import Decimal

import pytest
from sqlalchemy import inspect, select, text

from src.config import settings
from src.database import engine, Base, async_session_factory
from src.models.payout import PayoutRequestModel
from src.schemas.enums import PayoutRequestStatus
from src.seed import recreate_database_schema


async def _add_request(request_id: str, status: PayoutRequestStatus):
    async with async_session_factory() as db:
        db.add(PayoutRequestModel(
            payout_request_id=request_id, task_id="task-x", agent_id="agent-x", amount=Decimal("10.00"),
            deliverables=[], artifact_sha256="a" * 64, status=status, decision_reasons=[],
            sender_batch_id=f"payagent_{request_id}", paypal_request_id=f"rq_{request_id}",
        ))
        await db.commit()


async def _request_exists(request_id: str) -> bool:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT 1 FROM payout_requests WHERE payout_request_id = :id"), {"id": request_id}
        )
        return rows.first() is not None


@pytest.fixture
def live_paypal(monkeypatch):
    monkeypatch.setattr(settings, "MOCK_PAYPAL", False)


async def test_recreate_requires_confirmation_flag():
    with pytest.raises(RuntimeError, match="--yes-drop-everything"):
        await recreate_database_schema(yes_drop_everything=False)


async def test_recreate_refuses_with_inflight_payout(live_paypal):
    await _add_request("pr-inflight", PayoutRequestStatus.SUBMITTED)
    with pytest.raises(RuntimeError, match=r"pr-inflight \(SUBMITTED\)"):
        await recreate_database_schema(yes_drop_everything=True)
    assert await _request_exists("pr-inflight")


async def test_recreate_refuses_with_inflight_payout_on_drifted_schema(live_paypal):
    """Regression: a model-wide SELECT failed on a drifted table and the error was swallowed, dropping in-flight rows."""
    await _add_request("pr-inflight", PayoutRequestStatus.SUBMITTED)
    async with engine.begin() as conn:
        await conn.execute(text("ALTER TABLE payout_requests DROP COLUMN decided_by"))

    with pytest.raises(RuntimeError, match=r"pr-inflight \(SUBMITTED\)"):
        await recreate_database_schema(yes_drop_everything=True)
    assert await _request_exists("pr-inflight")


async def test_recreate_proceeds_when_only_terminal_requests(live_paypal):
    await _add_request("pr-done", PayoutRequestStatus.PAID)
    await recreate_database_schema(yes_drop_everything=True)
    assert not await _request_exists("pr-done")


async def test_recreate_proceeds_on_empty_database(live_paypal):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await recreate_database_schema(yes_drop_everything=True)
    async with engine.connect() as conn:
        tables = set(await conn.run_sync(lambda c: inspect(c).get_table_names()))
    assert set(Base.metadata.tables) <= tables
    async with async_session_factory() as db:
        assert (await db.execute(select(PayoutRequestModel))).first() is None
