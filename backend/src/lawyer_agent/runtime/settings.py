from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]

_MAX_SECRET_FILE_BYTES = 65_536


class _SecretFileSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LAWYER_",
        env_file=".env",
        extra="ignore",
        hide_input_in_errors=True,
    )


class AIJobPublisherSettings(_SecretFileSettings):
    environment: Environment
    database_url: SecretStr = Field(repr=False, exclude=True)
    rabbit_amqp_url_file: Path
    message_private_seed_ring_file: Path = Field(repr=False, exclude=True)
    message_active_kid: str
    confirm_timeout_seconds: float = 5.0
    claim_ttl_seconds: int = 30
    batch_size: int = 50


class AIJobWorkerSettings(_SecretFileSettings):
    environment: Environment
    database_url: SecretStr = Field(repr=False, exclude=True)
    rabbit_amqp_url_file: Path
    message_public_verify_ring_file: Path = Field(repr=False, exclude=True)
    observability_reference_key_file: Path = Field(repr=False, exclude=True)
    prefetch: int = 8
    lease_seconds: int = 60
    heartbeat_seconds: int = 20


class AIJobTopologySettings(_SecretFileSettings):
    environment: Environment
    rabbit_amqp_url_file: Path
    rabbit_management_credential_file: Path = Field(repr=False, exclude=True)


class AIJobMaintenanceSettings(_SecretFileSettings):
    environment: Environment
    database_url: SecretStr = Field(repr=False, exclude=True)
    scan_batch_size: int = 100
    rejection_retention_days: int = 7


def read_secret_file(path: Path, *, field_name: str) -> str:
    """Validate and read a secret file (absolute, regular, bounded, UTF-8)."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute path")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"{field_name} cannot be read") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{field_name} must be a regular file")
    try:
        with path.open("rb") as stream:
            payload = stream.read(_MAX_SECRET_FILE_BYTES + 1)
    except OSError as exc:
        raise ValueError(f"{field_name} cannot be read") from exc
    if not payload or len(payload) > _MAX_SECRET_FILE_BYTES:
        raise ValueError(f"{field_name} has an invalid size")
    try:
        text = payload.decode("utf-8").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{field_name} must contain UTF-8 text") from exc
    if not text or "\x00" in text:
        raise ValueError(f"{field_name} has invalid content")
    return text


def read_json_secret_file(path: Path, *, field_name: str) -> dict[str, str]:
    raw = read_secret_file(path, field_name=field_name)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must contain valid JSON") from exc
    if not isinstance(parsed, dict) or any(
        not isinstance(key, str) or not isinstance(value, str) for key, value in parsed.items()
    ):
        raise ValueError(f"{field_name} must be a mapping of string to string")
    return parsed
