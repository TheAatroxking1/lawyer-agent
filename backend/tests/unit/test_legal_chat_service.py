from __future__ import annotations

from collections.abc import Sequence

import pytest

from lawyer_agent.application.legal_chat import (
    LegalChatHttpService,
    LegalChatInvalidRequest,
    LegalChatProviderFailure,
    LegalChatTimeout,
    LegalChatUnavailable,
)
from lawyer_agent.domain.model_gateway import ChatMessage, TokenUsage

_TEXT = "违约金一般以实际损失为基础，约定过高可请求酌减。"


class _FakeGateway:
    def __init__(
        self,
        *,
        fail: Exception | None = None,
        empty: bool = False,
        usage: TokenUsage | None = None,
    ) -> None:
        self.fail = fail
        self.empty = empty
        self.usage = usage if usage is not None else TokenUsage(total_tokens=3)
        self.calls: list[tuple[str, tuple[ChatMessage, ...]]] = []

    async def chat(
        self,
        *,
        model_ref: str,
        messages: Sequence[ChatMessage],
    ) -> tuple[str, TokenUsage]:
        self.calls.append((model_ref, tuple(messages)))
        if self.fail is not None:
            raise self.fail
        return ("" if self.empty else _TEXT), self.usage


def _service(gateway: object | None = None) -> LegalChatHttpService:
    return LegalChatHttpService(gateway=gateway)


def _messages() -> tuple[ChatMessage, ...]:
    return (
        ChatMessage(role="system", content="你是法律助手。"),
        ChatMessage(role="user", content="违约金怎么算？"),
    )


async def test_chat_requires_configured_gateway() -> None:
    service = _service(gateway=None)
    with pytest.raises(LegalChatUnavailable):
        await service.chat(_messages())


async def test_chat_delegates_to_gateway_and_returns_text() -> None:
    gateway = _FakeGateway()
    text, usage = await _service(gateway).chat(_messages())
    assert text == _TEXT
    assert usage.total_tokens == 3
    assert len(gateway.calls) == 1
    model_ref, forwarded = gateway.calls[0]
    assert model_ref == "deepseek-chat"
    assert forwarded == _messages()


async def test_chat_validates_messages() -> None:
    service = _service(gateway=_FakeGateway())
    with pytest.raises(LegalChatInvalidRequest, match="non-empty"):
        await service.chat(())
    with pytest.raises(LegalChatInvalidRequest, match="strongly typed"):
        await service.chat(("raw",))  # type: ignore[arg-type]
    with pytest.raises(LegalChatInvalidRequest, match="non-empty"):
        await service.chat((ChatMessage(role="user", content="   "),))
    with pytest.raises(LegalChatInvalidRequest, match="4000"):
        await service.chat((ChatMessage(role="user", content="x" * 4001),))
    many = tuple(
        ChatMessage(role="user", content=f"m{i}") for i in range(33)
    )
    with pytest.raises(LegalChatInvalidRequest, match="32"):
        await service.chat(many)
    # Domain rejects roles outside the whitelist before the service sees them.
    with pytest.raises(ValueError, match="role"):
        ChatMessage(role="admin", content="x")


async def test_chat_maps_gateway_errors_to_stable_codes() -> None:
    from lawyer_agent.application.model_gateway import (
        ModelProviderInvalidResponse,
        ModelProviderTimeout,
        ModelProviderUnavailable,
    )

    timeout_service = _service(gateway=_FakeGateway(fail=ModelProviderTimeout("t")))
    with pytest.raises(LegalChatTimeout):
        await timeout_service.chat(_messages())

    for error, expected in (
        (ModelProviderUnavailable("u"), LegalChatProviderFailure),
        (ModelProviderInvalidResponse("i"), LegalChatProviderFailure),
    ):
        service = _service(gateway=_FakeGateway(fail=error))
        with pytest.raises(expected):
            await service.chat(_messages())
