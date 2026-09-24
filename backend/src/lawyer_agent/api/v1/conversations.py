"""Authenticated, creator-private conversation resources."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from lawyer_agent.api.dependencies import Services, TenantActorDependency
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.conversations import (
    ConversationDetail,
    ConversationError,
    ConversationPage,
    ConversationService,
    ConversationSummary,
    CreateConversationInput,
    MessageInput,
    PreparedTurn,
)
from lawyer_agent.application.tenancy import TenantActor

router = APIRouter(prefix="/tenants/{tenant_id}/conversations", tags=["conversations"])


def _service(tenant_id: UUID, actor: TenantActor, services: Any) -> ConversationService:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "resource_unavailable", "Resource is unavailable")
    service = getattr(services, "conversation_http", None)
    if service is None:
        raise ApiProblem(503, "conversation_unavailable", "会话服务尚未配置")
    return cast(ConversationService, service)


def _problem(exc: ConversationError) -> ApiProblem:
    return ApiProblem(
        exc.status,
        exc.code,
        "Resource is unavailable" if exc.status == 404 else "会话请求未完成，请根据错误码重试",
    )


@router.post("", response_model=ConversationSummary, status_code=201)
async def create_conversation(
    tenant_id: UUID, body: CreateConversationInput, actor: TenantActorDependency, services: Services
) -> ConversationSummary:
    service = _service(tenant_id, actor, services)
    try:
        return await service.repository.create(actor, body.title, body.review_id)
    except ConversationError as exc:
        raise _problem(exc) from None


@router.get("", response_model=ConversationPage)
async def list_conversations(
    tenant_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> ConversationPage:
    service = _service(tenant_id, actor, services)
    try:
        return await service.repository.list(actor, limit=limit, cursor=cursor)
    except ConversationError as exc:
        raise _problem(exc) from None


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    tenant_id: UUID,
    conversation_id: UUID,
    actor: TenantActorDependency,
    services: Services,
    message_cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> ConversationDetail:
    service = _service(tenant_id, actor, services)
    try:
        return await service.repository.get(actor, conversation_id, message_cursor=message_cursor)
    except ConversationError as exc:
        raise _problem(exc) from None


async def _stream(service: ConversationService, turn: PreparedTurn) -> AsyncIterator[str]:
    stream = service.stream(turn)
    try:
        async for event in stream:
            payload: dict[str, Any] = {}
            if event.kind == "delta":
                payload = {"text": event.text}
            elif event.kind == "done":
                payload = {"status": event.status}
            elif event.kind == "error":
                payload = {"code": event.code, "title": "回答未完成，已保留生成内容"}
            elif event.message_id:
                payload = {"message_id": str(event.message_id)}
            yield f"event: {event.kind}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    finally:
        await stream.aclose()


@router.post("/{conversation_id}/messages/stream")
async def send_message(
    tenant_id: UUID,
    conversation_id: UUID,
    body: MessageInput,
    actor: TenantActorDependency,
    services: Services,
) -> StreamingResponse:
    service = _service(tenant_id, actor, services)
    try:
        turn = await service.prepare(actor, conversation_id, body)
    except ConversationError as exc:
        raise _problem(exc) from None
    return StreamingResponse(
        _stream(service, turn),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
