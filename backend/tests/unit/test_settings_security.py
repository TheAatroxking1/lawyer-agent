from base64 import b64encode

import pytest
from pydantic import ValidationError

from lawyer_agent.application.identity import BlindIndexRolloutPhase
from lawyer_agent.config import (
    DEVELOPMENT_BLIND_INDEX_KEY_B64,
    DEVELOPMENT_CSRF_KEY_B64,
    DEVELOPMENT_DATA_ENCRYPTION_KEY_B64,
    DEVELOPMENT_ED25519_KEY_RING,
    DEVELOPMENT_REFRESH_TOKEN_KEY_B64,
    Settings,
)


def encoded_key(byte: int) -> str:
    return b64encode(bytes([byte]) * 32).decode("ascii")


def deployment_settings(environment: str = "production", **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": environment,
        "secret_key": "p" * 32,
        "database_url": "mysql+asyncmy://app:password@mysql.example.cn:3306/lawyer_agent",
        "redis_url": "redis://redis.example.cn:6379/0",
        "data_encryption_key_b64": encoded_key(11),
        "blind_index_key_b64": encoded_key(12),
        "blind_index_rollout_phase": "legacy-compatible",
        "blind_index_legacy_key_version": 1,
        "refresh_token_key_b64": encoded_key(13),
        "csrf_key_b64": encoded_key(14),
        "jwt_ed25519_key_ring": {"active": "configured-outside-source-control"},
        "trusted_origins": ("https://app.example.cn",),
        "cookie_secure": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_rejects_shared_or_missing_security_keys() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", secret_key="x" * 32)

    with pytest.raises(ValidationError, match="must be distinct"):
        deployment_settings(refresh_token_key_b64=encoded_key(11))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trusted_origins", ("http://app.example.cn",)),
        ("trusted_origins", ("https://*.example.cn",)),
        ("cookie_secure", False),
    ],
)
def test_production_rejects_insecure_cookie_or_origin(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        deployment_settings(**{field: value})


def test_production_accepts_explicit_distinct_security_configuration() -> None:
    settings = deployment_settings()

    assert settings.cookie_secure is True
    assert settings.trusted_origins == ("https://app.example.cn",)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployment_requires_explicit_blind_index_rollout_configuration(
    environment: str,
) -> None:
    with pytest.raises(ValidationError, match="rollout phase and legacy key version"):
        deployment_settings(
            environment,
            blind_index_rollout_phase=None,
            blind_index_legacy_key_version=None,
        )


def test_rotation_ready_requires_explicit_legacy_writer_drain_ack() -> None:
    with pytest.raises(ValidationError, match="drained acknowledgement"):
        deployment_settings(
            blind_index_rollout_phase="rotation-ready",
            blind_index_legacy_key_version=7,
            blind_index_legacy_writers_drained=False,
        )


def test_local_rollout_defaults_are_safe_and_strongly_typed() -> None:
    policy = Settings(environment="test").identity_rollout_policy()

    assert policy.phase is BlindIndexRolloutPhase.LEGACY_COMPATIBLE
    assert policy.legacy_key_version == 1
    assert policy.legacy_writers_drained is False


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "field, development_value",
    [
        ("data_encryption_key_b64", DEVELOPMENT_DATA_ENCRYPTION_KEY_B64),
        ("blind_index_key_b64", DEVELOPMENT_BLIND_INDEX_KEY_B64),
        ("refresh_token_key_b64", DEVELOPMENT_REFRESH_TOKEN_KEY_B64),
        ("csrf_key_b64", DEVELOPMENT_CSRF_KEY_B64),
        (
            "jwt_ed25519_key_ring",
            {"rotated-kid": DEVELOPMENT_ED25519_KEY_RING["development"]},
        ),
    ],
)
def test_deployment_rejects_each_development_key(
    environment: str, field: str, development_value: object
) -> None:
    with pytest.raises(ValidationError):
        deployment_settings(environment, **{field: development_value})


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "field, development_value",
    [
        ("database_url", "mysql+asyncmy://lawyer:lawyer@mysql:3306/lawyer_agent"),
        ("redis_url", "redis://redis:6379/0"),
    ],
)
def test_deployment_rejects_development_infrastructure_urls(
    environment: str, field: str, development_value: str
) -> None:
    with pytest.raises(ValidationError):
        deployment_settings(environment, **{field: development_value})


@pytest.mark.parametrize(
    "origin",
    [
        "https://app.example.cn/path",
        "https://app.example.cn?next=dashboard",
        "https://app.example.cn#section",
        "https://user:password@app.example.cn",
    ],
)
def test_deployment_rejects_non_origin_trusted_origin_values(origin: str) -> None:
    with pytest.raises(ValidationError):
        deployment_settings(trusted_origins=(origin,))


def test_deployment_normalizes_and_deduplicates_trusted_origins() -> None:
    settings = deployment_settings(
        trusted_origins=("https://APP.example.cn/", "https://app.example.cn")
    )

    assert settings.trusted_origins == ("https://app.example.cn",)
