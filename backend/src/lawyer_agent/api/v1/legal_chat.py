from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lawyer_agent.api.dependencies import AccountSession, Services
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.legal_chat import (
    LegalChatError,
    LegalChatHttpService,
)
from lawyer_agent.domain.model_gateway import ChatMessage

router = APIRouter(prefix="/legal", tags=["legal-chat"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessageBody(StrictModel):
    role: str = Field(min_length=1)
    content: str = Field(min_length=1)

    @field_validator("role")
    @classmethod
    def _role_whitelist(cls, value: str) -> str:
        if value not in {"system", "user", "assistant"}:
            raise ValueError("role must be system, user or assistant")
        return value

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class LegalChatBody(StrictModel):
    messages: list[ChatMessageBody] = Field(min_length=1)


class ChatUsageBody(StrictModel):
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class LegalChatReply(StrictModel):
    text: str
    usage: ChatUsageBody


def _map_error(exc: LegalChatError) -> ApiProblem:
    return ApiProblem(exc.status, exc.code, exc.title)


@router.post("/chat", response_model=LegalChatReply)
async def legal_chat(
    body: Annotated[LegalChatBody, Body()],
    current: AccountSession,
    services: Services,
) -> LegalChatReply:
    del current
    value = getattr(services, "legal_chat_http", None)
    if value is None:
        raise ApiProblem(
            503, "model_provider_unavailable", "Model provider is unavailable"
        )
    service = cast(LegalChatHttpService, value)
    messages = tuple(
        ChatMessage(
            role=_literal_role(item.role),
            content=item.content,
        )
        for item in body.messages
    )
    try:
        text, usage = await service.chat(messages)
    except LegalChatError as exc:
        raise _map_error(exc) from None
    return LegalChatReply(
        text=text,
        usage=ChatUsageBody(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
        ),
    )


def _literal_role(value: str) -> Literal["system", "user", "assistant"]:
    if value not in {"system", "user", "assistant"}:
        raise ApiProblem(422, "legal_chat_invalid_request", "Legal chat request is invalid")
    return cast(Literal["system", "user", "assistant"], value)
