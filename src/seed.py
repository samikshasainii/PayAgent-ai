import argparse
import asyncio
import secrets
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional
from sqlalchemy import select, delete, inspect, text, Enum as SQLEnum

from src.database import async_session_factory, init_db, engine, Base
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

def mask_recipient_email(email: str) -> str:
    """Mask email for production log safety: first 2 chars of local part + '***@' + domain."""
    if "@" in email:
        local, domain = email.split("@", 1)
        prefix = local[:2]
        return f"{prefix}***@{domain}"
    return "***"

# Requests in these states have no PayPal work in flight; anything else blocks --reset / --recreate-schema outside mock mode
TERMINAL_STATUSES = {
    PayoutRequestStatus.PAID,
    PayoutRequestStatus.FAILED,
    PayoutRequestStatus.POLICY_REJECTED,
    PayoutRequestStatus.HUMAN_REJECTED,
    PayoutRequestStatus.CANCELED,
    PayoutRequestStatus.RETURNED,
}

async def _refuse_if_inflight_payouts():
    """
    Fail-closed in-flight check for --recreate-schema. It must work on a drifted schema (the reason the
    command exists), so it reads only the two columns every schema version has, via raw SQL.
    """
    async with engine.connect() as conn:
        try:
            if not await conn.run_sync(lambda c: inspect(c).has_table("payout_requests")):
                return
            rows = (await conn.execute(text("SELECT payout_request_id, CAST(status AS VARCHAR) FROM payout_requests"))).all()
        except Exception as exc:
            raise RuntimeError(
                f"Refusing to --recreate-schema: the in-flight payout check could not run ({type(exc).__name__}: {exc})."
            ) from exc
    # SQLAlchemy stores Enum members by name
    terminal_names = {s.name for s in TERMINAL_STATUSES}
    active_ids = [f"{pid} ({st})" for pid, st in rows if st not in terminal_names]
    if active_ids:
        raise RuntimeError(
            f"Refusing to --recreate-schema outside mock mode: active non-final payout request(s) exist: {', '.join(active_ids)}. "
            "Wait for poller reconciliation or cancel unclaimed items before resetting."
        )

async def recreate_database_schema(yes_drop_everything: bool = False):
    """Drops and recreates all tables and enums. Safe recovery command for schema drift."""
    if not yes_drop_everything:
        raise RuntimeError("Refusing to --recreate-schema without --yes-drop-everything confirmation flag.")

    # Auditor safety guard: refuse if non-final requests exist outside mock mode
    if not settings.MOCK_PAYPAL:
        await _refuse_if_inflight_payouts()

    print("Dropping all existing tables and enums...")
    async with engine.begin() as conn:
        def _drop_and_create(sync_conn):
            Base.metadata.drop_all(sync_conn)
            if sync_conn.dialect.name == "postgresql":
                # Only this app's enum types; a drifted DB may still hold ones drop_all no longer knows about
                app_enums = {
                    col.type.name
                    for table in Base.metadata.tables.values()
                    for col in table.columns
                    if isinstance(col.type, SQLEnum) and col.type.name
                }
                for name in sorted(app_enums):
                    sync_conn.execute(text(f'DROP TYPE IF EXISTS "{name}" CASCADE'))
            Base.metadata.create_all(sync_conn)
        await conn.run_sync(_drop_and_create)
    print("Schema recreation complete.")


async def seed_database(
    reset: bool = False,
    recreate_schema: bool = False,
    yes_drop_everything: bool = False,
    keys_output_path: Optional[str] = None,
):
    """Populates initial demo agents and tasks for local review and sandbox testing."""
    if keys_output_path:
        check = subprocess.run(["git", "check-ignore", "-q", keys_output_path], check=False)
        if check.returncode != 0:
            raise RuntimeError(f"Refusing to write secrets: {keys_output_path} is not gitignored")

    if recreate_schema:
        await recreate_database_schema(yes_drop_everything=yes_drop_everything)
    else:
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

        # P6: Reset state if requested (or when recreate_schema ran)
        if reset and not recreate_schema:
            print("Resetting existing demo tasks, payout requests, and circuit-breaker incidents...")
            demo_task_ids = ["task-pr-audit-101", "task-high-value-102", "task-dataset-curation-103"]
            demo_agent_ids = list(demo_agents_keys.keys())

            # Auditor safety guard: refuse reset if non-final requests exist outside mock mode
            if not settings.MOCK_PAYPAL:
                stmt_non_final = select(PayoutRequestModel).where(
                    PayoutRequestModel.task_id.in_(demo_task_ids),
                    PayoutRequestModel.status.not_in(TERMINAL_STATUSES)
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
                if is_prod and keys_output_path:
                    new_key = ag_info["api_key"]
                    existing.api_key_hash = hash_api_key(new_key)
                    seeded_agents.append((ag_info["agent_id"], new_key))
                    print(f"  [.] Agent key rotated: {ag_info['agent_id']}")
                elif not is_prod:
                    seeded_agents.append((ag_info["agent_id"], ag_info["api_key"]))
                    print(f"  [.] Agent already present: {ag_info['agent_id']} (reset to ACTIVE)")
                else:
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
                if is_prod:
                    status_name = existing.status.value if hasattr(existing.status, "value") else str(existing.status)
                    print(f"  [.] Task already present: {t_info['task_id']} (left as {status_name})")
                else:
                    existing.status = TaskStatus.OPEN
                    print(f"  [.] Task already present: {t_info['task_id']} (reset to OPEN)")

        await db.commit()

    if is_prod and keys_output_path:
        lines = []
        for aid, key in seeded_agents:
            var_name = aid.replace("-", "_").upper() + "_KEY"
            lines.append(f"{var_name}={key}\n")
        with open(keys_output_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
        print(f"Agent keys written to gitignored file: {keys_output_path}")

    print("\nDatabase seeding completed successfully!")
    if not is_prod:
        print(f"Operator Token: {settings.OPERATOR_TOKEN}")
        for aid, key in seeded_agents:
            print(f"Agent: {aid} | Key: {key}")
    else:
        print("Production environment active: Credentials generated securely without console logging.")
        if not keys_output_path:
            print("Note: In production, register agents via POST /api/v1/agents to retrieve single-use API keys.")
    if is_prod:
        print(f"Sandbox Recipient Email: {mask_recipient_email(recipient)}")
    else:
        print(f"Sandbox Recipient Email: {recipient}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed PayAgent Demo Data")
    parser.add_argument("--reset", action="store_true", help="Clear previous demo tasks, runs, and incidents before seeding")
    parser.add_argument("--recreate-schema", action="store_true", help="Drop and recreate all tables and enums before seeding (recovery command for schema drift)")
    parser.add_argument("--yes-drop-everything", action="store_true", help="Confirmation flag required for --recreate-schema")
    args = parser.parse_args()
    asyncio.run(
        seed_database(
            reset=args.reset,
            recreate_schema=args.recreate_schema,
            yes_drop_everything=args.yes_drop_everything,
        )
    )
