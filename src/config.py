import os
from typing import Optional
from decimal import Decimal
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Application
    APP_ENV: str = "development"
    DEBUG: bool = True
    DEMO_MODE: bool = True
    APP_PORT: int = 8000
    PORT: int = 8000
    APP_HOST: str = "0.0.0.0"
    HOST: str = "0.0.0.0"
    PUBLIC_HOST: Optional[str] = None

    # Database
    DATABASE_URL: Optional[str] = "sqlite+aiosqlite:///./data/processed/payagent.db"

    # Auth
    OPERATOR_TOKEN: str = "payagent-operator-demo-secret"

    # Policy & Spend Caps
    MAX_SINGLE_TASK_AUTO_APPROVE: Decimal = Decimal("50.00")
    MAX_AGENT_HOURLY_VELOCITY: Decimal = Decimal("200.00")
    MIN_LLM_CONFIDENCE: float = 0.75
    CIRCUIT_BREAKER_INCIDENTS: int = 3
    CIRCUIT_BREAKER_INCIDENT_THRESHOLD: int = 3
    CIRCUIT_BREAKER_WINDOW_MINUTES: int = 10
    CIRCUIT_BREAKER_WINDOW_SECONDS: int = 600

    # PayPal Integration
    PAYPAL_CLIENT_ID: str = "mock-client-id"
    PAYPAL_CLIENT_SECRET: str = "mock-client-secret"
    PAYPAL_MODE: str = "sandbox"
    PAYPAL_BASE_URL: str = "https://api-m.sandbox.paypal.com"
    PAYPAL_WEBHOOK_ID: Optional[str] = None
    SANDBOX_RECIPIENT_EMAIL: Optional[str] = None
    MOCK_PAYPAL: bool = False

    # Poller
    POLLER_ENABLED: bool = True

    # LLM (Gate 2): any OpenAI-compatible endpoint; defaults to Gemini's.
    # For OpenAI, set LLM_BASE_URL=https://api.openai.com/v1 and LLM_MODEL=gpt-4o-mini.
    GEMINI_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    LLM_API_KEY: Optional[str] = None
    LLM_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    LLM_MODEL: str = "gemini-3.5-flash-lite"
    LLM_TIMEOUT_SECONDS: int = 45
    MOCK_LLM: bool = False

    @model_validator(mode="after")
    def align_settings(self):
        if self.APP_ENV == "test" and "MOCK_LLM" not in os.environ:
            self.MOCK_LLM = True
        # Sync APP_PORT / PORT
        if self.APP_PORT != 8000 and self.PORT == 8000:
            self.PORT = self.APP_PORT
        elif self.PORT != 8000 and self.APP_PORT == 8000:
            self.APP_PORT = self.PORT

        # Sync APP_HOST / HOST
        if self.APP_HOST != "0.0.0.0" and self.HOST == "0.0.0.0":
            self.HOST = self.APP_HOST
        elif self.HOST != "0.0.0.0" and self.APP_HOST == "0.0.0.0":
            self.APP_HOST = self.HOST

        # Resolve LLM API key for the configured provider (never send one vendor's key to another)
        if not self.LLM_API_KEY:
            if "googleapis.com" in self.LLM_BASE_URL:
                self.LLM_API_KEY = self.GEMINI_API_KEY
            else:
                self.LLM_API_KEY = self.OPENAI_API_KEY

        # Sync Circuit breaker params
        if self.CIRCUIT_BREAKER_WINDOW_MINUTES != 10 and self.CIRCUIT_BREAKER_WINDOW_SECONDS == 600:
            self.CIRCUIT_BREAKER_WINDOW_SECONDS = self.CIRCUIT_BREAKER_WINDOW_MINUTES * 60
        elif self.CIRCUIT_BREAKER_WINDOW_SECONDS != 600 and self.CIRCUIT_BREAKER_WINDOW_MINUTES == 10:
            self.CIRCUIT_BREAKER_WINDOW_MINUTES = max(1, self.CIRCUIT_BREAKER_WINDOW_SECONDS // 60)

        if self.CIRCUIT_BREAKER_INCIDENTS != 3 and self.CIRCUIT_BREAKER_INCIDENT_THRESHOLD == 3:
            self.CIRCUIT_BREAKER_INCIDENT_THRESHOLD = self.CIRCUIT_BREAKER_INCIDENTS
        elif self.CIRCUIT_BREAKER_INCIDENT_THRESHOLD != 3 and self.CIRCUIT_BREAKER_INCIDENTS == 3:
            self.CIRCUIT_BREAKER_INCIDENTS = self.CIRCUIT_BREAKER_INCIDENT_THRESHOLD

        # Validate production security
        if self.APP_ENV == "production":
            insecure_defaults = {"payagent-operator-demo-secret", "change-me-demo-operator-token"}
            if not self.OPERATOR_TOKEN or self.OPERATOR_TOKEN in insecure_defaults:
                raise ValueError("OPERATOR_TOKEN must be securely set in production environment")

        return self

settings = Settings()
