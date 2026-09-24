from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import ApplicationServices
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.main import create_app

ORIGIN = "http://localhost:5173"
REGISTER = {
    "username": "synthetic-lawyer",
    "password": "synthetic-password-0001",
    "display_name": "合成律师",
}


@dataclass(frozen=True)
class _Result:
    user_id: UUID | None = None
    session_id: UUID | None = None
    access_token: str = "synthetic-access-token"  # noqa: S105
    refresh_token: str = "R" * 43


class _Identity:
    user_id = new_uuid7()
    fail_authentication = False
    internal_error = False
    internal_value_error = False

    async def register(self, command: object, *, audit_context: object) -> _Result:
        del command, audit_context
        return _Result(user_id=self.user_id)

    async def authenticate(
        self, identifier: object, password: str, *, audit_context: object
    ) -> _Result | None:
        del identifier, password, audit_context
        if self.internal_error:
            raise RuntimeError("SELECT secret_ciphertext FROM auth_identities")
        if self.internal_value_error:
            raise ValueError("cipher key invariant exposed secret-material")
        return None if self.fail_authentication else _Result(user_id=self.user_id)


class _Sessions:
    def __init__(self, identity: _Identity) -> None:
        self.session_id = new_uuid7()
        self.user_id = identity.user_id
        self.revoked = False

    async def start(self, *, user_id: UUID, audit_context: object) -> _Result:
        del user_id, audit_context
        return _Result(session_id=self.session_id)

    async def refresh(self, raw_token: str, *, audit_context: object) -> _Result:
        del raw_token, audit_context
        return _Result(session_id=self.session_id, refresh_token="S" * 43)

    async def refresh_session_id(self, raw_token: str) -> UUID:
        del raw_token
        return self.session_id

    async def validate_access(self, encoded: str, *, audience: object) -> object:
        from lawyer_agent.application.sessions import InvalidSession
        from lawyer_agent.domain.sessions import Audience

        if encoded != "account-token" or audience is not Audience.ACCOUNT:
            raise InvalidSession
        return type(
            "Validated",
            (),
            {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "tenant_id": None,
                "membership_id": None,
            },
        )()

    async def revoke(self, session_id: UUID, *, reason: object, audit_context: object) -> None:
        del session_id, reason, audit_context
        self.revoked = True

    async def switch_tenant(self, command: object, *, audit_context: object) -> _Result:
        del audit_context
        assert command.session_id == self.session_id
        return _Result(session_id=self.session_id, refresh_token="W" * 43)

    async def tenant_access(self, command: object, *, audit_context: object) -> str:
        del audit_context
        assert command.session_id == self.session_id
        return "synthetic-tenant-token"


class _RateLimiter:
    deny = False
    fail = False

    async def consume(self, rule: object, dimensions: object) -> object:
        del rule, dimensions
        if self.fail:
            from lawyer_agent.infrastructure.redis.client import (
                RedisDependencyInvalidResponse,
            )

            raise RedisDependencyInvalidResponse
        return type(
            "Decision",
            (),
            {
                "allowed": not self.deny,
                "retry_after_seconds": 7 if self.deny else 0,
            },
        )()


class _Csrf:
    def issue(self, session_id: UUID) -> str:
        del session_id
        return "N" * 22 + "." + "M" * 43

    def verify(self, **values: object) -> None:
        from lawyer_agent.infrastructure.security.csrf import InvalidCsrfToken

        assert values["origin"] == ORIGIN
        if values["header_token"] != values["cookie_token"]:
            raise InvalidCsrfToken


class _TokenService:
    def verify(self, token: str, *, audience: object, now: datetime) -> object:
        from lawyer_agent.domain.sessions import Audience, InvalidToken

        if token != "account-token" or audience is not Audience.ACCOUNT:  # noqa: S105
            raise InvalidToken
        return type(
            "Claims",
            (),
            {
                "auth_version": 1,
                "issued_at": now,
                "user_id": _Identity.user_id,
                "session_id": new_uuid7(),
            },
        )()

    def issue_platform(self, claims: object) -> str:
        assert claims.tenant_id is None
        assert claims.membership_id is None
        assert claims.expires_at - claims.issued_at <= timedelta(minutes=5)
        return "synthetic-platform-access-token"


class _Platform:
    deny_exchange = False

    def __init__(self) -> None:
        self.last_permission: str | None = None

    async def authorize_access(self, actor: object, **values: object) -> object:
        if self.deny_exchange:
            from lawyer_agent.application.platform import PlatformAuthorizationDenied

            raise PlatformAuthorizationDenied("permission_denied")
        assert values["permission"] in {
            "tenant_application.read",
            "tenant_application.review",
        }
        self.last_permission = str(values["permission"])
        return actor


class _StepUp:
    def __init__(self) -> None:
        self.issue_calls = 0

    async def issue(self, **values: object) -> object:
        self.issue_calls += 1
        assert str(values["action"]).startswith("tenant_application.review:")
        return type("Grant", (), {"value": "G" * 43})()


class _Accounts:
    async def get(self, user_id: UUID) -> object:
        return type(
            "Account",
            (),
            {
                "id": user_id,
                "display_name": "合成律师",
                "status": "active",
                "created_at": datetime.now(UTC),
            },
        )()

    async def list_tenants(self, user_id: UUID) -> tuple[object, ...]:
        del user_id
        return ()


def _services() -> tuple[ApplicationServices, _Sessions]:
    identity = _Identity()
    sessions = _Sessions(identity)
    services = ApplicationServices(
        identity=identity,
        sessions=sessions,
        tenancy=object(),
        invitations=object(),
        platform=_Platform(),
        rate_limiter=_RateLimiter(),
        step_up=_StepUp(),
        csrf=_Csrf(),
        token_service=_TokenService(),
        accounts=_Accounts(),
    )
    return services, sessions


def _client() -> tuple[TestClient, _Sessions]:
    services, sessions = _services()

    @asynccontextmanager
    async def service_factory(settings: Settings):
        del settings
        yield services

    settings = Settings(environment="test", secret_key="x" * 32)
    return (
        TestClient(
            create_app(settings, service_factory=service_factory),
            base_url="https://testserver",
        ),
        sessions,
    )


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("__Host-lawyer_csrf")
    assert token is not None
    return {"Origin": ORIGIN, "X-CSRF-Token": token}


def test_tenant_access_keeps_account_cookies_and_returns_only_access_token() -> None:
    client, sessions = _client()
    with client:
        client.post("/api/v1/auth/register", json=REGISTER, headers={"Origin": ORIGIN})
        cookies = dict(client.cookies)
        response = client.post("/api/v1/auth/tenant-access", json={
            "tenant_id": str(new_uuid7()), "membership_id": str(new_uuid7()),
        }, headers={"Origin": ORIGIN, "Authorization": "Bearer account-token"})
        assert response.status_code == 200
        assert response.json() == {"access_token": "synthetic-tenant-token"}
        assert not response.headers.get_list("set-cookie")
        assert dict(client.cookies) == cookies
        assert sessions.revoked is False


def test_register_login_refresh_logout_contract() -> None:
    client, sessions = _client()
    with client:
        registered = client.post(
            "/api/v1/auth/register", json=REGISTER, headers={"Origin": ORIGIN}
        )
        assert registered.status_code == 201
        assert registered.json() == {
            "access_token": "synthetic-access-token",
            "token_type": "Bearer",
        }
        set_cookie = registered.headers.get_list("set-cookie")
        assert any(
            "__Host-lawyer_refresh=" in value and "HttpOnly" in value
            for value in set_cookie
        )
        assert any(
            "__Host-lawyer_csrf=" in value and "HttpOnly" not in value
            for value in set_cookie
        )
        assert all("Secure" in value and "Path=/" in value for value in set_cookie)
        assert all("Domain=" not in value for value in set_cookie)
        assert all("Max-Age=31536000" in value for value in set_cookie)

        refreshed = client.post("/api/v1/auth/refresh", headers=_csrf(client))
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] == "synthetic-access-token"  # noqa: S105

        logged_in = client.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={
                "kind": "username",
                "identifier": "synthetic-lawyer",
                "password": "synthetic-password-0001",
            },
        )
        assert logged_in.status_code == 200

        switched = client.post(
            "/api/v1/auth/switch-tenant",
            headers={"Authorization": "Bearer account-token", "Origin": ORIGIN},
            json={"tenant_id": str(new_uuid7()), "membership_id": str(new_uuid7())},
        )
        assert switched.status_code == 200

        logged_out = client.post("/api/v1/auth/logout", headers=_csrf(client))
        assert logged_out.status_code == 204
        assert sessions.revoked is True
        deleted = logged_out.headers.get_list("set-cookie")
        refresh_deletion = next(
            value for value in deleted if value.startswith("__Host-lawyer_refresh=")
        )
        csrf_deletion = next(
            value for value in deleted if value.startswith("__Host-lawyer_csrf=")
        )
        assert "HttpOnly" in refresh_deletion
        assert "HttpOnly" not in csrf_deletion
        for value in (refresh_deletion, csrf_deletion):
            assert "Max-Age=0" in value
            assert "Secure" in value
            assert "SameSite=lax" in value
            assert "Path=/" in value
            assert "Domain=" not in value


def test_account_projection_contracts_use_account_audience() -> None:
    client, _ = _client()
    with client:
        account = client.get(
            "/api/v1/me", headers={"Authorization": "Bearer account-token"}
        )
        tenants = client.get(
            "/api/v1/me/tenants", headers={"Authorization": "Bearer account-token"}
        )
    assert account.status_code == 200
    assert set(account.json()) == {"id", "display_name", "status", "created_at"}
    assert tenants.status_code == 200 and tenants.json() == {"items": []}


def test_cookie_issuers_require_an_exact_trusted_origin() -> None:
    client, _ = _client()
    with client:
        for headers in ({}, {"Origin": "https://evil.example"}, {"Origin": "null"}):
            response = client.post("/api/v1/auth/register", json=REGISTER, headers=headers)
            assert response.status_code == 403
            assert response.json()["code"] == "csrf_origin_rejected"


def test_register_boundary_rejects_normalized_blank_identity_and_display_name() -> None:
    client, _ = _client()
    with client:
        blank_username = client.post(
            "/api/v1/auth/register",
            headers={"Origin": ORIGIN},
            json={**REGISTER, "username": "   "},
        )
        blank_display_name = client.post(
            "/api/v1/auth/register",
            headers={"Origin": ORIGIN},
            json={**REGISTER, "display_name": "   "},
        )

    assert blank_username.status_code == 422
    assert blank_display_name.status_code == 422
    assert blank_username.json()["code"] == "request_validation_failed"


def test_reauth_platform_exchange_requires_all_authoritative_factors() -> None:
    client, _ = _client()
    body = {
        "kind": "username",
        "identifier": "synthetic-lawyer",
        "password": "synthetic-password-0001",
        "tenant_id": str(new_uuid7()),
        "action": "tenant_application.review:approve",
    }
    with client:
        response = client.post(
            "/api/v1/auth/reauth",
            headers={"Authorization": "Bearer account-token", "Origin": ORIGIN},
            json=body,
        )
    assert response.status_code == 200
    assert response.json() == {
        "access_token": "synthetic-platform-access-token",
        "token_type": "Bearer",
        "step_up_grant": "G" * 43,
    }


def test_reauth_read_exchange_issues_platform_token_without_step_up_grant() -> None:
    services, _ = _services()

    @asynccontextmanager
    async def service_factory(settings: Settings):
        del settings
        yield services

    client = TestClient(
        create_app(
            Settings(environment="test", secret_key="x" * 32),
            service_factory=service_factory,
        )
    )
    with client:
        response = client.post(
            "/api/v1/auth/reauth",
            headers={"Authorization": "Bearer account-token", "Origin": ORIGIN},
            json={
                "kind": "username",
                "identifier": "synthetic-lawyer",
                "password": "synthetic-password-0001",
                "action": "tenant_application.read",
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "access_token": "synthetic-platform-access-token",
        "token_type": "Bearer",
    }
    assert services.platform.last_permission == "tenant_application.read"
    assert services.step_up.issue_calls == 0


def test_reauth_rejects_read_tenant_binding_and_unbound_review_confusion() -> None:
    client, _ = _client()
    common = {
        "kind": "username",
        "identifier": "synthetic-lawyer",
        "password": "synthetic-password-0001",
    }
    with client:
        bound_read = client.post(
            "/api/v1/auth/reauth",
            headers={"Authorization": "Bearer account-token", "Origin": ORIGIN},
            json={
                **common,
                "tenant_id": str(new_uuid7()),
                "action": "tenant_application.read",
            },
        )
        unbound_review = client.post(
            "/api/v1/auth/reauth",
            headers={"Authorization": "Bearer account-token", "Origin": ORIGIN},
            json={**common, "action": "tenant_application.review:approve"},
        )

    assert bound_read.status_code == 422
    assert unbound_review.status_code == 422
    assert bound_read.json()["code"] == "reauth_request_invalid"
    assert unbound_review.json()["code"] == "reauth_request_invalid"


def test_reauth_read_exchange_rejects_tenant_audience_token() -> None:
    client, _ = _client()
    with client:
        response = client.post(
            "/api/v1/auth/reauth",
            headers={"Authorization": "Bearer tenant-token", "Origin": ORIGIN},
            json={
                "kind": "username",
                "identifier": "synthetic-lawyer",
                "password": "synthetic-password-0001",
                "action": "tenant_application.read",
            },
        )

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_failed"


def test_reauth_does_not_upgrade_a_user_without_authoritative_platform_permission() -> None:
    services, _ = _services()
    services.platform.deny_exchange = True

    @asynccontextmanager
    async def service_factory(settings: Settings):
        del settings
        yield services

    client = TestClient(
        create_app(
            Settings(environment="test", secret_key="x" * 32),
            service_factory=service_factory,
        )
    )
    with client:
        response = client.post(
            "/api/v1/auth/reauth",
            headers={"Authorization": "Bearer account-token", "Origin": ORIGIN},
            json={
                "kind": "username",
                "identifier": "synthetic-lawyer",
                "password": "synthetic-password-0001",
                "tenant_id": str(new_uuid7()),
                "action": "tenant_application.review:approve",
            },
        )
    assert response.status_code == 403
    assert response.json()["code"] == "authorization_denied"


def test_refresh_rejects_missing_or_mismatched_double_submit_csrf() -> None:
    client, _ = _client()
    with client:
        assert client.post(
            "/api/v1/auth/register", json=REGISTER, headers={"Origin": ORIGIN}
        ).status_code == 201
        response = client.post(
            "/api/v1/auth/refresh",
            headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "csrf_validation_failed"


def test_authentication_errors_are_uniform_and_do_not_echo_credentials() -> None:
    services, _ = _services()
    services.identity.fail_authentication = True

    @asynccontextmanager
    async def service_factory(settings: Settings):
        del settings
        yield services

    client = TestClient(
        create_app(
            Settings(environment="test", secret_key="x" * 32),
            service_factory=service_factory,
        )
    )
    with client:
        responses = [
            client.post(
                "/api/v1/auth/login",
                headers={"Origin": ORIGIN},
                json={
                    "kind": "username",
                    "identifier": identifier,
                    "password": "wrong-password-material",
                },
            )
            for identifier in ("missing-user", "existing-user")
        ]
    assert all(response.status_code == 401 for response in responses)
    assert all(response.json()["code"] == "authentication_failed" for response in responses)
    assert responses[0].json()["title"] == responses[1].json()["title"]
    assert all("wrong-password-material" not in response.text for response in responses)


def test_auth_rate_limit_and_redis_failure_are_stable_and_redacted() -> None:
    services, _ = _services()

    @asynccontextmanager
    async def service_factory(settings: Settings):
        del settings
        yield services

    client = TestClient(
        create_app(
            Settings(environment="test", secret_key="x" * 32),
            service_factory=service_factory,
        )
    )
    with client:
        services.rate_limiter.deny = True
        limited = client.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"kind": "username", "identifier": "user", "password": "wrong"},
        )
        services.rate_limiter.deny = False
        services.rate_limiter.fail = True
        unavailable = client.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"kind": "username", "identifier": "user", "password": "wrong"},
        )
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "7"
    assert limited.headers["Retry-After"].isdigit()
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "security_dependency_unavailable"
    assert "redis" not in unavailable.text.lower()


def test_cors_preflight_only_allows_configured_origin_and_headers() -> None:
    client, _ = _client()
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,x-csrf-token",
    }
    with client:
        allowed = client.options(
            "/api/v1/auth/refresh", headers={**headers, "Origin": ORIGIN}
        )
        denied = client.options(
            "/api/v1/auth/refresh",
            headers={**headers, "Origin": "https://evil.example"},
        )
    assert allowed.status_code == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


def test_request_validation_and_internal_errors_are_redacted() -> None:
    client, _ = _client()
    with client:
        invalid = client.post(
            "/api/v1/auth/register",
            headers={"Origin": ORIGIN},
            json={"username": "x", "password": "secret-value", "unexpected": "forbidden"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["code"] == "request_validation_failed"
        assert "secret-value" not in invalid.text

    services, _ = _services()
    services.identity.internal_error = True

    @asynccontextmanager
    async def failing_factory(settings: Settings):
        del settings
        yield services

    failing = TestClient(
        create_app(
            Settings(environment="test", secret_key="x" * 32),
            service_factory=failing_factory,
        ),
        raise_server_exceptions=False,
    )
    with failing:
        internal = failing.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"kind": "username", "identifier": "user", "password": "secret"},
        )
    assert internal.status_code == 500
    assert internal.json()["code"] == "internal_error"
    assert "select" not in internal.text.lower()
    assert "ciphertext" not in internal.text.lower()


def test_internal_value_errors_are_redacted_as_500_not_client_422() -> None:
    services, _ = _services()
    services.identity.internal_value_error = True

    @asynccontextmanager
    async def failing_factory(settings: Settings):
        del settings
        yield services

    client = TestClient(
        create_app(
            Settings(environment="test", secret_key="x" * 32),
            service_factory=failing_factory,
        ),
        raise_server_exceptions=False,
    )
    with client:
        response = client.post(
            "/api/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"kind": "username", "identifier": "user", "password": "secret"},
        )

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert "invariant" not in response.text
    assert "secret-material" not in response.text


def test_api_responses_and_openapi_do_not_expose_internal_security_fields() -> None:
    client, _ = _client()
    forbidden = {
        "refresh_token",
        "password_hash",
        "subject_ciphertext",
        "blind_index",
        "auth_version",
        "authz_version",
        "key_version",
    }
    with client:
        response = client.post(
            "/api/v1/auth/register", json=REGISTER, headers={"Origin": ORIGIN}
        )
        assert forbidden.isdisjoint(response.json())
        schemas = str(client.get("/openapi.json").json()).lower()
        assert all(name not in schemas for name in forbidden)


def test_problem_details_reuses_safe_request_id_and_generates_one_for_unsafe_input() -> None:
    client, _ = _client()
    with client:
        reused = client.get("/missing", headers={"X-Request-ID": "request-123"})
        assert reused.json()["trace_id"] == "request-123"
        assert reused.headers["X-Request-ID"] == "request-123"

        unsafe = client.get("/missing", headers={"X-Request-ID": "secret\nvalue"})
        assert unsafe.status_code == 404
        assert unsafe.json()["trace_id"] != "secret\nvalue"
        assert len(unsafe.json()["trace_id"]) == 32


def test_complete_identity_authorization_endpoint_inventory() -> None:
    client, _ = _client()
    expected = {
        ("POST", "/api/v1/auth/register"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/refresh"),
        ("POST", "/api/v1/auth/logout"),
        ("POST", "/api/v1/auth/reauth"),
        ("POST", "/api/v1/auth/switch-tenant"),
        ("GET", "/api/v1/me"),
        ("GET", "/api/v1/me/tenants"),
        ("POST", "/api/v1/tenants"),
        ("GET", "/api/v1/tenants/{tenant_id}"),
        ("PATCH", "/api/v1/tenants/{tenant_id}"),
        ("GET", "/api/v1/tenants/{tenant_id}/members"),
        ("POST", "/api/v1/tenants/{tenant_id}/invitations"),
        ("POST", "/api/v1/invitations/accept"),
        ("PATCH", "/api/v1/tenants/{tenant_id}/members/{membership_id}"),
        ("DELETE", "/api/v1/tenants/{tenant_id}/members/{membership_id}"),
        ("GET", "/api/v1/platform/tenant-applications"),
        ("POST", "/api/v1/platform/tenant-applications/{tenant_id}/approve"),
        ("POST", "/api/v1/platform/tenant-applications/{tenant_id}/reject"),
    }
    with client:
        schema = client.app.openapi()
        actual = {
            (method.upper(), path)
            for path, operation in schema["paths"].items()
            for method in operation
            if method != "parameters"
        }
    assert expected <= actual
