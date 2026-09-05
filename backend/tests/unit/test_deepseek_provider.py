from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from lawyer_agent.application.model_gateway import (
    ModelGateway,
    ModelInputInvalid,
    ModelProviderInvalidResponse,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from lawyer_agent.domain.model_gateway import (
    CallLimits,
    ChatMessage,
    ModelCallRecord,
    ModelOperation,
)
from lawyer_agent.infrastructure.providers.deepseek import (
    DeepSeekChatProvider,
)

_API_KEY = "sk-deepseek-test-key-not-a-real-key-000"
_MODEL = "deepseek-chat"
_BASE = "https://api.deepseek.com"


class _MemoryRecorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


def _provider(
    handler: object,
    *,
    api_key: str = _API_KEY,
) -> DeepSeekChatProvider:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return DeepSeekChatProvider(
        api_key=api_key,
        client=httpx.AsyncClient(transport=transport),
    )


def _ok_json(text: str = "你好，我是 DeepSeek。") -> dict[str, object]:
    return {
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 7,
            "total_tokens": 19,
        },
    }


def _messages() -> tuple[ChatMessage, ...]:
    return (
        ChatMessage(role="system", content="你是法律助手。"),
        ChatMessage(role="user", content="违约金怎么计算？"),
    )


def test_chat_posts_openai_compatible_request_and_parses_reply() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_ok_json())

    provider = _provider(handler)
    text, usage = asyncio.run(
        provider.chat(messages=_messages(), timeout_seconds=9.5)
    )
    assert text == "你好，我是 DeepSeek。"
    assert usage.prompt_tokens == 12
    assert usage.completion_tokens == 7
    assert usage.total_tokens == 19
    assert captured["method"] == "POST"
    assert captured["url"] == f"{_BASE}/chat/completions"
    assert captured["auth"] == f"Bearer {_API_KEY}"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == _MODEL
    assert body["stream"] is False
    assert body["messages"] == [
        {"role": "system", "content": "你是法律助手。"},
        {"role": "user", "content": "违约金怎么计算？"},
    ]


def test_chat_rejects_empty_or_untyped_messages_without_request() -> None:
    sent: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200, json=_ok_json())

    provider = _provider(handler)
    with pytest.raises(ModelInputInvalid, match="non-empty"):
        asyncio.run(provider.chat(messages=(), timeout_seconds=5.0))
    with pytest.raises(ModelInputInvalid, match="strongly typed"):
        asyncio.run(
            provider.chat(messages=("raw",), timeout_seconds=5.0)  # type: ignore[arg-type]
        )
    assert sent == []


def test_chat_maps_http_error_status_to_unavailable_without_leaking_key() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="invalid api key secret")

    provider = _provider(handler)
    with pytest.raises(ModelProviderUnavailable) as raised:
        asyncio.run(
            provider.chat(messages=_messages(), timeout_seconds=5.0)
        )
    assert "401" in str(raised.value)
    assert _API_KEY not in str(raised.value)
    assert "invalid api key" not in str(raised.value)


def test_chat_maps_timeout_to_stable_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    provider = _provider(handler)
    with pytest.raises(ModelProviderTimeout):
        asyncio.run(provider.chat(messages=_messages(), timeout_seconds=1.0))


def test_chat_rejects_malformed_or_empty_responses() -> None:
    cases = (
        "not-json",
        json.dumps({"usage": {}}),
        json.dumps({"choices": []}),
        json.dumps({"choices": [{"message": {}}]}),
        json.dumps({"choices": [{"message": {"content": "   "}}]}),
    )

    for raw in cases:

        async def handler(
            request: httpx.Request, raw: str = raw
        ) -> httpx.Response:
            return httpx.Response(200, text=raw)

        provider = _provider(handler)
        with pytest.raises(ModelProviderInvalidResponse):
            asyncio.run(
                provider.chat(messages=_messages(), timeout_seconds=5.0)
            )


def test_chat_normalizes_missing_usage_fields() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 3},
            },
        )

    provider = _provider(handler)
    text, usage = asyncio.run(
        provider.chat(messages=_messages(), timeout_seconds=5.0)
    )
    assert text == "ok"
    assert usage.prompt_tokens == 3
    assert usage.completion_tokens == 0
    assert usage.total_tokens == 0


def test_embed_and_rerank_are_unavailable() -> None:
    provider = _provider(_ok_json)

    with pytest.raises(ModelProviderUnavailable, match="embed"):
        asyncio.run(
            provider.embed(texts=("a",), dimension=8, timeout_seconds=5.0)
        )
    with pytest.raises(ModelProviderUnavailable, match="rerank"):
        asyncio.run(
            provider.rerank(query="q", documents=("d",), timeout_seconds=5.0)
        )


def test_blank_api_key_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="api key"):
        DeepSeekChatProvider(api_key="   ")
    with pytest.raises(ValueError, match="api key"):
        DeepSeekChatProvider(api_key="")


def test_gateway_chat_success_records_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_json("有据回答。"))

    recorder = _MemoryRecorder()
    gateway = ModelGateway(
        _provider(handler),
        recorder,
        limits=CallLimits(timeout_seconds=9.5),
    )
    text, usage = asyncio.run(
        gateway.chat(model_ref=_MODEL, messages=_messages())
    )
    assert text == "有据回答。"
    assert usage.total_tokens == 19
    assert len(recorder.records) == 1
    assert recorder.records[0].operation is ModelOperation.CHAT
    assert recorder.records[0].status == "success"
    assert recorder.records[0].usage is not None
    assert recorder.records[0].usage.total_tokens == 19


def test_gateway_chat_failure_records_error_without_code_leak() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    recorder = _MemoryRecorder()
    gateway = ModelGateway(_provider(handler), recorder)
    with pytest.raises(ModelProviderUnavailable):
        asyncio.run(gateway.chat(model_ref=_MODEL, messages=_messages()))
    assert len(recorder.records) == 1
    assert recorder.records[0].operation is ModelOperation.CHAT
    assert recorder.records[0].status == "error"

