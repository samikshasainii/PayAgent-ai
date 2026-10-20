import os
from typing import Optional
from decimal import Decimal
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    APP_ENV: str = "development"
    DEBUG: bool = True
    DEMO_MODE: bool = True
    PORT: int = 8000
    HOST: str = "0.0.0.0"

    # Database
    DATABASE_URL: Optional[str] = None  # If None, defaults to sqlite+aiosqlite:///./data/payagent.db

    # Auth
    OPERATOR_TOKEN: str = "payagent-operator-demo-secret"

    # Policy & Spend Caps
    MAX_SINGLE_TASK_AUTO_APPROVE: Decimal = Decimal("50.00")
    MAX_AGENT_HOURLY_VELOCITY: Decimal = Decimal("200.00")
    MIN_LLM_CONFIDENCE: float = 0.75
    CIRCUIT_BREAKER_INCIDENT_THRESHOLD: int = 3
    CIRCUIT_BREAKER_WINDOW_SECONDS: int = 600

    # PayPal Integration
    PAYPAL_CLIENT_ID: str = "mock-client-id"
    PAYPAL_CLIENT_SECRET: str = "mock-client-secret"
    PAYPAL_BASE_URL: str = "https://api-m.sandbox.paypal.com"
    PAYPAL_WEBHOOK_ID: Optional[str] = None
    MOCK_PAYPAL: bool = True

    # LLM (Gate 2)
    LLM_API_KEY: Optional[str] = None
    LLM_MODEL: str = "gpt-4o-mini"

settings = Settings()
