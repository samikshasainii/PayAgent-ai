import os
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base
from src.config import settings

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

engine = create_async_engine(
    DATABASE_URL,
    echo=settings.DEBUG and False,  # Keep clean in production
    future=True,
)

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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
