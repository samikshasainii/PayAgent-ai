import logging
import os
from typing import AsyncGenerator
import sqlalchemy
from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from sqlalchemy.pool import NullPool
from src.config import settings

logger = logging.getLogger(__name__)

def get_database_url() -> str:
    url = settings.DATABASE_URL or "sqlite+aiosqlite:///./data/processed/payagent.db"
    if url.startswith("sqlite+aiosqlite:///"):
        sqlite_file = url.replace("sqlite+aiosqlite:///", "", 1)
        dir_name = os.path.dirname(sqlite_file)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        return url
    # Normalize postgres url if provided without asyncpg driver
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+asyncpg://", 1)
    elif url.startswith("postgresql://") and not url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url

DATABASE_URL = get_database_url()

engine_kwargs = {
    "echo": settings.DEBUG and False,  # Keep clean in production
    "future": True,
}
if settings.APP_ENV == "test":
    # pytest-asyncio gives each test its own event loop; pooled asyncpg connections are bound to the loop that opened them
    engine_kwargs["poolclass"] = NullPool

engine = create_async_engine(
    DATABASE_URL,
    **engine_kwargs,
)

# Milestone 2 SQLite concurrency hardening:
# Configure WAL journal mode and busy_timeout to prevent 'database is locked' errors during PayPal network wait
if "sqlite" in DATABASE_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

Base = declarative_base()

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()

async def init_db():
    # Register every model on Base.metadata; otherwise create_all and the schema guard below see nothing
    import src.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        def _verify_columns(sync_conn):
            inspector = inspect(sync_conn)
            existing_tables = set(inspector.get_table_names())
            missing = []
            for table_name, table in Base.metadata.tables.items():
                if table_name not in existing_tables:
                    missing.append((table_name, "<entire table>"))
                    continue
                live_cols = {col["name"] for col in inspector.get_columns(table_name)}
                for col in table.columns:
                    if col.name not in live_cols:
                        missing.append((table_name, col.name))

            if sync_conn.dialect.name == "postgresql" and hasattr(inspector, "get_enums"):
                db_enums = {e["name"].lower(): set(e["labels"]) for e in inspector.get_enums()}
                for table in Base.metadata.tables.values():
                    for col in table.columns:
                        if isinstance(col.type, sqlalchemy.Enum):
                            enum_name = (col.type.name or "").lower()
                            if enum_name not in db_enums:
                                missing.append((f"enum '{enum_name}'", "missing type"))
                                continue
                            existing_labels = db_enums[enum_name]
                            for label in col.type.enums:
                                if label not in existing_labels:
                                    missing.append((f"enum '{enum_name}'", f"missing label '{label}'"))
            return missing

        missing = await conn.run_sync(_verify_columns)
        if missing:
            for item1, item2 in missing:
                desc = f"{item1} {item2}" if item1.startswith("enum ") else f"table='{item1}', column='{item2}'"
                logger.error(f"Live database schema mismatch: {desc}")
            if settings.APP_ENV == "production":
                missing_str = ", ".join(
                    f"{t} {c}" if t.startswith("enum ") else f"{t}.{c}"
                    for t, c in missing
                )
                raise RuntimeError(
                    f"Startup schema check failed in production: {missing_str}. "
                    "Schema is frozen (ARCHITECTURE.md section 4.4); recover with "
                    "'python -m src.seed --recreate-schema --yes-drop-everything'."
                )
