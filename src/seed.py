import argparse
import asyncio
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from sqlalchemy import select, delete

from src.database import async_session_factory, init_db
from src.config import settings
from src.models.agent import AgentModel
from src.models.task import TaskModel
from src.models.payout import PayoutRequestModel, PayoutItemModel
from src.models.budget import BudgetWindowModel
from src.models.incident import AgentIncidentModel
from src.schemas.enums import AgentStatus, TaskStatus, PayoutRequestStatus
from src.services.auth import hash_api_key

def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

async def seed_database(reset: bool = False):
    """Populates initial demo agents and tasks for local review and sandbox testing."""
    print("Initializing database tables...")
    await init_db()

    async with async_session_factory() as db:
        # P5: Determine credentials posture
        is_prod = settings.APP_ENV == "production"
        recipient = settings.SANDBOX_RECIPIENT_EMAIL or "sb-personal-contractor@personal.example.com"

        demo_agents_keys = {
            "agent-code-reviewer-01": secrets.token_urlsafe(32) if is_prod else "pa_demo_agent_key_secret_123",
            "agent-data-labeler-02": secrets.token_urlsafe(32) if is_prod else "pa_demo_agent_key_secret_456"
        }

        # P6: Reset state if requested
        if reset:
            print("Resetting existing demo tasks, payout requests, and circuit-breaker incidents...")
            demo_task_ids = ["task-pr-audit-101", "task-high-value-102", "task-dataset-curation-103"]
            demo_agent_ids = list(demo_agents_keys.keys())

            # Auditor safety guard: refuse reset if non-final requests exist outside mock mode
            if not settings.MOCK_PAYPAL:
                terminal_states = {
                    PayoutRequestStatus.PAID,
                    PayoutRequestStatus.FAILED,
                    PayoutRequestStatus.POLICY_REJECTED,
                    PayoutRequestStatus.HUMAN_REJECTED,
                    PayoutRequestStatus.CANCELED,
                    PayoutRequestStatus.RETURNED,
                }
                stmt_non_final = select(PayoutRequestModel).where(
                    PayoutRequestModel.task_id.in_(demo_task_ids),
                    PayoutRequestModel.status.not_in(terminal_states)
                )
                non_final_reqs = (await db.execute(stmt_non_final)).scalars().all()
                if non_final_reqs:
                    active_ids = [f"{r.payout_request_id} ({r.status.value})" for r in non_final_reqs]
                    raise RuntimeError(
                        f"Refusing to --reset database outside mock mode: active non-final payout request(s) exist: {', '.join(active_ids)}. "
                        "Wait for poller reconciliation or cancel unclaimed items before resetting."
                    )

            # Delete existing payout requests for demo tasks
            stmt_reqs = select(PayoutRequestModel).where(PayoutRequestModel.task_id.in_(demo_task_ids))
            reqs = (await db.execute(stmt_reqs)).scalars().all()
            for r in reqs:
                await db.delete(r)

            # Clear incidents and budget holds for demo agents
            await db.execute(delete(AgentIncidentModel).where(AgentIncidentModel.agent_id.in_(demo_agent_ids)))
            await db.execute(delete(BudgetWindowModel).where(BudgetWindowModel.agent_id.in_(demo_agent_ids)))
            
            # Reset tasks to OPEN
            await db.execute(delete(TaskModel).where(TaskModel.task_id.in_(demo_task_ids)))
            # Reset agents to ACTIVE
            await db.execute(delete(AgentModel).where(AgentModel.agent_id.in_(demo_agent_ids)))
            await db.commit()
            print("  [+] Database reset complete.")

        # 1. Seed Agents
        agents_data = [
            {
                "agent_id": "agent-code-reviewer-01",
                "name": "Autonomous Code Reviewer",
                "api_key": demo_agents_keys["agent-code-reviewer-01"],
                "hourly_cap": Decimal("200.00")
            },
            {
                "agent_id": "agent-data-labeler-02",
                "name": "Data Synthesis Swarm",
                "api_key": demo_agents_keys["agent-data-labeler-02"],
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
                print(f"  [+] Seeded agent: {ag_info['agent_id']}")
            else:
                existing.status = AgentStatus.ACTIVE
                seeded_agents.append((ag_info["agent_id"], ag_info["api_key"]))
                print(f"  [.] Agent already present: {ag_info['agent_id']} (reset to ACTIVE)")

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
                existing.status = TaskStatus.OPEN
                print(f"  [.] Task already present: {t_info['task_id']} (reset to OPEN)")

        await db.commit()

    print("\nDatabase seeding completed successfully!")
    if not is_prod:
        print(f"Operator Token: {settings.OPERATOR_TOKEN}")
        for aid, key in seeded_agents:
            print(f"Agent: {aid} | Key: {key}")
    else:
        print("Production environment active: Credentials generated securely without console logging.")
        print("Note: In production, register agents via POST /api/v1/agents to retrieve single-use API keys.")
    print(f"Sandbox Recipient Email: {recipient}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed PayAgent Demo Data")
    parser.add_argument("--reset", action="store_true", help="Clear previous demo tasks, runs, and incidents before seeding")
    args = parser.parse_args()
    asyncio.run(seed_database(reset=args.reset))
