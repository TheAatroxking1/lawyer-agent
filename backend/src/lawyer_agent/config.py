from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_SECRET = "development-only-change-before-exposure"  # noqa: S105


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAWYER_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    secret_key: str = Field(default=DEVELOPMENT_SECRET, min_length=32)
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def reject_development_secret_outside_local_environments(self) -> "Settings":
        if self.environment in {"staging", "production"} and self.secret_key == DEVELOPMENT_SECRET:
            raise ValueError("LAWYER_SECRET_KEY must be replaced outside development and test")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
