from base64 import b64decode
from binascii import Error as BinasciiError
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEVELOPMENT_SECRET = "development-only-change-before-exposure"  # noqa: S105
DEVELOPMENT_DATA_ENCRYPTION_KEY_B64 = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
DEVELOPMENT_BLIND_INDEX_KEY_B64 = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="
DEVELOPMENT_REFRESH_TOKEN_KEY_B64 = "AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM="  # noqa: S105
DEVELOPMENT_CSRF_KEY_B64 = "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ="
DEVELOPMENT_ED25519_KEY_RING = {"development": "development-only-ed25519-key"}


def _decode_32_byte_key(value: str, field_name: str) -> bytes:
    try:
        decoded = b64decode(value, validate=True)
    except BinasciiError as exc:
        raise ValueError(f"{field_name} must be valid Base64") from exc
    if len(decoded) != 32:
        raise ValueError(f"{field_name} must decode to exactly 32 bytes")
    return decoded


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAWYER_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "staging", "production"] = "development"
    secret_key: str = Field(default=DEVELOPMENT_SECRET, min_length=32)
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    database_url: str = "mysql+asyncmy://lawyer:lawyer@mysql:3306/lawyer_agent"
    redis_url: str = "redis://redis:6379/0"
    database_pool_size: int = Field(default=10, gt=0)
    database_max_overflow: int = Field(default=20, ge=0)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    trusted_origins: tuple[str, ...] = ("http://localhost:5173",)
    cookie_secure: bool = False
    data_encryption_key_b64: str = DEVELOPMENT_DATA_ENCRYPTION_KEY_B64
    blind_index_key_b64: str = DEVELOPMENT_BLIND_INDEX_KEY_B64
    refresh_token_key_b64: str = DEVELOPMENT_REFRESH_TOKEN_KEY_B64
    csrf_key_b64: str = DEVELOPMENT_CSRF_KEY_B64
    jwt_ed25519_key_ring: dict[str, str] = Field(
        default_factory=lambda: DEVELOPMENT_ED25519_KEY_RING.copy()
    )

    @field_validator(
        "data_encryption_key_b64",
        "blind_index_key_b64",
        "refresh_token_key_b64",
        "csrf_key_b64",
    )
    @classmethod
    def validate_base64_security_key(cls, value: str, info: ValidationInfo) -> str:
        _decode_32_byte_key(value, info.field_name or "security key")
        return value

    @field_validator("trusted_origins")
    @classmethod
    def validate_trusted_origins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("trusted_origins must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("trusted_origins must not contain duplicates")
        return value

    @field_validator("jwt_ed25519_key_ring")
    @classmethod
    def validate_ed25519_key_ring(cls, value: dict[str, str]) -> dict[str, str]:
        if not value:
            raise ValueError("jwt_ed25519_key_ring must not be empty")
        if any(not key.strip() or not material.strip() for key, material in value.items()):
            raise ValueError("jwt_ed25519_key_ring entries must not be blank")
        if len(set(value.values())) != len(value):
            raise ValueError("jwt_ed25519_key_ring must not repeat key material")
        return value

    @model_validator(mode="after")
    def reject_development_secret_outside_local_environments(self) -> "Settings":
        if self.environment not in {"staging", "production"}:
            return self

        if self.secret_key == DEVELOPMENT_SECRET:
            raise ValueError("LAWYER_SECRET_KEY must be replaced outside development and test")

        encoded_keys = (
            self.data_encryption_key_b64,
            self.blind_index_key_b64,
            self.refresh_token_key_b64,
            self.csrf_key_b64,
        )
        decoded_keys = tuple(
            _decode_32_byte_key(value, "production security key") for value in encoded_keys
        )
        if len(set(decoded_keys)) != len(decoded_keys):
            raise ValueError("production security keys must be distinct")
        if encoded_keys == (
            DEVELOPMENT_DATA_ENCRYPTION_KEY_B64,
            DEVELOPMENT_BLIND_INDEX_KEY_B64,
            DEVELOPMENT_REFRESH_TOKEN_KEY_B64,
            DEVELOPMENT_CSRF_KEY_B64,
        ):
            raise ValueError("production security keys must be configured explicitly")
        if self.jwt_ed25519_key_ring == DEVELOPMENT_ED25519_KEY_RING:
            raise ValueError("production Ed25519 key ring must be configured explicitly")
        if not self.cookie_secure:
            raise ValueError("cookie_secure must be enabled outside development and test")
        for origin in self.trusted_origins:
            parsed = urlparse(origin)
            if "*" in origin or parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("production trusted_origins must be explicit HTTPS origins")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
