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
from lawyer_agent.infrastructure.redis.client import SecurityRedisTopology


def encoded_key(byte: int) -> str:
    return b64encode(bytes([byte]) * 32).decode("ascii")


def deployment_values(environment: str = "production") -> dict[str, object]:
    values: dict[str, object] = {
        "environment": environment,
        "secret_key": "p" * 32,
        "database_url": "mysql+asyncmy://app:password@mysql.example.cn:3306/lawyer_agent",
        "redis_url": "redis://redis.example.cn:6379/0",
        "redis_security_topology": "standalone",
        "data_encryption_key_ring": {7: encoded_key(11)},
        "data_encryption_active_key_version": 7,
        "blind_index_key_ring": {7: encoded_key(12)},
        "blind_index_active_key_version": 7,
        "blind_index_rollout_phase": "legacy-compatible",
        "blind_index_legacy_key_version": 7,
        "refresh_token_key_b64": encoded_key(13),
        "csrf_key_b64": encoded_key(14),
        "jwt_active_kid": "active",
        "jwt_ed25519_key_ring": {"active": encoded_key(15)},
        "trusted_origins": ("https://app.example.cn",),
        "cookie_secure": True,
    }
    return values


def deployment_settings(environment: str = "production", **overrides: object) -> Settings:
    values = deployment_values(environment)
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
    assert settings.decoded_data_encryption_key_ring() == {7: bytes([11]) * 32}
    assert settings.decoded_blind_index_key_ring() == {7: bytes([12]) * 32}


@pytest.mark.parametrize(
    ("field", "active_field"),
    [
        ("data_encryption_key_ring", "data_encryption_active_key_version"),
        ("blind_index_key_ring", "blind_index_active_key_version"),
    ],
)
@pytest.mark.parametrize(
    "ring",
    [
        {0: encoded_key(21)},
        {32768: encoded_key(21)},
        {True: encoded_key(21)},
        {7: "not-base64"},
        {7: b64encode(b"a" * 31).decode("ascii")},
        {7: encoded_key(21), 8: encoded_key(21)},
    ],
)
def test_versioned_security_key_rings_reject_invalid_versions_material_and_duplicates(
    field: str,
    active_field: str,
    ring: dict[int, str],
) -> None:
    with pytest.raises(ValidationError, match="key ring|version|Base64|32 bytes|repeat"):
        Settings(
            environment="test",
            **{field: ring, active_field: next(iter(ring))},
        )


@pytest.mark.parametrize(
    ("field", "active_field"),
    [
        ("data_encryption_key_ring", "data_encryption_active_key_version"),
        ("blind_index_key_ring", "blind_index_active_key_version"),
    ],
)
def test_versioned_security_key_ring_requires_configured_active_version(
    field: str,
    active_field: str,
) -> None:
    with pytest.raises(ValidationError, match="active.*version"):
        Settings(
            environment="test",
            **{field: {7: encoded_key(21)}, active_field: 8},
        )


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployment_requires_explicit_versioned_cipher_and_blind_index_rings(
    environment: str,
) -> None:
    values = deployment_values(environment)
    values.pop("data_encryption_key_ring")
    values.pop("data_encryption_active_key_version")

    with pytest.raises(ValidationError, match="versioned.*key ring"):
        Settings(**values)


def test_blind_index_rollout_legacy_version_must_remain_in_the_ring() -> None:
    with pytest.raises(ValidationError, match="legacy.*key ring"):
        deployment_settings(
            blind_index_key_ring={8: encoded_key(12)},
            blind_index_active_key_version=8,
            blind_index_legacy_key_version=7,
        )


def test_versioned_key_rings_parse_from_environment_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAWYER_DATA_ENCRYPTION_KEY_RING", '{"7":"' + encoded_key(21) + '"}')
    monkeypatch.setenv("LAWYER_DATA_ENCRYPTION_ACTIVE_KEY_VERSION", "7")
    monkeypatch.setenv("LAWYER_BLIND_INDEX_KEY_RING", '{"8":"' + encoded_key(22) + '"}')
    monkeypatch.setenv("LAWYER_BLIND_INDEX_ACTIVE_KEY_VERSION", "8")

    settings = Settings(environment="test")

    assert settings.decoded_data_encryption_key_ring() == {7: bytes([21]) * 32}
    assert settings.decoded_blind_index_key_ring() == {8: bytes([22]) * 32}


@pytest.mark.parametrize(
    "key_ring",
    [
        {"bad kid": encoded_key(15)},
        {"active": "not-base64"},
        {"active": b64encode(b"a" * 31).decode("ascii")},
        {"active": b64encode(b"a" * 33).decode("ascii")},
        {"first": encoded_key(15), "second": encoded_key(15)},
    ],
)
def test_jwt_seed_ring_requires_unique_base64_rfc8032_seeds(
    key_ring: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="JWT|jwt|Ed25519|32 bytes|repeat"):
        Settings(
            environment="test",
            jwt_active_kid=next(iter(key_ring)),
            jwt_ed25519_key_ring=key_ring,
        )


def test_multi_key_jwt_ring_never_guesses_the_active_kid() -> None:
    for ring in (
        {"old": encoded_key(15), "new": encoded_key(16)},
        {"development": encoded_key(15), "new": encoded_key(16)},
    ):
        with pytest.raises(ValidationError, match="jwt_active_kid"):
            Settings(environment="test", jwt_ed25519_key_ring=ring)


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployment_requires_explicit_active_jwt_kid(environment: str) -> None:
    values = deployment_values(environment)
    values.pop("jwt_active_kid")

    with pytest.raises(ValidationError, match="explicit.*jwt_active_kid"):
        Settings(**values)


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
    settings = Settings(environment="test")
    policy = settings.identity_rollout_policy()

    assert policy.phase is BlindIndexRolloutPhase.LEGACY_COMPATIBLE
    assert policy.legacy_key_version == 1
    assert policy.legacy_writers_drained is False
    assert settings.redis_security_topology is SecurityRedisTopology.STANDALONE


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_deployment_requires_explicit_security_redis_topology(
    environment: str,
) -> None:
    with pytest.raises(ValidationError, match="Redis topology"):
        deployment_settings(environment, redis_security_topology=None)


@pytest.mark.parametrize(
    "raw_topology", ["cluster", "sharded", "unknown", True, ["cluster"]]
)
def test_security_redis_topology_rejects_cluster_and_unknown_raw_values(
    raw_topology: object,
) -> None:
    with pytest.raises(ValidationError):
        Settings(environment="development", redis_security_topology=raw_topology)


def test_production_accepts_explicit_sentinel_primary_topology() -> None:
    settings = deployment_settings(redis_security_topology="sentinel-primary")

    assert settings.redis_security_topology is SecurityRedisTopology.SENTINEL_PRIMARY


@pytest.mark.parametrize("environment", ["staging", "production"])
@pytest.mark.parametrize(
    "field, development_value",
    [
        (
            "data_encryption_key_ring",
            {7: DEVELOPMENT_DATA_ENCRYPTION_KEY_B64},
        ),
        (
            "blind_index_key_ring",
            {7: DEVELOPMENT_BLIND_INDEX_KEY_B64},
        ),
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


@pytest.mark.parametrize(
    "overrides",
    [
        {"jwt_ed25519_key_ring": {"active": encoded_key(11)}},
        {"jwt_ed25519_key_ring": {"active": encoded_key(12)}},
        {"jwt_ed25519_key_ring": {"active": encoded_key(13)}},
        {"jwt_ed25519_key_ring": {"active": encoded_key(14)}},
        {"secret_key": encoded_key(11)},
        {"secret_key": encoded_key(12)},
        {"secret_key": encoded_key(13)},
        {"secret_key": encoded_key(14)},
        {"secret_key": encoded_key(15)},
    ],
)
def test_production_rejects_jwt_and_application_secret_key_reuse_across_purposes(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ValidationError, match="must be distinct"):
        deployment_settings(**overrides)


@pytest.mark.parametrize(
    "ring",
    [
        {"01": encoded_key(21)},
        {"+1": encoded_key(21)},
        {" 1": encoded_key(21)},
        {"1": encoded_key(21), 1: encoded_key(22)},
    ],
)
def test_versioned_key_ring_rejects_noncanonical_or_colliding_version_keys(
    ring: dict[object, str],
) -> None:
    with pytest.raises(ValidationError, match="canonical|duplicate|version"):
        Settings(
            environment="test",
            data_encryption_key_ring=ring,
            data_encryption_active_key_version=1,
        )


def test_environment_json_rejects_semantically_duplicate_version_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "LAWYER_DATA_ENCRYPTION_KEY_RING",
        '{"1":"' + encoded_key(21) + '","01":"' + encoded_key(22) + '"}',
    )
    monkeypatch.setenv("LAWYER_DATA_ENCRYPTION_ACTIVE_KEY_VERSION", "1")

    with pytest.raises(ValidationError, match="canonical|duplicate"):
        Settings(environment="test")


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("jwt_ed25519_key_ring", {"active": "JWT-SUPER-SECRET-NOT-BASE64"}),
        ("data_encryption_key_ring", {7: "DATA-SUPER-SECRET-NOT-BASE64"}),
        ("blind_index_key_ring", {7: "BLIND-SUPER-SECRET-NOT-BASE64"}),
        ("refresh_token_key_b64", "REFRESH-SUPER-SECRET-NOT-BASE64"),
        ("csrf_key_b64", "CSRF-SUPER-SECRET-NOT-BASE64"),
        ("secret_key", "SHORT-SUPER-SECRET"),
    ],
)
def test_security_settings_validation_errors_hide_input_material(
    field: str,
    invalid_value: object,
) -> None:
    exposed_fragment = next(iter(invalid_value.values())) if isinstance(
        invalid_value, dict
    ) else invalid_value

    with pytest.raises(ValidationError) as raised:
        Settings(environment="test", **{field: invalid_value})

    assert exposed_fragment not in str(raised.value)
    assert exposed_fragment not in repr(raised.value)


def test_security_settings_repr_and_default_dump_hide_key_material() -> None:
    settings = deployment_settings()
    rendered = repr(settings)
    dumped = settings.model_dump()

    for material in (
        settings.secret_key,
        *settings.data_encryption_key_ring.values(),
        *settings.blind_index_key_ring.values(),
        settings.refresh_token_key_b64,
        settings.csrf_key_b64,
        *settings.jwt_ed25519_key_ring.values(),
    ):
        assert material not in rendered
        assert material not in dumped.values()
