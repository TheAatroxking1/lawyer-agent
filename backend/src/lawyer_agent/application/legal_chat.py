"""Public legal chat HTTP orchestration (non-model glue, configured model gateway).

A logged-in account can ask a legal question through the project-owned Model
Gateway. The gateway/provider are composed by the container from Settings;
when no model provider is configured the service is created with no gateway
and every call fails with a stable 503 ``model_provider_unavailable`` -- the
API never fakes an answer or falls back silently (spec 7.2).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from lawyer_agent.domain.model_gateway import ChatMessage, TokenUsage

_MAX_MESSAGES = 32
_MAX_CONTENT_CHARS = 4000
_ALLOWED_ROLES = {"system", "user", "assistant"}


class LegalChatError(Exception):
    status: int = 500
    code: str = "legal_chat_error"
    title: str = "Legal chat failed"


class LegalChatInvalidRequest(LegalChatError):
    status = 422
    code = "legal_chat_invalid_request"
    title = "Legal chat request is invalid"


class LegalChatUnavailable(LegalChatError):
    status = 503
    code = "model_provider_unavailable"
    title = "Model provider is unavailable"


class LegalChatProviderFailure(LegalChatError):
    status = 502
    code = "model_provider_failure"
    title = "Model provider failed"


class LegalChatTimeout(LegalChatError):
    status = 504
    code = "model_provider_timeout"
    title = "Model provider timed out"


class _GatewayPort(Protocol):
    async def chat(
        self,
        *,
        model_ref: str,
        messages: Sequence[ChatMessage],
    ) -> tuple[str, TokenUsage]: ...


class LegalChatHttpService:
    """Composition facade used by the legal chat endpoint."""

    def __init__(self, gateway: _GatewayPort | None, model_ref: str = "deepseek-chat") -> None:
        self._gateway = gateway
        self._model_ref = model_ref

    @property
    def gateway_available(self) -> bool:
        return self._gateway is not None

    async def chat(self, messages: Sequence[ChatMessage]) -> tuple[str, TokenUsage]:
        _validate_messages(messages)
        if self._gateway is None:
            raise LegalChatUnavailable("chat provider is not configured")
        from lawyer_agent.application.model_gateway import (
            ModelProviderInvalidResponse,
            ModelProviderTimeout,
            ModelProviderUnavailable,
        )

        try:
            return await self._gateway.chat(
                model_ref=self._model_ref,
                messages=tuple(messages),
            )
        except ModelProviderTimeout as exc:
            raise LegalChatTimeout(str(exc)) from exc
        except (ModelProviderUnavailable, ModelProviderInvalidResponse) as exc:
            raise LegalChatProviderFailure(str(exc)) from exc

    async def chat_stream(
        self, messages: Sequence[ChatMessage], *, server_owned_history: bool = False
    ) -> AsyncIterator[str]:
        """Streams answer text deltas when the gateway supports ``chat_stream``.

        Validation and capability checks happen before any delta; mid-stream
        provider failures are mapped to the same stable codes as :meth:`chat`
        so the SSE layer can announce an incomplete answer via an ``error`` event.
        """
        _validate_messages(messages, server_owned_history=server_owned_history)
        if self._gateway is None:
            raise LegalChatUnavailable("chat provider is not configured")
        stream_capable = getattr(self._gateway, "chat_stream", None)
        if not callable(stream_capable):
            raise LegalChatProviderFailure("chat streaming is not supported by the model gateway")
        from lawyer_agent.application.model_gateway import (
            ModelProviderInvalidResponse,
            ModelProviderTimeout,
            ModelProviderUnavailable,
        )

        try:
            async for text in stream_capable(
                model_ref=self._model_ref,
                messages=tuple(messages),
            ):
                yield text
        except ModelProviderTimeout as exc:
            raise LegalChatTimeout(str(exc)) from exc
        except (ModelProviderUnavailable, ModelProviderInvalidResponse) as exc:
            raise LegalChatProviderFailure(str(exc)) from exc


def _validate_messages(
    messages: Sequence[ChatMessage], *, server_owned_history: bool = False
) -> None:
    if not isinstance(messages, Sequence) or not messages:
        raise LegalChatInvalidRequest("messages must be a non-empty sequence")
    if len(messages) > _MAX_MESSAGES:
        raise LegalChatInvalidRequest(f"messages must contain at most {_MAX_MESSAGES} entries")
    for message in messages:
        if not isinstance(message, ChatMessage):
            raise LegalChatInvalidRequest("messages must be strongly typed")
        if message.role not in _ALLOWED_ROLES:
            raise LegalChatInvalidRequest("chat role must be system, user or assistant")
        if not isinstance(message.content, str) or not message.content.strip():
            raise LegalChatInvalidRequest("chat content must be non-empty text")
        content_limit = (
            65536 if server_owned_history and message.role == "assistant" else _MAX_CONTENT_CHARS
        )
        if len(message.content) > content_limit:
            raise LegalChatInvalidRequest(
                f"chat content must be at most {content_limit} characters"
            )
