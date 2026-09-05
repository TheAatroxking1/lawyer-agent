import json
import re
import stat
from base64 import b64decode
from binascii import Error as BinasciiError
from functools import lru_cache
from pathlib import Path
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
DEVELOPMENT_ED25519_KEY_RING = {
    "development": "BQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQU="
}
DEVELOPMENT_DATABASE_URL = "mysql+asyncmy://lawyer:lawyer@mysql:3306/lawyer_agent"
DEVELOPMENT_REDIS_URL = "redis://redis:6379/0"
_JWT_KID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,128}\Z", re.ASCII)
_MAX_SECRET_FILE_BYTES = 65_536
_SECURITY_SECRET_FILES = {
    "secret_key_file": ("secret_key", False),
    "data_encryption_key_b64_file": ("data_encryption_key_b64", False),
    "data_encryption_key_ring_file": ("data_encryption_key_ring", True),
    "blind_index_key_b64_file": ("blind_index_key_b64", False),
    "blind_index_key_ring_file": ("blind_index_key_ring", True),
    "refresh_token_key_b64_file": ("refresh_token_key_b64", False),
    "csrf_key_b64_file": ("csrf_key_b64", False),
    "jwt_ed25519_key_ring_file": ("jwt_ed25519_key_ring", True),
}


def _reject_duplicate_json_object_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError("security key ring JSON contains a duplicate key")
        parsed[key] = value
    return parsed


def _decode_32_byte_key(value: str, field_name: str) -> bytes:
    try:
        decoded = b64decode(value, validate=True)
    except BinasciiError as exc:
        raise ValueError(f"{field_name} must be valid Base64") from exc
    if len(decoded) != 32:
        raise ValueError(f"{field_name} must decode to exactly 32 bytes")
    return decoded


def _validate_versioned_key_ring(
    value: dict[int, str] | None,
    field_name: str,
) -> dict[int, str] | None:
    if value is None:
        return None
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    decoded: list[bytes] = []
    for version, material in value.items():
        if type(version) is not int or not 1 <= version <= 32767:
            raise ValueError(f"{field_name} versions must be from 1 to 32767")
        decoded.append(_decode_32_byte_key(material, field_name))
    if len(set(decoded)) != len(decoded):
        raise ValueError(f"{field_name} must not repeat decoded key material")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LAWYER_",
        env_file=".env",
        extra="ignore",
        validate_default=True,
        hide_input_in_errors=True,
    )

    environment: Literal["development", "test", "staging", "production"] = "development"
    secret_key: str = Field(
        default=DEVELOPMENT_SECRET,
        min_length=32,
        repr=False,
        exclude=True,
    )
    secret_key_file: Path | None = Field(default=None, repr=False, exclude=True)
    api_prefix: str = "/api/v1"
    log_level: str = "INFO"
    database_url: str = Field(default=DEVELOPMENT_DATABASE_URL, repr=False, exclude=True)
    redis_url: str = Field(default=DEVELOPMENT_REDIS_URL, repr=False, exclude=True)
    redis_key_prefix: str = Field(default="lawyer:", pattern=r"^[a-z0-9][a-z0-9:-]{0,62}:$")
    redis_security_topology: SecurityRedisTopology = SecurityRedisTopology.STANDALONE
    database_pool_size: int = Field(default=10, gt=0)
    database_max_overflow: int = Field(default=20, ge=0)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    trusted_origins: tuple[str, ...] = ("http://localhost:5173",)
    cookie_secure: bool = False
    data_encryption_key_b64: str = Field(
        default=DEVELOPMENT_DATA_ENCRYPTION_KEY_B64,
        repr=False,
        exclude=True,
    )
    data_encryption_key_b64_file: Path | None = Field(
        default=None, repr=False, exclude=True
    )
    data_encryption_key_ring: dict[int, str] | None = Field(
        default=None,
        repr=False,
        exclude=True,
    )
    data_encryption_key_ring_file: Path | None = Field(
        default=None, repr=False, exclude=True
    )
    data_encryption_active_key_version: int | None = None
    blind_index_key_b64: str = Field(
        default=DEVELOPMENT_BLIND_INDEX_KEY_B64,
        repr=False,
        exclude=True,
    )
    blind_index_key_b64_file: Path | None = Field(
        default=None, repr=False, exclude=True
    )
    blind_index_key_ring: dict[int, str] | None = Field(
        default=None,
        repr=False,
        exclude=True,
    )
    blind_index_key_ring_file: Path | None = Field(
        default=None, repr=False, exclude=True
    )
    blind_index_active_key_version: int | None = None
    blind_index_rollout_phase: Literal[
        "legacy-compatible", "rotation-ready"
    ] | None = None
    blind_index_legacy_key_version: int | None = None
    blind_index_legacy_writers_drained: bool = False
    refresh_token_key_b64: str = Field(
        default=DEVELOPMENT_REFRESH_TOKEN_KEY_B64,
        repr=False,
        exclude=True,
    )
    refresh_token_key_b64_file: Path | None = Field(
        default=None, repr=False, exclude=True
    )
    csrf_key_b64: str = Field(
        default=DEVELOPMENT_CSRF_KEY_B64,
        repr=False,
        exclude=True,
    )
    csrf_key_b64_file: Path | None = Field(default=None, repr=False, exclude=True)
    jwt_issuer: str = "https://identity.lawyer-agent.local"
    jwt_active_kid: str = "development"
    jwt_ed25519_key_ring: dict[str, str] = Field(
        default_factory=lambda: DEVELOPMENT_ED25519_KEY_RING.copy(),
        repr=False,
        exclude=True,
    )
    jwt_ed25519_key_ring_file: Path | None = Field(
        default=None, repr=False, exclude=True
    )
    deepseek_api_key: str | None = Field(default=None, repr=False, exclude=True)
    deepseek_api_key_file: Path | None = Field(
        default=None, repr=False, exclude=True
    )

    @model_validator(mode="before")
    @classmethod
    def load_security_secret_files(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        loaded = dict(value)
        for file_field, (target_field, parse_json) in _SECURITY_SECRET_FILES.items():
            raw_path = loaded.get(file_field)
            if raw_path is None:
                continue
            if target_field in loaded:
                raise ValueError(
                    f"{target_field} and {file_field} cannot both be configured"
                )
            path = Path(raw_path)
            if not path.is_absolute():
                raise ValueError(f"{file_field} must be an absolute path")
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError(f"{file_field} cannot be read") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(f"{file_field} must be a regular file")
            try:
                with path.open("rb") as secret_stream:
                    payload = secret_stream.read(_MAX_SECRET_FILE_BYTES + 1)
            except OSError as exc:
                raise ValueError(f"{file_field} cannot be read") from exc
            if not payload or len(payload) > _MAX_SECRET_FILE_BYTES:
                raise ValueError(f"{file_field} has an invalid size")
            try:
                text_value = payload.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise ValueError(f"{file_field} must contain UTF-8 text") from exc
            if not text_value or "\x00" in text_value:
                raise ValueError(f"{file_field} has invalid content")
            if parse_json:
                try:
                    loaded[target_field] = json.loads(
                        text_value,
                        object_pairs_hook=_reject_duplicate_json_object_pairs,
                    )
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{file_field} must contain valid JSON") from exc
            else:
                loaded[target_field] = text_value
        raw_deepseek_file = loaded.get("deepseek_api_key_file")
        if raw_deepseek_file is not None:
            if loaded.get("deepseek_api_key") is not None:
                raise ValueError(
                    "deepseek_api_key and deepseek_api_key_file cannot both be configured"
                )
            path = Path(raw_deepseek_file)
            if not path.is_absolute():
                raise ValueError("deepseek_api_key_file must be an absolute path")
            try:
                metadata = path.stat(follow_symlinks=False)
            except OSError as exc:
                raise ValueError("deepseek_api_key_file cannot be read") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("deepseek_api_key_file must be a regular file")
            try:
                with path.open("rb") as secret_stream:
                    payload = secret_stream.read(_MAX_SECRET_FILE_BYTES + 1)
            except OSError as exc:
                raise ValueError("deepseek_api_key_file cannot be read") from exc
            if not payload or len(payload) > _MAX_SECRET_FILE_BYTES:
                raise ValueError("deepseek_api_key_file has an invalid size")
            try:
                text_value = payload.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError as exc:
                raise ValueError("deepseek_api_key_file must contain UTF-8 text") from exc
            if not text_value or "\x00" in text_value:
                raise ValueError("deepseek_api_key_file has invalid content")
            try:
                parsed = json.loads(
                    text_value,
                    object_pairs_hook=_reject_duplicate_json_object_pairs,
                )
            except json.JSONDecodeError as exc:
                raise ValueError("deepseek_api_key_file must contain valid JSON") from exc
            if not isinstance(parsed, dict) or set(parsed) != {"api_key"}:
                raise ValueError(
                    "deepseek_api_key_file must be a JSON object with a single api_key field"
                )
            if not isinstance(parsed["api_key"], str):
                raise ValueError("deepseek_api_key_file api_key must be text")
            loaded["deepseek_api_key"] = parsed["api_key"]
        return loaded

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

    @field_validator(
        "data_encryption_key_ring",
        "blind_index_key_ring",
        mode="before",
    )
    @classmethod
    def normalize_versioned_key_ring(cls, value: object) -> object:
        if value is None or not isinstance(value, dict):
            return value
        normalized: dict[int, object] = {}
        for raw_version, material in value.items():
            if isinstance(raw_version, bool):
                raise ValueError("security key ring versions must be integers")
            if isinstance(raw_version, int):
                version = raw_version
            elif (
                isinstance(raw_version, str)
                and raw_version.isascii()
                and raw_version.isdigit()
            ):
                version = int(raw_version)
                if str(version) != raw_version:
                    raise ValueError(
                        "security key ring versions must use canonical decimal strings"
                    )
            else:
                raise ValueError("security key ring versions must be integers")
            if version in normalized:
                raise ValueError("security key ring contains a duplicate version")
            normalized[version] = material
        return normalized

    @field_validator("data_encryption_key_ring", "blind_index_key_ring")
    @classmethod
    def validate_versioned_key_ring(
        cls,
        value: dict[int, str] | None,
        info: ValidationInfo,
    ) -> dict[int, str] | None:
        return _validate_versioned_key_ring(value, info.field_name or "security key ring")

    @field_validator(
        "data_encryption_active_key_version",
        "blind_index_active_key_version",
        mode="before",
    )
    @classmethod
    def reject_boolean_active_key_version(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("active key version must be an integer")
        return value

    @field_validator(
        "data_encryption_active_key_version",
        "blind_index_active_key_version",
    )
    @classmethod
    def validate_active_key_version(
        cls,
        value: int | None,
        info: ValidationInfo,
    ) -> int | None:
        if value is not None and (type(value) is not int or not 1 <= value <= 32767):
            raise ValueError(f"{info.field_name} must be from 1 to 32767")
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
        if any(
            _JWT_KID_PATTERN.fullmatch(key) is None or not material.strip()
            for key, material in value.items()
        ):
            raise ValueError("jwt_ed25519_key_ring contains an invalid JWT kid or blank seed")
        decoded = tuple(
            _decode_32_byte_key(material, "JWT Ed25519 seed")
            for material in value.values()
        )
        if len(set(decoded)) != len(decoded):
            raise ValueError("jwt_ed25519_key_ring must not repeat decoded key material")
        return value

    @field_validator("deepseek_api_key")
    @classmethod
    def normalize_deepseek_api_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("deepseek_api_key must be text")
        stripped = value.strip()
        return stripped if stripped else None

    @model_validator(mode="after")
    def validate_active_jwt_key(self) -> "Settings":
        if (
            len(self.jwt_ed25519_key_ring) > 1
            and "jwt_active_kid" not in self.model_fields_set
        ):
            raise ValueError("multi-key JWT rings require an explicit jwt_active_kid")
        if (
            self.environment in {"staging", "production"}
            and "jwt_active_kid" not in self.model_fields_set
        ):
            raise ValueError(
                "staging and production require an explicit jwt_active_kid"
            )
        if (
            self.jwt_active_kid == "development"
            and self.jwt_active_kid not in self.jwt_ed25519_key_ring
            and len(self.jwt_ed25519_key_ring) == 1
            and self.environment in {"development", "test"}
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

    @model_validator(mode="after")
    def validate_versioned_key_ring_actives(self) -> "Settings":
        pairs = (
            (
                "data-encryption key ring",
                self.data_encryption_key_ring,
                self.data_encryption_active_key_version,
            ),
            (
                "blind-index key ring",
                self.blind_index_key_ring,
                self.blind_index_active_key_version,
            ),
        )
        for name, ring, active_version in pairs:
            if (ring is None) != (active_version is None):
                raise ValueError(f"{name} and active key version must be configured together")
            if ring is not None and active_version not in ring:
                raise ValueError(f"{name} active key version is not configured")
        if (
            self.blind_index_key_ring is not None
            and self.blind_index_legacy_key_version is not None
            and self.blind_index_legacy_key_version not in self.blind_index_key_ring
        ):
            raise ValueError("blind-index legacy key version must remain in the key ring")
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
        if self.environment in {"staging", "production"} and not {
            "data_encryption_key_ring",
            "data_encryption_active_key_version",
            "blind_index_key_ring",
            "blind_index_active_key_version",
        }.issubset(self.model_fields_set):
            raise ValueError(
                "staging and production require explicit versioned security key rings "
                "and active versions"
            )
        self.identity_rollout_policy()
        if self.environment not in {"staging", "production"}:
            return self

        if self.secret_key == DEVELOPMENT_SECRET:
            raise ValueError("LAWYER_SECRET_KEY must be replaced outside development and test")

        decoded_keys = (
            tuple(self.decoded_data_encryption_key_ring().values())
            + tuple(self.decoded_blind_index_key_ring().values())
            + tuple(
                _decode_32_byte_key(material, "deployment JWT seed")
                for material in self.jwt_ed25519_key_ring.values()
            )
            + (
            _decode_32_byte_key(self.refresh_token_key_b64, "deployment security key"),
            _decode_32_byte_key(self.csrf_key_b64, "deployment security key"),
                self.secret_key.encode("utf-8"),
            )
        )
        try:
            decoded_application_secret = b64decode(self.secret_key, validate=True)
        except BinasciiError:
            decoded_application_secret = b""
        if len(decoded_application_secret) == 32:
            decoded_keys += (decoded_application_secret,)
        if len(set(decoded_keys)) != len(decoded_keys):
            raise ValueError("deployment security keys must be distinct")
        development_keys = {
            _decode_32_byte_key(DEVELOPMENT_DATA_ENCRYPTION_KEY_B64, "development key"),
            _decode_32_byte_key(DEVELOPMENT_BLIND_INDEX_KEY_B64, "development key"),
            _decode_32_byte_key(DEVELOPMENT_REFRESH_TOKEN_KEY_B64, "development key"),
            _decode_32_byte_key(DEVELOPMENT_CSRF_KEY_B64, "development key"),
            _decode_32_byte_key(
                DEVELOPMENT_ED25519_KEY_RING["development"],
                "development key",
            ),
        }
        if any(key in development_keys for key in decoded_keys):
            raise ValueError("deployment security keys must replace every development key")
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

    def decoded_data_encryption_key_ring(self) -> dict[int, bytes]:
        if self.data_encryption_key_ring is None:
            return {
                1: _decode_32_byte_key(
                    self.data_encryption_key_b64,
                    "data_encryption_key_b64",
                )
            }
        return {
            version: _decode_32_byte_key(material, "data_encryption_key_ring")
            for version, material in self.data_encryption_key_ring.items()
        }

    def decoded_blind_index_key_ring(self) -> dict[int, bytes]:
        if self.blind_index_key_ring is None:
            return {
                1: _decode_32_byte_key(
                    self.blind_index_key_b64,
                    "blind_index_key_b64",
                )
            }
        return {
            version: _decode_32_byte_key(material, "blind_index_key_ring")
            for version, material in self.blind_index_key_ring.items()
        }

    @property
    def effective_data_encryption_active_key_version(self) -> int:
        return self.data_encryption_active_key_version or 1

    @property
    def effective_blind_index_active_key_version(self) -> int:
        return self.blind_index_active_key_version or 1


@lru_cache
def get_settings() -> Settings:
    return Settings()
