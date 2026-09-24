from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lawyer_agent.api.dependencies import services, tenant_actor
from lawyer_agent.api.errors import ApiProblem, api_problem_handler
from lawyer_agent.api.v1.conversations import router
from lawyer_agent.application.conversations import (
    ConversationError,
    ConversationPage,
    ConversationService,
    ConversationSummary,
    PreparedTurn,
)
from lawyer_agent.application.legal_chat import LegalChatHttpService
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.conversations import ConversationRepository
from tests.unit.test_history_cursor import cursor


def client_for(repo):
    tenant = uuid4()
    app = FastAPI()
    app.add_exception_handler(ApiProblem, api_problem_handler)
    app.include_router(router, prefix="/api/v1")
    provider = LegalChatHttpService(SimpleNamespace())
    app.dependency_overrides[services] = lambda: SimpleNamespace(
        conversation_http=ConversationService(repo, provider)
    )
    app.dependency_overrides[tenant_actor] = lambda: SimpleNamespace(
        context=SimpleNamespace(tenant_id=tenant)
    )
    return TestClient(app), tenant


def test_scope_and_body_fail_before_repository():
    repo = SimpleNamespace(start=AsyncMock(), list=AsyncMock())
    client, tenant = client_for(repo)
    assert client.get(f"/api/v1/tenants/{uuid4()}/conversations").status_code == 404
    base = f"/api/v1/tenants/{tenant}/conversations/{uuid4()}/messages/stream"
    response = client.post(
        base, json={"content": "问题", "request_id": str(uuid4()), "messages": []}
    )
    assert response.status_code == 422
    repo.start.assert_not_called()
    repo.list.assert_not_called()


def test_concurrent_request_is_json_conflict_before_sse():
    repo = SimpleNamespace(start=AsyncMock(side_effect=ConversationError("conversation_busy")))
    client, tenant = client_for(repo)
    response = client.post(
        f"/api/v1/tenants/{tenant}/conversations/{uuid4()}/messages/stream",
        json={"content": "问题", "request_id": str(uuid4())},
    )
    assert response.status_code == 409 and response.json()["code"] == "conversation_busy"


def test_replay_emits_explicit_terminal_status():
    repo = SimpleNamespace(
        start=AsyncMock(return_value=PreparedTurn(None, (), uuid4(), "中断内容", "incomplete"))
    )
    client, tenant = client_for(repo)
    response = client.post(
        f"/api/v1/tenants/{tenant}/conversations/{uuid4()}/messages/stream",
        json={"content": "问题", "request_id": str(uuid4())},
    )
    assert response.status_code == 200
    assert "event: delta" in response.text and '"status": "incomplete"' in response.text
    assert response.headers["cache-control"] == "no-store"


def test_list_forwards_validated_pagination():
    repo = SimpleNamespace(list=AsyncMock(return_value=ConversationPage(items=())))
    client, tenant = client_for(repo)
    response = client.get(f"/api/v1/tenants/{tenant}/conversations?limit=2&cursor=opaque")
    assert response.json() == {"items": [], "next_cursor": None}
    assert repo.list.await_args.kwargs == {"limit": 2, "cursor": "opaque"}
    assert client.get(f"/api/v1/tenants/{tenant}/conversations?limit=101").status_code == 422


def test_create_can_bind_an_owned_contract_review():
    review_id = new_uuid7()
    summary = ConversationSummary(
        id=new_uuid7(),
        title="合同追问",
        updated_at="2026-09-21T00:00:00Z",
        review_id=review_id,
    )
    repo = SimpleNamespace(create=AsyncMock(return_value=summary))
    client, tenant = client_for(repo)

    response = client.post(
        f"/api/v1/tenants/{tenant}/conversations",
        json={"title": "合同追问", "review_id": str(review_id)},
    )

    assert response.status_code == 201
    assert response.json()["review_id"] == str(review_id)
    repo.create.assert_awaited_once()
    assert repo.create.await_args.args[1:] == ("合同追问", review_id)


@pytest.mark.parametrize(
    "at,ident",
    [
        ("2026-09-21T00:00:00", 123),
        ("0001-01-01T00:00:00+01:00", "valid_uuid"),
    ],
)
@pytest.mark.parametrize("detail", [False, True])
def test_malformed_cursor_is_422_at_real_repository_boundary(at, ident, detail):
    @asynccontextmanager
    async def transaction():
        yield None

    @asynccontextmanager
    async def sessions():
        yield SimpleNamespace(begin=transaction)

    repo = ConversationRepository(sessions)
    client, tenant = client_for(repo)
    user, conversation_id = new_uuid7(), new_uuid7()
    repo._authorize = AsyncMock(return_value=(tenant, user, new_uuid7()))
    repo._owned = AsyncMock(
        return_value=SimpleNamespace(tenant_id=tenant, creator_user_id=user, id=conversation_id)
    )
    repo._expire = AsyncMock()
    bad_cursor = cursor(tenant, user, at, str(new_uuid7()) if ident == "valid_uuid" else ident)
    path = f"/api/v1/tenants/{tenant}/conversations"
    if detail:
        path += f"/{conversation_id}"
    response = client.get(path, params={"message_cursor" if detail else "cursor": bad_cursor})
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_history_cursor"
