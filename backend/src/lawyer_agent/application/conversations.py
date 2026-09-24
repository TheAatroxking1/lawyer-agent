"""Tenant/user-owned chat history and durable streaming orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

import anyio
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lawyer_agent.application.legal_chat import LegalChatError, LegalChatHttpService
from lawyer_agent.application.tenancy import TenantActor
from lawyer_agent.domain.model_gateway import ChatMessage


class ConversationError(Exception):
    def __init__(self, code: str, status: int = 409) -> None:
        self.code, self.status = code, status
        super().__init__(code)


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CreateConversationInput(WireModel):
    title: str = Field(default="新对话", min_length=1, max_length=120)
    review_id: UUID | None = None

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must not be blank")
        return value.strip()


class MessageInput(WireModel):
    content: str = Field(min_length=1, max_length=4000)
    request_id: UUID

    @field_validator("content")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class ConversationSummary(WireModel):
    id: UUID
    title: str
    updated_at: datetime
    review_id: UUID | None = None


class MessageView(WireModel):
    id: UUID
    role: Literal["user", "assistant"]
    content: str
    status: Literal["streaming", "completed", "incomplete"]


class ConversationDetail(ConversationSummary):
    messages: tuple[MessageView, ...]
    has_more: bool = False
    next_message_cursor: str | None = None


class ConversationPage(WireModel):
    items: tuple[ConversationSummary, ...]
    next_cursor: str | None = None


@dataclass(frozen=True)
class TurnLease:
    tenant_id: UUID
    user_id: UUID
    conversation_id: UUID
    request_id: UUID
    token: str = field(repr=False)
    actor: TenantActor | None = field(default=None, repr=False)


@dataclass(frozen=True)
class PreparedTurn:
    lease: TurnLease | None
    messages: tuple[ChatMessage, ...]
    assistant_id: UUID
    content: str
    status: Literal["streaming", "completed", "incomplete"]


@dataclass(frozen=True)
class ChatEvent:
    kind: Literal["started", "delta", "error", "done"]
    text: str = ""
    status: str = ""
    code: str = ""
    message_id: UUID | None = None


class ConversationStore(Protocol):
    async def create(
        self, actor: TenantActor, title: str, review_id: UUID | None = None
    ) -> ConversationSummary: ...
    async def list(
        self, actor: TenantActor, *, limit: int = 30, cursor: str | None = None
    ) -> ConversationPage: ...
    async def get(
        self, actor: TenantActor, conversation_id: UUID, *, message_cursor: str | None = None
    ) -> ConversationDetail: ...
    async def start(
        self, actor: TenantActor, conversation_id: UUID, content: str, request_id: UUID
    ) -> PreparedTurn: ...
    async def append(self, lease: TurnLease, text: str) -> None: ...
    async def finish(
        self, lease: TurnLease, status: Literal["completed", "incomplete"]
    ) -> None: ...


class ConversationService:
    def __init__(self, repository: ConversationStore, provider: LegalChatHttpService) -> None:
        self.repository, self.provider = repository, provider

    async def prepare(
        self, actor: TenantActor, conversation_id: UUID, body: MessageInput
    ) -> PreparedTurn:
        # Check provider configuration before accepting a new durable user turn.
        if not self.provider.gateway_available:
            raise ConversationError("model_provider_unavailable", 503)
        return await self.repository.start(actor, conversation_id, body.content, body.request_id)

    async def stream(self, turn: PreparedTurn) -> AsyncGenerator[ChatEvent, None]:
        if turn.lease is None:
            yield ChatEvent("started", message_id=turn.assistant_id)
            if turn.content:
                yield ChatEvent("delta", text=turn.content)
            yield ChatEvent("done", status=turn.status)
            return
        finalized = False
        status = "incomplete"
        length = 0
        try:
            yield ChatEvent("started", message_id=turn.assistant_id)
            # Database lease outlives this bounded model wait; crashed requests are recovered later.
            async with asyncio.timeout(180):
                async for text in self.provider.chat_stream(
                    turn.messages, server_owned_history=True
                ):
                    if not isinstance(text, str) or length + len(text) > 65536:
                        raise ConversationError("conversation_answer_too_large", 502)
                    if not text:
                        continue
                    await self.repository.append(turn.lease, text)
                    length += len(text)
                    yield ChatEvent("delta", text=text)
            if length == 0:
                raise ConversationError("model_provider_failure", 502)
            await self.repository.finish(turn.lease, "completed")
            finalized, status = True, "completed"
        except (LegalChatError, ConversationError) as exc:
            yield ChatEvent("error", code=exc.code)
        except TimeoutError:
            yield ChatEvent("error", code="model_provider_timeout")
        except Exception:  # noqa: BLE001 -- provider/storage details never cross the wire
            yield ChatEvent("error", code="conversation_stream_failed")
        finally:
            if not finalized:
                # Starlette cancels the enclosing AnyIO scope on disconnect.
                with anyio.CancelScope(shield=True):
                    with anyio.move_on_after(6):
                        await self.repository.finish(turn.lease, "incomplete")
        yield ChatEvent("done", status=status)
