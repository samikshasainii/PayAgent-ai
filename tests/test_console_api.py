import pytest
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from httpx import AsyncClient, ASGITransport
from src.main import app
from src.config import settings
from src.database import async_session_factory
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.budget import BudgetWindowModel
from src.schemas.enums import AgentStatus
from src.services.auth import hash_api_key
from src.services.guardian import get_current_hour_window

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

@pytest.mark.asyncio
async def test_register_agent_id_validation_rejects_injection():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}
        
        # Injected agent_id with quotes, parentheses, slashes
        payload = {
            "agent_id": "evil');window.__xssRelease=1;//",
            "name": "Malicious Agent",
            "hourly_cap": "200.00"
        }
        res = await client.post("/api/v1/agents", json=payload, headers=headers)
        assert res.status_code == 422
        
        # Valid agent_id passes pattern
        valid_payload = {
            "agent_id": "valid-agent.01_v2:test",
            "name": "Valid Agent",
            "hourly_cap": "200.00"
        }
        res_ok = await client.post("/api/v1/agents", json=valid_payload, headers=headers)
        assert res_ok.status_code == 201

@pytest.mark.asyncio
async def test_create_task_id_validation_rejects_injection():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}
        
        # Injected task_id
        payload = {
            "task_id": "task<script>alert(1)</script>",
            "title": "Malicious Task",
            "acceptance_criteria": "None",
            "budget": "20.00",
            "allowed_recipient": "contractor@sandbox.paypal.com",
            "assigned_agent_id": "agent-code-reviewer-01",
            "required_deliverables": ["report"],
            "deadline": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        }
        res = await client.post("/api/v1/tasks", json=payload, headers=headers)
        assert res.status_code == 422

@pytest.mark.asyncio
async def test_legacy_task_id_in_db_returns_200_in_task_list():
    async with async_session_factory() as session:
        # Create assigned agent
        agent = AgentModel(
            agent_id="legacy-agent-01",
            name="Legacy Agent",
            api_key_hash=hash_api_key("test_key"),
            hourly_cap=Decimal("100.00"),
            status=AgentStatus.ACTIVE
        )
        session.add(agent)
        
        legacy_task = TaskModel(
            task_id="legacy task #7",
            title="Legacy Task",
            acceptance_criteria="None",
            budget=Decimal("30.00"),
            allowed_recipient="contractor@sandbox.paypal.com",
            assigned_agent_id="legacy-agent-01",
            required_deliverables=["report"],
            deadline=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1),
        )
        session.add(legacy_task)
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {settings.OPERATOR_TOKEN}"}
        res = await client.get("/api/v1/tasks", headers=headers)
        assert res.status_code == 200
        tasks = res.json()
        ids = [t["task_id"] for t in tasks]
        assert "legacy task #7" in ids
