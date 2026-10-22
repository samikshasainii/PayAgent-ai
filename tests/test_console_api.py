import pytest
import uuid
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from src.main import app
from src.config import settings
from src.database import async_session_factory, Base, engine
from src.models.agent import AgentModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import AgentStatus
from src.services.auth import hash_api_key
from src.services.guardian import get_current_hour_window

@pytest.fixture(autouse=True)
async def setup_test_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield

@pytest.mark.asyncio
async def test_get_config_requires_operator_token():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No token
        res = await client.get("/api/v1/config")
        assert res.status_code == 401

        # Invalid token
        res = await client.get("/api/v1/config", headers={"Authorization": "Bearer wrong-token"})
        assert res.status_code == 401

        # Valid token
        res = await client.get("/api/v1/config", headers={"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"})
        assert res.status_code == 200
        data = res.json()
        assert "mock_paypal" in data
        assert "mock_llm" in data
        # Ensure no secrets leaked
        assert "api_key" not in data
        assert "secret" not in data
        assert "token" not in data

@pytest.mark.asyncio
async def test_list_agents_burn_and_quarantine_metrics():
    transport = ASGITransport(app=app)
    agent_id = f"ag-console-{uuid.uuid4().hex[:6]}"
    hour_win = get_current_hour_window()

    async with async_session_factory() as db:
        agent = AgentModel(
            agent_id=agent_id,
            name="Console Test Agent",
            api_key_hash=hash_api_key("key"),
            status=AgentStatus.QUARANTINED,
            hourly_cap=Decimal("200.00")
        )
        budget = BudgetWindowModel(
            agent_id=agent_id,
            window_start=hour_win,
            reserved=Decimal("50.00"),
            spent=Decimal("25.00")
        )
        db.add_all([agent, budget])
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # No token -> 401
        res = await client.get("/api/v1/agents")
        assert res.status_code == 401

        # With operator token
        res = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"})
        assert res.status_code == 200
        agents = res.json()
        target = next((a for a in agents if a["agent_id"] == agent_id), None)
        assert target is not None
        assert target["name"] == "Console Test Agent"
        assert target["status"] == "QUARANTINED"
        assert Decimal(str(target["hourly_cap"])) == Decimal("200.00")
        assert Decimal(str(target["current_reserved"])) == Decimal("50.00")
        assert Decimal(str(target["current_spent"])) == Decimal("25.00")
        assert Decimal(str(target["current_burn"])) == Decimal("75.00")
