import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select

from src.database import async_session_factory, init_db
from src.config import settings
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.schemas.enums import AgentStatus, TaskStatus
from src.services.auth import hash_api_key

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

async def seed_database():
    """Populates initial demo agents and tasks for local review and sandbox testing."""
    print("Initializing database tables...")
    await init_db()

    async with async_session_factory() as db:
        # Determine sandbox recipient email from settings or default
        recipient = settings.SANDBOX_RECIPIENT_EMAIL or "sb-personal-contractor@personal.example.com"

        # 1. Seed Agents
        agents_data = [
            {
                "agent_id": "agent-code-reviewer-01",
                "name": "Autonomous Code Reviewer",
                "api_key": "pa_demo_agent_key_secret_123",
                "hourly_cap": Decimal("200.00")
            },
            {
                "agent_id": "agent-data-labeler-02",
                "name": "Data Synthesis Swarm",
                "api_key": "pa_demo_agent_key_secret_456",
                "hourly_cap": Decimal("150.00")
            }
        ]

        seeded_agents = []
        for ag_info in agents_data:
            stmt = select(AgentModel).where(AgentModel.agent_id == ag_info["agent_id"])
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if not existing:
                agent = AgentModel(
                    agent_id=ag_info["agent_id"],
                    name=ag_info["name"],
                    api_key_hash=hash_api_key(ag_info["api_key"]),
                    status=AgentStatus.ACTIVE,
                    hourly_cap=ag_info["hourly_cap"]
                )
                db.add(agent)
                seeded_agents.append((ag_info["agent_id"], ag_info["api_key"]))
                print(f"  [+] Seeded agent: {ag_info['agent_id']} (Key: {ag_info['api_key']})")
            else:
                seeded_agents.append((ag_info["agent_id"], ag_info["api_key"]))
                print(f"  [.] Agent already present: {ag_info['agent_id']}")

        # 2. Seed Tasks
        tasks_data = [
            {
                "task_id": "task-pr-audit-101",
                "title": "Review Security Vulnerabilities in PR #42",
                "acceptance_criteria": "Verify OWASP Top 10 security review and checklist completed",
                "budget": Decimal("25.00"),
                "currency": "USD",
                "allowed_recipient": recipient,
                "assigned_agent_id": "agent-code-reviewer-01",
                "required_deliverables": ["review_summary", "security_checklist"],
                "deadline": utc_now() + timedelta(days=7),
                "status": TaskStatus.OPEN
            },
            {
                "task_id": "task-high-value-102",
                "title": "Core Architecture Refactor Audit (High Value)",
                "acceptance_criteria": "Comprehensive architectural risk audit and stress benchmarks",
                "budget": Decimal("120.00"),
                "currency": "USD",
                "allowed_recipient": recipient,
                "assigned_agent_id": "agent-code-reviewer-01",
                "required_deliverables": ["audit_report", "test_suite_logs"],
                "deadline": utc_now() + timedelta(days=7),
                "status": TaskStatus.OPEN
            },
            {
                "task_id": "task-dataset-curation-103",
                "title": "Annotate 500 Multimodal Payout Samples",
                "acceptance_criteria": "Gold-standard labels verified with inter-annotator agreement > 0.90",
                "budget": Decimal("45.00"),
                "currency": "USD",
                "allowed_recipient": recipient,
                "assigned_agent_id": "agent-data-labeler-02",
                "required_deliverables": ["dataset_url", "validation_metrics"],
                "deadline": utc_now() + timedelta(days=5),
                "status": TaskStatus.OPEN
            }
        ]

        for t_info in tasks_data:
            stmt = select(TaskModel).where(TaskModel.task_id == t_info["task_id"])
            existing = (await db.execute(stmt)).scalar_one_or_none()
            if not existing:
                task = TaskModel(**t_info)
                db.add(task)
                print(f"  [+] Seeded task: {t_info['task_id']} (${t_info['budget']}) -> Assigned to {t_info['assigned_agent_id']}")
            else:
                print(f"  [.] Task already present: {t_info['task_id']}")

        await db.commit()

    print("\nDatabase seeding completed successfully!")
    print(f"Operator Token: {settings.OPERATOR_TOKEN}")
    print(f"Sandbox Recipient Email: {recipient}")
    for aid, key in seeded_agents:
        print(f"Agent: {aid} | Key: {key}")

if __name__ == "__main__":
    asyncio.run(seed_database())
