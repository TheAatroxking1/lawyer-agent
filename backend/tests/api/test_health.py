from fastapi.testclient import TestClient

from lawyer_agent.config import Settings
from lawyer_agent.main import create_app


def build_client() -> TestClient:
    settings = Settings(environment="test", secret_key="x" * 32)
    return TestClient(create_app(settings))


def test_liveness_contract() -> None:
    response = build_client().get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "0.1.0"}


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
