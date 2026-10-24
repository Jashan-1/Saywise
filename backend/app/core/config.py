from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """How the process runs. Client behavior lives in scope settings, never here."""

    model_config = SettingsConfigDict(
        env_prefix="SAYSO_", env_file=REPO_ROOT / ".env", extra="ignore"
    )

    database_url: str = "postgresql+asyncpg://sayso:sayso@localhost:5433/sayso"
    public_base_url: str = "http://localhost:8000"
    signing_secret: SecretStr
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_from_number: str = ""
    ai_provider: str = "anthropic"
    ai_model: str = ""
    ai_api_key: SecretStr = SecretStr("")
    log_level: str = "INFO"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_from: str = ""

    @field_validator("signing_secret")
    @classmethod
    def _long_enough(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value()) < 32:
            raise ValueError("SAYSO_SIGNING_SECRET must be at least 32 characters")
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
