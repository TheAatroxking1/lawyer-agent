from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
from base64 import b64encode
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from time import monotonic
from typing import Any

import pytest
from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import ApplicationServices
from lawyer_agent.config import Settings
from lawyer_agent.infrastructure.redis.client import RedisAsyncioAdapter
from lawyer_agent.main import create_app

FORBIDDEN = {
    "refresh_token",
    "password_hash",
    "blind_index",
    "ciphertext",
    "auth_version",
    "authz_version",
    "key_version",
}
EXPECTED_IDENTITY_PATHS = {
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/auth/reauth",
    "/api/v1/auth/switch-tenant",
    "/api/v1/me",
    "/api/v1/me/tenants",
    "/api/v1/tenants",
    "/api/v1/tenants/{tenant_id}",
    "/api/v1/tenants/{tenant_id}/members",
    "/api/v1/tenants/{tenant_id}/invitations",
    "/api/v1/invitations/accept",
    "/api/v1/tenants/{tenant_id}/members/{membership_id}",
    "/api/v1/platform/tenant-applications",
    "/api/v1/platform/tenant-applications/{tenant_id}/approve",
    "/api/v1/platform/tenant-applications/{tenant_id}/reject",
}
REPOSITORY_ROOT = Path(__file__).parents[3]


def _application_services(readiness: Any) -> ApplicationServices:
    placeholder = object()
    return ApplicationServices(
        identity=placeholder,
        sessions=placeholder,
        tenancy=placeholder,
        invitations=placeholder,
        platform=placeholder,
        rate_limiter=placeholder,
        step_up=placeholder,
        csrf=placeholder,
        token_service=placeholder,
        accounts=placeholder,  # type: ignore[arg-type]
        readiness=readiness,
    )


def _client(readiness: Any) -> TestClient:
    @asynccontextmanager
    async def service_factory(_settings: Settings) -> AsyncIterator[ApplicationServices]:
        yield _application_services(readiness)

    settings = Settings(environment="test", secret_key="x" * 32)
    return TestClient(create_app(settings, service_factory=service_factory))


def _openapi() -> dict[str, Any]:
    settings = Settings(environment="test", secret_key="x" * 32)
    return create_app(settings).openapi()


def test_openapi_has_no_internal_security_fields() -> None:
    document = _openapi()
    serialized = json.dumps(document, ensure_ascii=False).lower()

    assert all(name not in serialized for name in FORBIDDEN)
    assert EXPECTED_IDENTITY_PATHS <= document["paths"].keys()


def test_ready_runs_mysql_and_redis_checks_concurrently() -> None:
    from lawyer_agent.api.router import ConcurrentReadinessProbe

    started: set[str] = set()
    both_started = asyncio.Event()

    async def check(name: str) -> None:
        started.add(name)
        if len(started) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=0.1)

    readiness = ConcurrentReadinessProbe(
        checks=(lambda: check("mysql"), lambda: check("redis")),
        timeout_seconds=0.2,
    )
    with _client(readiness) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert started == {"mysql", "redis"}


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_ready_rejects_an_unbounded_deadline(timeout: float) -> None:
    from lawyer_agent.api.router import ConcurrentReadinessProbe

    async def check() -> None:
        return None

    with pytest.raises(ValueError, match="timeout"):
        ConcurrentReadinessProbe(checks=(check,), timeout_seconds=timeout)


async def test_redis_readiness_uses_the_ping_command() -> None:
    class RedisSdk:
        def __init__(self) -> None:
            self.ping_calls = 0

        async def ping(self) -> bool:
            self.ping_calls += 1
            return True

    sdk = RedisSdk()
    adapter = RedisAsyncioAdapter(sdk)  # type: ignore[arg-type]

    await adapter.ping()

    assert sdk.ping_calls == 1


def test_ready_is_bounded_and_sanitizes_dependency_failure() -> None:
    from lawyer_agent.api.router import ConcurrentReadinessProbe

    leaked = (
        "mysql redis mysql+asyncmy://user:password@database/internal "
        "redis://token@cache/0 secret exception"
    )

    async def fail() -> None:
        raise RuntimeError(leaked)

    async def hang() -> None:
        await asyncio.sleep(10)

    for check in (fail, hang):
        readiness = ConcurrentReadinessProbe(checks=(check,), timeout_seconds=0.03)
        started_at = monotonic()
        with _client(readiness) as client:
            response = client.get("/health/ready")
        elapsed = monotonic() - started_at

        assert response.status_code == 503
        assert response.headers["content-type"].startswith("application/problem+json")
        assert elapsed < 1
        serialized = json.dumps(response.json(), ensure_ascii=False).lower()
        assert all(fragment not in serialized for fragment in leaked.lower().split())
        assert response.json()["code"] == "service_not_ready"


def test_compose_wires_app_dependencies_and_read_only_secret_files() -> None:
    import yaml

    compose = yaml.safe_load((REPOSITORY_ROOT / "deploy/compose.yaml").read_text("utf-8"))
    api = compose["services"]["api"]
    environment = api["environment"]

    assert environment["LAWYER_DATABASE_URL"] == "${LAWYER_DATABASE_URL}"
    assert environment["LAWYER_REDIS_URL"] == "${LAWYER_REDIS_URL:-redis://redis:6379/0}"
    assert api["read_only"] is True
    assert "/health/ready" in " ".join(api["healthcheck"]["test"])

    mounted = {item["target"]: item for item in api["secrets"]}
    file_variables = {
        name: value
        for name, value in environment.items()
        if name.startswith("LAWYER_") and name.endswith("_FILE")
    }
    assert {
        "LAWYER_SECRET_KEY_FILE",
        "LAWYER_DATA_ENCRYPTION_KEY_RING_FILE",
        "LAWYER_BLIND_INDEX_KEY_RING_FILE",
        "LAWYER_REFRESH_TOKEN_KEY_B64_FILE",
        "LAWYER_CSRF_KEY_B64_FILE",
        "LAWYER_JWT_ED25519_KEY_RING_FILE",
    } <= file_variables.keys()
    assert {
        "LAWYER_DATA_ENCRYPTION_ACTIVE_KEY_VERSION",
        "LAWYER_BLIND_INDEX_ACTIVE_KEY_VERSION",
        "LAWYER_JWT_ACTIVE_KID",
        "LAWYER_TRUSTED_ORIGINS",
        "LAWYER_COOKIE_SECURE",
        "LAWYER_JWT_ISSUER",
    } <= environment.keys()
    for path in file_variables.values():
        assert path.startswith("/run/secrets/")
        target = path.removeprefix("/run/secrets/")
        assert set(mounted[target]) == {"source", "target"}
        secret_source = mounted[target]["source"]
        assert compose["secrets"][secret_source] == {"file": f"./secrets/{target}"}

    gitignore = (REPOSITORY_ROOT / ".gitignore").read_text("utf-8")
    dev_script = (REPOSITORY_ROOT / "scripts/dev.ps1").read_text("utf-8")
    example = (REPOSITORY_ROOT / "deploy/compose.env.example").read_text("utf-8")
    assert "deploy/secrets/" in gitignore
    assert all(target in dev_script for target in mounted)
    assert "LAWYER_DATABASE_URL=mysql+asyncmy://lawyer:" in example
    assert "@mysql:3306/lawyer_agent" in example
    assert "LAWYER_DATA_ENCRYPTION_KEY_B64=" in example
    assert "LAWYER_BLIND_INDEX_KEY_B64=" in example
    assert "LAWYER_JWT_ACTIVE_KID=" in example
    production_example = (
        REPOSITORY_ROOT / "deploy/compose.production.env.example"
    ).read_text("utf-8")
    assert "LAWYER_ENVIRONMENT=production" in production_example
    assert "never store key material here" in production_example
    assert "lawyer_data_encryption_key_ring" in production_example
    assert "lawyer_blind_index_key_ring" in production_example


def test_compose_security_shape_can_construct_production_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yaml

    compose = yaml.safe_load((REPOSITORY_ROOT / "deploy/compose.yaml").read_text("utf-8"))
    environment = compose["services"]["api"]["environment"]
    required = {
        "LAWYER_ENVIRONMENT",
        "LAWYER_DATABASE_URL",
        "LAWYER_REDIS_URL",
        "LAWYER_REDIS_SECURITY_TOPOLOGY",
        "LAWYER_BLIND_INDEX_ROLLOUT_PHASE",
        "LAWYER_BLIND_INDEX_LEGACY_KEY_VERSION",
        "LAWYER_BLIND_INDEX_LEGACY_WRITERS_DRAINED",
        "LAWYER_DATA_ENCRYPTION_ACTIVE_KEY_VERSION",
        "LAWYER_BLIND_INDEX_ACTIVE_KEY_VERSION",
        "LAWYER_JWT_ACTIVE_KID",
        "LAWYER_TRUSTED_ORIGINS",
        "LAWYER_COOKIE_SECURE",
        "LAWYER_JWT_ISSUER",
    }
    assert required <= environment.keys()

    def material(byte: int) -> str:
        return b64encode(bytes([byte]) * 32).decode("ascii")

    secret_values = {
        "secret_key_file": "s" * 32,
        "data_encryption_key_ring_file": json.dumps({"2609": material(11)}),
        "blind_index_key_ring_file": json.dumps(
            {"2609": material(12), "2610": material(16)}
        ),
        "refresh_token_key_b64_file": material(13),
        "csrf_key_b64_file": material(14),
        "jwt_ed25519_key_ring_file": json.dumps({"release-2026-09": material(15)}),
    }
    file_values: dict[str, Path] = {}
    for field, value in secret_values.items():
        path = tmp_path / field
        path.write_text(value, encoding="utf-8")
        file_values[field] = path

    production_example = (
        REPOSITORY_ROOT / "deploy/compose.production.env.example"
    ).read_text("utf-8")
    deployed_values = {
        name.strip(): value
        for line in production_example.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
        for name, separator, value in (line.partition("="),)
        if separator
    }
    deployed_values.update(
        {
            "LAWYER_SECRET_KEY_FILE": str(file_values["secret_key_file"]),
            "LAWYER_DATA_ENCRYPTION_KEY_RING_FILE": str(
                file_values["data_encryption_key_ring_file"]
            ),
            "LAWYER_BLIND_INDEX_KEY_RING_FILE": str(
                file_values["blind_index_key_ring_file"]
            ),
            "LAWYER_REFRESH_TOKEN_KEY_B64_FILE": str(
                file_values["refresh_token_key_b64_file"]
            ),
            "LAWYER_CSRF_KEY_B64_FILE": str(file_values["csrf_key_b64_file"]),
            "LAWYER_JWT_ED25519_KEY_RING_FILE": str(
                file_values["jwt_ed25519_key_ring_file"]
            ),
        }
    )
    assert deployed_values.keys() <= environment.keys()
    for name in tuple(os.environ):
        if name.startswith("LAWYER_"):
            monkeypatch.delenv(name, raising=False)
    for name, value in deployed_values.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.environment == "production"
    assert settings.data_encryption_active_key_version == 2609
    assert settings.blind_index_active_key_version == 2610
    assert settings.jwt_active_kid == "release-2026-09"


def test_development_loads_single_security_keys_from_secret_files(tmp_path: Path) -> None:
    def material(byte: int) -> str:
        return b64encode(bytes([byte]) * 32).decode("ascii")

    values = {
        "secret_key": "s" * 32,
        "data_encryption_key_b64": material(11),
        "blind_index_key_b64": material(12),
        "refresh_token_key_b64": material(13),
        "csrf_key_b64": material(14),
        "jwt_ed25519_key_ring": json.dumps({"mounted": material(15)}),
    }
    paths: dict[str, Path] = {}
    for field, value in values.items():
        path = tmp_path / field
        path.write_text(value + "\n", encoding="utf-8")
        paths[f"{field}_file"] = path

    settings = Settings(environment="development", **paths)  # type: ignore[arg-type]

    assert settings.secret_key == values["secret_key"]
    assert settings.data_encryption_key_b64 == values["data_encryption_key_b64"]
    assert settings.blind_index_key_b64 == values["blind_index_key_b64"]
    assert settings.refresh_token_key_b64 == values["refresh_token_key_b64"]
    assert settings.csrf_key_b64 == values["csrf_key_b64"]
    assert settings.jwt_ed25519_key_ring == {"mounted": material(15)}
    assert settings.jwt_active_kid == "mounted"


def test_migration_is_explicit_and_script_does_not_echo_configuration() -> None:
    script = (REPOSITORY_ROOT / "scripts/migrate.ps1").read_text("utf-8")
    main = (REPOSITORY_ROOT / "backend/src/lawyer_agent/main.py").read_text("utf-8")
    alembic_environment = (
        REPOSITORY_ROOT / "backend/alembic/env.py"
    ).read_text("utf-8")

    assert re.search(r"uv\s+run\s+alembic\s+upgrade\s+head", script)
    assert "EnvironmentFile" in script
    assert "LAWYER_DATABASE_URL" in script
    assert not re.search(r"write-(host|output)|echo|get-childitem\s+env:", script, re.I)
    assert "alembic" not in main.lower()
    assert "LAWYER_DATABASE_URL" in alembic_environment
    assert "unused@127.0.0.1" not in (
        REPOSITORY_ROOT / "backend/alembic.ini"
    ).read_text("utf-8")
    assert "print(" not in alembic_environment


def test_migration_script_fails_closed_without_a_database_url() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("PowerShell is required to verify scripts/migrate.ps1")
    child_environment = os.environ.copy()
    child_environment.pop("LAWYER_DATABASE_URL", None)

    completed = subprocess.run(  # noqa: S603 - executable is resolved locally
        [
            executable,
            "-NoProfile",
            "-File",
            str(REPOSITORY_ROOT / "scripts/migrate.ps1"),
        ],
        cwd=REPOSITORY_ROOT,
        env=child_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    combined_output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "LAWYER_DATABASE_URL is required" in combined_output
    assert "mysql+" not in combined_output


def test_pending_production_reviews_are_explicitly_unverified() -> None:
    review = (
        REPOSITORY_ROOT / "docs/project-decisions/pending-production-reviews.md"
    ).read_text("utf-8")
    required = ("Permission", "Argon2", "p95", "短信", "邮件", "微信", "Vault/KMS", "审计保留")

    assert all(term.lower() in review.lower() for term in required)
    assert "尚未验证" in review
    assert "不削弱" in review
