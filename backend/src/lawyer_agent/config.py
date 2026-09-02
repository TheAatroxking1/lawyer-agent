from base64 import b64decode
from binascii import Error as BinasciiError
from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from lawyer_agent.application.identity import (
    BlindIndexRolloutPhase,
    BlindIndexRolloutPolicy,
)
from lawyer_agent.domain.origins import HttpOrigin
from lawyer_agent.infrastructure.redis.client import SecurityRedisTopology

DEVELOPMENT_SECRET = "development-only-change-before-exposure"  # noqa: S105
DEVELOPMENT_DATA_ENCRYPTION_KEY_B64 = "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="
DEVELOPMENT_BLIND_INDEX_KEY_B64 = "AgICAgICAgICAgICAgICAgICAgICAgICAgICAgICAgI="
DEVELOPMENT_REFRESH_TOKEN_KEY_B64 = "AwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwM="  # noqa: S105
DEVELOPMENT_CSRF_KEY_B64 = "BAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ="
DEVELOPMENT_ED25519_KEY_RING = {"development": "development-only-ed25519-key"}
DEVELOPMENT_DATABASE_URL = "mysql+asyncmy://lawyer:lawyer@mysql:3306/lawyer_agent"
DEVELOPMENT_REDIS_URL = "redis://redis:6379/0"


def _decode_32_byte_key(value: str, field_name: str) -> bytes:
    try:
        decoded = b64decode(value, validate=True)
    except BinasciiError as exc:
        raise ValueError(f"{field_name} must be valid Base64") from exc
    if len(decoded) != 32:
        raise ValueError(f"{field_name} must decode to exactly 32 bytes")
    return decoded


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LAWYER_", env_file=".env", extra="ignore", validate_default=True
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    secret_key: str = Field(default=DEVELOPMENT_SECRET, min_length=32)
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    database_url: str = DEVELOPMENT_DATABASE_URL
    redis_url: str = DEVELOPMENT_REDIS_URL
    redis_key_prefix: str = Field(default="lawyer:", pattern=r"^[a-z0-9][a-z0-9:-]{0,62}:$")
    redis_security_topology: SecurityRedisTopology = SecurityRedisTopology.STANDALONE
    database_pool_size: int = Field(default=10, gt=0)
    database_max_overflow: int = Field(default=20, ge=0)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    trusted_origins: tuple[str, ...] = ("http://localhost:5173",)
    cookie_secure: bool = False
    data_encryption_key_b64: str = DEVELOPMENT_DATA_ENCRYPTION_KEY_B64
    blind_index_key_b64: str = DEVELOPMENT_BLIND_INDEX_KEY_B64
    blind_index_rollout_phase: Literal[
        "legacy-compatible", "rotation-ready"
    ] | None = None
    blind_index_legacy_key_version: int | None = None
    blind_index_legacy_writers_drained: bool = False
    refresh_token_key_b64: str = DEVELOPMENT_REFRESH_TOKEN_KEY_B64
    csrf_key_b64: str = DEVELOPMENT_CSRF_KEY_B64
    jwt_issuer: str = "https://identity.lawyer-agent.local"
    jwt_active_kid: str = "development"
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
        return tuple(
            dict.fromkeys(HttpOrigin.parse(origin).value for origin in value)
        )

    @field_validator("redis_security_topology", mode="before")
    @classmethod
    def validate_security_redis_topology(cls, value: object) -> object:
        if value not in (
            SecurityRedisTopology.STANDALONE,
            SecurityRedisTopology.SENTINEL_PRIMARY,
        ):
            raise ValueError(
                "Redis topology must be standalone or a Sentinel-resolved primary"
            )
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
    def validate_active_jwt_key(self) -> "Settings":
        if (
            self.jwt_active_kid == "development"
            and self.jwt_active_kid not in self.jwt_ed25519_key_ring
            and len(self.jwt_ed25519_key_ring) == 1
        ):
            # A single externally configured key is unambiguous. Rotation rings
            # still require an explicit active kid and therefore fail closed.
            self.jwt_active_kid = next(iter(self.jwt_ed25519_key_ring))
        if self.jwt_active_kid not in self.jwt_ed25519_key_ring:
            raise ValueError("jwt_active_kid must identify a configured signing key")
        parsed = urlparse(self.jwt_issuer)
        if parsed.scheme != "https" or not parsed.netloc or "*" in self.jwt_issuer:
            raise ValueError("jwt_issuer must be an explicit HTTPS origin")
        return self

    @field_validator("blind_index_legacy_key_version")
    @classmethod
    def validate_legacy_blind_index_version(cls, value: int | None) -> int | None:
        if value is None:
            return value
        if isinstance(value, bool) or not 1 <= value <= 32767:
            raise ValueError("blind_index_legacy_key_version must be from 1 to 32767")
        return value

    @model_validator(mode="after")
    def reject_development_secret_outside_local_environments(self) -> "Settings":
        if (
            self.environment in {"staging", "production"}
            and "redis_security_topology" not in self.model_fields_set
        ):
            raise ValueError(
                "staging and production require an explicit security Redis topology"
            )
        if self.environment in {"staging", "production"} and (
            self.blind_index_rollout_phase is None
            or self.blind_index_legacy_key_version is None
        ):
            raise ValueError(
                "staging and production require an explicit blind-index rollout phase "
                "and legacy key version"
            )
        self.identity_rollout_policy()
        if self.environment not in {"staging", "production"}:
            return self

        if self.secret_key == DEVELOPMENT_SECRET:
            raise ValueError("LAWYER_SECRET_KEY must be replaced outside development and test")

        encoded_key_pairs = (
            (self.data_encryption_key_b64, DEVELOPMENT_DATA_ENCRYPTION_KEY_B64),
            (self.blind_index_key_b64, DEVELOPMENT_BLIND_INDEX_KEY_B64),
            (self.refresh_token_key_b64, DEVELOPMENT_REFRESH_TOKEN_KEY_B64),
            (self.csrf_key_b64, DEVELOPMENT_CSRF_KEY_B64),
        )
        decoded_keys = tuple(
            _decode_32_byte_key(value, "deployment security key")
            for value, _ in encoded_key_pairs
        )
        if len(set(decoded_keys)) != len(decoded_keys):
            raise ValueError("deployment security keys must be distinct")
        if any(
            key == _decode_32_byte_key(default, "development security key")
            for key, (_, default) in zip(decoded_keys, encoded_key_pairs, strict=True)
        ):
            raise ValueError("deployment security keys must replace every development key")
        if any(
            material in DEVELOPMENT_ED25519_KEY_RING.values()
            for material in self.jwt_ed25519_key_ring.values()
        ):
            raise ValueError("deployment Ed25519 key ring must replace development key material")
        if self.database_url == DEVELOPMENT_DATABASE_URL:
            raise ValueError("database_url must be replaced outside development and test")
        if self.redis_url == DEVELOPMENT_REDIS_URL:
            raise ValueError("redis_url must be replaced outside development and test")
        if not self.cookie_secure:
            raise ValueError("cookie_secure must be enabled outside development and test")
        for origin in self.trusted_origins:
            parsed = urlparse(origin)
            if "*" in origin or parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("production trusted_origins must be explicit HTTPS origins")
        return self

    def identity_rollout_policy(self) -> BlindIndexRolloutPolicy:
        phase = BlindIndexRolloutPhase(
            self.blind_index_rollout_phase
            or BlindIndexRolloutPhase.LEGACY_COMPATIBLE.value
        )
        legacy_key_version = (
            1
            if self.blind_index_legacy_key_version is None
            else self.blind_index_legacy_key_version
        )
        return BlindIndexRolloutPolicy(
            phase=phase,
            legacy_key_version=legacy_key_version,
            legacy_writers_drained=self.blind_index_legacy_writers_drained,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
