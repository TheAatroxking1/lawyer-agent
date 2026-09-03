from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import ApplicationServices
from lawyer_agent.config import Settings
from lawyer_agent.main import create_app


def build_client() -> TestClient:
    settings = Settings(environment="test", secret_key="x" * 32)
    return TestClient(create_app(settings))


def test_liveness_contract() -> None:
    response = build_client().get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


def test_liveness_does_not_require_application_services() -> None:
    class FailingReadiness:
        async def check(self) -> None:
            raise RuntimeError("mysql redis secret dsn")

    started = False

    @asynccontextmanager
    async def service_factory(_settings: Settings) -> AsyncIterator[ApplicationServices]:
        nonlocal started
        started = True
        placeholder = object()
        yield ApplicationServices(
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
            readiness=FailingReadiness(),
        )

    with TestClient(
        create_app(
            Settings(environment="test", secret_key="x" * 32),
            service_factory=service_factory,
        )
    ) as client:
        ready_response = client.get("/health/ready")
        live_response = client.get("/health/live")

    assert started is True
    assert ready_response.status_code == 503
    assert live_response.status_code == 200


def test_unknown_route_uses_problem_details() -> None:
    response = build_client().get("/missing")
    assert response.status_code == 404
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Not Found"
    assert body["status"] == 404
    assert body["code"] == "route_not_found"
    assert len(body["trace_id"]) == 32


def test_request_id_is_reused_as_trace_id() -> None:
    response = build_client().get("/missing", headers={"x-request-id": "request-123"})
    assert response.json()["trace_id"] == "request-123"
