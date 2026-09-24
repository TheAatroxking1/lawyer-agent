import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import account_session, services
from lawyer_agent.api.errors import ApiProblem, api_problem_handler
from lawyer_agent.api.v1.legal_retrieval_qa import router
from lawyer_agent.application.model_gateway import ModelProviderBusy, ModelProviderTimeout


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("error,status,code", [
    (ModelProviderBusy("private payload"), 503, "model_provider_busy"),
    (ModelProviderTimeout("private payload"), 504, "model_provider_timeout"),
])
def test_http_and_sse_busy_timeout_are_stable_without_answer(stream, error, status, code):
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.add_exception_handler(ApiProblem, api_problem_handler)
    app.dependency_overrides[account_session] = lambda: object()
    app.dependency_overrides[services] = lambda: SimpleNamespace(
        legal_retrieval_qa_http=SimpleNamespace(answer=AsyncMock(side_effect=error)),
    )
    with TestClient(app) as client:
        response = client.post("/api/v1/legal/questions" + ("/stream" if stream else ""),
                               json={"question": "synthetic question"})
    assert "private payload" not in response.text
    if stream:
        assert response.status_code == 200
        assert "event: answer" not in response.text
        events = response.text.strip().split("\n\n")
        assert [event.splitlines()[0] for event in events] == [
            "event: started", "event: error", "event: done",
        ]
        payload = json.loads(events[1].split("data: ")[1])
        assert payload["code"] == code and payload["status"] == status
    else:
        assert response.status_code == status
        assert response.json()["code"] == code
