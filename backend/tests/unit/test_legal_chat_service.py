from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

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


class _FakeStreamGateway(_FakeGateway):
    def __init__(
        self,
        *,
        deltas: tuple[str, ...] = ("你", "好"),
        fail_before: Exception | None = None,
        fail_after: Exception | None = None,
    ) -> None:
        super().__init__()
        self.deltas = deltas
        self.fail_before = fail_before
        self.fail_after = fail_after

    async def chat_stream(
        self,
        *,
        model_ref: str,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        self.calls.append((model_ref, tuple(messages)))
        if self.fail_before is not None:
            raise self.fail_before
        for index, delta in enumerate(self.deltas):
            yield delta
            if self.fail_after is not None and index == len(self.deltas) - 1:
                raise self.fail_after


async def _collect_stream(stream: AsyncIterator[str]) -> list[str]:
    return [text async for text in stream]


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


async def test_chat_stream_requires_configured_gateway() -> None:
    service = _service(gateway=None)
    assert service.gateway_available is False
    with pytest.raises(LegalChatUnavailable):
        await _collect_stream(service.chat_stream(_messages()))


async def test_chat_stream_requires_stream_capable_gateway() -> None:
    service = _service(gateway=_FakeGateway())
    assert service.gateway_available is True
    with pytest.raises(LegalChatProviderFailure, match="streaming"):
        await _collect_stream(service.chat_stream(_messages()))


async def test_chat_stream_delegates_and_forwards_deltas() -> None:
    gateway = _FakeStreamGateway(deltas=("你", "好"))
    service = _service(gateway)
    deltas = await _collect_stream(service.chat_stream(_messages()))
    assert deltas == ["你", "好"]
    assert len(gateway.calls) == 1
    model_ref, forwarded = gateway.calls[0]
    assert model_ref == "deepseek-chat"
    assert forwarded == _messages()


async def test_chat_stream_validates_messages() -> None:
    gateway = _FakeStreamGateway()
    service = _service(gateway)
    with pytest.raises(LegalChatInvalidRequest, match="non-empty"):
        await _collect_stream(service.chat_stream(()))
    with pytest.raises(LegalChatInvalidRequest, match="strongly typed"):
        await _collect_stream(service.chat_stream(("raw",)))  # type: ignore[arg-type]
    with pytest.raises(LegalChatInvalidRequest, match="4000"):
        await _collect_stream(
            service.chat_stream((ChatMessage(role="user", content="x" * 4001),))
        )
    assert gateway.calls == []


async def test_chat_stream_maps_gateway_errors_to_stable_codes() -> None:
    from lawyer_agent.application.model_gateway import (
        ModelProviderInvalidResponse,
        ModelProviderTimeout,
        ModelProviderUnavailable,
    )

    timeout_before = _service(
        gateway=_FakeStreamGateway(fail_before=ModelProviderTimeout("t"))
    )
    with pytest.raises(LegalChatTimeout):
        await _collect_stream(timeout_before.chat_stream(_messages()))

    for error, expected in (
        (ModelProviderUnavailable("u"), LegalChatProviderFailure),
        (ModelProviderInvalidResponse("i"), LegalChatProviderFailure),
    ):
        service = _service(
            gateway=_FakeStreamGateway(fail_after=error, deltas=("部分",))
        )
        with pytest.raises(expected):
            await _collect_stream(service.chat_stream(_messages()))
