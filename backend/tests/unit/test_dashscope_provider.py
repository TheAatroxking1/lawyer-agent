from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import httpx
import pytest

from lawyer_agent.application.model_gateway import (
    ModelGatewayError,
    ModelInputInvalid,
    ModelProviderInvalidResponse,
    ModelProviderJsonGenerationError,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from lawyer_agent.domain.model_gateway import ChatImage, ChatMessage
from lawyer_agent.infrastructure.providers.dashscope import (
    DashScopeChatProvider,
    DashScopeConfigurationError,
    DashScopeSettings,
)

_KEY = "sk-test-dashscope-secret-123456789"
_JSON_GENERATION_MESSAGE = (
    "Model output became abnormal while generating a JSON response for response_format"
)


def test_contract_message_accepts_30_pages_but_rejects_more():
    picture = ChatImage(data=b"\x89PNG\r\n\x1a\nsynthetic", media_type="image/png")
    assert len(ChatMessage(role="user", content="全部页面", images=(picture,) * 30).images) == 30
    with pytest.raises(ValueError):
        ChatMessage(role="user", content="超出合同页数", images=(picture,) * 31)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,stream_response", [(400, False), (400, True), (200, True)])
async def test_json_generation_error_is_typed_and_does_not_disclose_payload(
    status: int, stream_response: bool, caplog: pytest.LogCaptureFixture,
) -> None:
    payload = {"error": {
        "code": "invalid_parameter_error",
        "message": f"{_JSON_GENERATION_MESSAGE}: PRIVATE_BODY {_KEY}",
    }}
    wire = json.dumps(payload)
    if status == 200:
        wire = 'data: {"choices":[{"delta":{"content":"PRIVATE_PARTIAL"}}]}\n\n' + (
            f"data: {wire}\n\n"
        )
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text=wire),
    )) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, json_mode=True, stream_response=stream_response,
        )
        with pytest.raises(ModelGatewayError) as caught:
            await provider.chat(
                messages=[ChatMessage(role="user", content="PRIVATE_PROMPT")],
                timeout_seconds=5,
            )
    assert caught.value.code == "model_json_generation_failed"
    assert isinstance(caught.value, ModelProviderJsonGenerationError)
    assert isinstance(caught.value, ModelProviderInvalidResponse)
    assert caught.value.__cause__ is None
    for secret in (_KEY, "PRIVATE_BODY", "PRIVATE_PROMPT", "PRIVATE_PARTIAL"):
        assert secret not in str(caught.value)
        assert secret not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [
    (401, {"error": {"code": "invalid_parameter_error", "message": _JSON_GENERATION_MESSAGE}}),
    (403, {"error": {"code": "invalid_parameter_error", "message": _JSON_GENERATION_MESSAGE}}),
    (400, {"error": {"code": "other_error", "message": _JSON_GENERATION_MESSAGE}}),
    (400, {"error": {"code": "invalid_parameter_error", "message": "invalid JSON format"}}),
    (400, {"error": {"code": "invalid_parameter_error", "message": 42}}),
    (400, {"error": _JSON_GENERATION_MESSAGE}),
    (400, [_JSON_GENERATION_MESSAGE]),
    (400, "PRIVATE_INVALID_JSON"),
])
@pytest.mark.parametrize("stream_response", [False, True])
async def test_other_http_errors_keep_unavailable_classification(
    status: int, body: object, stream_response: bool,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        respond,
    )) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, json_mode=True, stream_response=stream_response,
        )
        with pytest.raises(ModelProviderUnavailable) as caught:
            await provider.chat(
                messages=[ChatMessage(role="user", content="JSON")], timeout_seconds=5,
            )
    assert caught.value.code == "model_provider_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("frame", [
    {"error": {"code": "other_error", "message": _JSON_GENERATION_MESSAGE}},
    {"error": {"code": "invalid_parameter_error", "message": "JSON schema invalid"}},
    {"error": _JSON_GENERATION_MESSAGE},
    [_JSON_GENERATION_MESSAGE],
    None,
])
async def test_other_stream_errors_keep_invalid_response_classification(frame: object) -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=f"data: {json.dumps(frame)}\n\n"),
    )) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, json_mode=True, stream_response=True,
        )
        with pytest.raises(ModelProviderInvalidResponse) as caught:
            await provider.chat(
                messages=[ChatMessage(role="user", content="JSON")], timeout_seconds=5,
            )
    assert caught.value.code == "model_provider_invalid_response"


@pytest.mark.asyncio
@pytest.mark.parametrize("response_limit,body_size", [(1024, 1025), (1024 * 1024, 65537)])
async def test_http_error_body_read_is_bounded_and_closes_stream(
    response_limit: int, body_size: int,
) -> None:
    class OversizedError(httpx.AsyncByteStream):
        chunks_read = 0
        closed = False

        async def __aiter__(self):
            self.chunks_read += 1
            yield b" " * body_size
            self.chunks_read += 1
            yield json.dumps({"error": {
                "code": "invalid_parameter_error", "message": _JSON_GENERATION_MESSAGE,
            }}).encode()

        async def aclose(self):
            self.closed = True

    stream = OversizedError()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(400, stream=stream),
    )) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, json_mode=True, max_response_bytes=response_limit,
        )
        with pytest.raises(ModelProviderUnavailable):
            await provider.chat(
                messages=[ChatMessage(role="user", content="JSON")], timeout_seconds=5,
            )
    assert stream.chunks_read == 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["stop", "length"])
async def test_collected_stream_preserves_json_usage_and_truncation(finish):
    from lawyer_agent.application.model_gateway import ModelProviderOutputTruncated

    class Fragmented(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            frames = [
                {"choices": [{"delta": {"content": '{"意见":"'}, "finish_reason": None}]},
                {"choices": [{"delta": {"content": '明确期限"}'}, "finish_reason": finish}]},
                {"choices": [], "usage": {
                    "prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}},
            ]
            wire = ("".join("data: " + json.dumps(f, ensure_ascii=False) + "\r\n\r\n"
                            for f in frames) + "data: [DONE]\r\n\r\n").encode()
            for start in range(0, len(wire), 7):
                yield wire[start:start+7]

        async def aclose(self):
            self.closed = True

    stream = Fragmented()

    def handler(request):
        body = json.loads(request.content)
        assert body['stream'] is True
        assert body['stream_options'] == {'include_usage': True}
        assert body['response_format'] == {'type': 'json_object'}
        assert 'max_tokens' not in body
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, json_mode=True, max_tokens=None, stream_response=True,
        )
        if finish == 'length':
            with pytest.raises(ModelProviderOutputTruncated):
                await provider.chat(messages=[ChatMessage(role='user', content='返回JSON')],
                                    timeout_seconds=5)
        else:
            result, usage = await provider.chat(
                messages=[ChatMessage(role='user', content='返回JSON')], timeout_seconds=5)
            assert result == '{"意见":"明确期限"}'
            assert usage.total_tokens == 17
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize('wire', [
    'data: [DONE]\n\n',
    'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\n',
    'data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n',
    'data: {"choices":[{"delta":{"tool_calls":[{}]},"finish_reason":null}]}\n\n',
])
async def test_collected_stream_rejects_incomplete_protocol(wire):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text=wire),
    )) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, stream_response=True,
        )
        with pytest.raises(ModelProviderInvalidResponse):
            await provider.chat(messages=[ChatMessage(role='user', content='返回JSON')],
                                timeout_seconds=5)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_collected_stream_closes_on_timeout_or_cancellation(cancel):
    started = asyncio.Event()

    class Hanging(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"{"}}]}\n\n'
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            self.closed = True

    stream = Hanging()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=stream),
    )) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, stream_response=True,
        )
        task = asyncio.create_task(provider.chat(
            messages=[ChatMessage(role='user', content='JSON')],
            timeout_seconds=5 if cancel else 0.05,
        ))
        await started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else ModelProviderTimeout):
            await task
    assert stream.closed


@pytest.mark.asyncio
async def test_multimodal_chat_sends_image_bytes_and_records_usage() -> None:
    picture = ChatImage(data=b"\x89PNG\r\n\x1a\nsynthetic", media_type="image/png")
    assert "synthetic" not in repr(picture)

    async def handler(request: httpx.Request) -> httpx.Response:
        message = json.loads(request.content)["messages"][0]
        assert message["content"][0] == {"type": "text", "text": "读取此页"}
        url = message["content"][1]["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        assert base64.b64decode(url.split(",", 1)[1]) == picture.data
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": "已读取"}}],
            "usage": {"prompt_tokens": 160, "completion_tokens": 7, "total_tokens": 167},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}), client=client,
        )
        text, usage = await provider.chat(
            messages=[ChatMessage(role="user", content="读取此页", images=(picture,))],
            timeout_seconds=5.0,
        )
    assert text == "已读取" and usage.total_tokens == 167


@pytest.mark.parametrize("data,media_type", [(b"", "image/png"), (b"abc", "image/png"),
    (b"x" * (2 * 1024 * 1024 + 1), "image/jpeg"), (b"abc", "image/svg+xml")],
    ids=["empty", "invalid-signature", "oversized", "unsupported-format"])
def test_chat_image_rejects_invalid_or_oversized_bytes(data: bytes, media_type: str) -> None:
    with pytest.raises(ValueError):
        ChatImage(data=data, media_type=media_type)


def _config(tmp_path: Path, payload: object) -> Path:
    path = tmp_path / "dashscope.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_unspecified_output_budget_omits_token_limit_fields(streaming):
    def handler(request):
        payload = json.loads(request.content)
        assert "max_tokens" not in payload
        assert "max_completion_tokens" not in payload
        if streaming:
            frame = {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]}
            return httpx.Response(200, text="data: " + json.dumps(frame) + "\n\ndata: [DONE]\n\n")
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client, max_tokens=None,
        )
        messages = [ChatMessage(role="user", content="返回JSON")]
        if streaming:
            assert [part async for part in provider.chat_stream(
                messages=messages, timeout_seconds=5.0,
            )] == ["{}"]
        else:
            result, _ = await provider.chat(messages=messages, timeout_seconds=5.0)
            assert result == "{}"


@pytest.mark.asyncio
async def test_structured_calls_opt_into_json_object_without_changing_default():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": '{}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for structured in (False, True):
            provider = DashScopeChatProvider(
                settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
                client=client, json_mode=structured,
            )
            await provider.chat(messages=[ChatMessage(role="user", content="返回JSON")],
                                timeout_seconds=5.0)
    assert "response_format" not in seen[0]
    assert "presence_penalty" not in seen[0]
    assert seen[1]["response_format"] == {"type": "json_object"}
    assert seen[1]["presence_penalty"] == 0.0


def test_settings_load_key_from_explicit_file_without_disclosing_it(tmp_path: Path) -> None:
    path = _config(tmp_path, {"DASHSCOPE_API_KEY": _KEY})

    settings = DashScopeSettings.load(path=path, environ={})

    assert settings.api_key.get_secret_value() == _KEY
    assert _KEY not in repr(settings)


def test_settings_load_selected_non_secret_defaults_from_file(tmp_path: Path) -> None:
    path = _config(
        tmp_path,
        {
            "DASHSCOPE_API_KEY": _KEY,
            "LAWYER_DASHSCOPE_BASE_URL": (
                "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            ),
            "LAWYER_DASHSCOPE_MODEL": "qwen3.6-flash",
        },
    )
    settings = DashScopeSettings.load(path=path, environ={})
    assert settings.model_name == "qwen3.6-flash"


def test_settings_load_key_from_environment() -> None:
    settings = DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY})
    assert settings.api_key.get_secret_value() == _KEY
    assert settings.base_url == (
        "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    )
    assert settings.model_name == "qwen3.6-flash"


def test_settings_accept_selected_official_maas_endpoint() -> None:
    settings = DashScopeSettings.load(
        environ={
            "DASHSCOPE_API_KEY": _KEY,
            "LAWYER_DASHSCOPE_BASE_URL": (
                "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            ),
            "LAWYER_DASHSCOPE_MODEL": "qwen3.6-flash",
        }
    )
    assert settings.model_name == "qwen3.6-flash"


def test_settings_reject_two_key_sources(tmp_path: Path) -> None:
    path = _config(tmp_path, {"DASHSCOPE_API_KEY": _KEY})
    with pytest.raises(DashScopeConfigurationError) as caught:
        DashScopeSettings.load(path=path, environ={"DASHSCOPE_API_KEY": _KEY})
    assert caught.value.code == "dashscope_key_sources_conflict"
    assert _KEY not in str(caught.value)


def test_settings_missing_key_has_stable_code() -> None:
    with pytest.raises(DashScopeConfigurationError) as caught:
        DashScopeSettings.load(environ={})
    assert caught.value.code == "dashscope_api_key_missing"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"DASHSCOPE_API_KEY": ""},
        {"DASHSCOPE_API_KEY": 7},
        {"DASHSCOPE_API_KEY": _KEY, "extra": "no"},
    ],
)
def test_settings_reject_invalid_file_without_disclosing_secret(
    tmp_path: Path, payload: object
) -> None:
    path = _config(tmp_path, payload)
    with pytest.raises(DashScopeConfigurationError) as caught:
        DashScopeSettings.load(path=path, environ={})
    assert caught.value.code == "dashscope_config_invalid"
    assert _KEY not in str(caught.value)


def test_settings_require_absolute_regular_utf8_json_file(tmp_path: Path) -> None:
    for path in (Path("relative.json"), tmp_path):
        with pytest.raises(DashScopeConfigurationError) as caught:
            DashScopeSettings.load(path=path, environ={})
        assert caught.value.code == "dashscope_config_invalid"

    invalid_utf8 = tmp_path / "invalid.json"
    invalid_utf8.write_bytes(b"\xff")
    with pytest.raises(DashScopeConfigurationError):
        DashScopeSettings.load(path=invalid_utf8, environ={})


def test_settings_reject_untrusted_or_credential_bearing_base_url() -> None:
    for base_url in (
        "http://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://evil.example/v1",
        "https://dashscopeevil.aliyuncs.com/v1",
        "https://maas.aliyuncs.com.evil.example/v1",
        "https://dashscope.aliyuncs.com/v1?api_key=secret",
        "https://user:pass@dashscope.aliyuncs.com/v1",
    ):
        with pytest.raises(DashScopeConfigurationError) as caught:
            DashScopeSettings.load(
                environ={"DASHSCOPE_API_KEY": _KEY, "LAWYER_DASHSCOPE_BASE_URL": base_url}
            )
        assert caught.value.code == "dashscope_config_invalid"


def test_settings_direct_construction_rejects_untrusted_url() -> None:
    with pytest.raises(ValueError):
        DashScopeSettings(api_key=_KEY, base_url="https://evil.example/v1")


@pytest.mark.asyncio
async def test_chat_uses_non_thinking_non_streaming_payload_and_maps_usage() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {_KEY}"
        payload = json.loads(request.content)
        assert payload == {
            "model": "qwen3.6-flash",
            "messages": [{"role": "user", "content": "请审查"}],
            "stream": False,
            "enable_thinking": False,
            "max_tokens": 4096,
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "审查结果",
                            "reasoning_content": "discard",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        text, usage = await provider.chat(
            messages=[ChatMessage(role="user", content="请审查")], timeout_seconds=5.0
        )

    assert text == "审查结果"
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (2, 3, 5)
    assert "discard" not in text


@pytest.mark.asyncio
async def test_chat_logs_only_safe_http_status_on_provider_failure(caplog) -> None:
    from lawyer_agent.application.model_gateway import ModelProviderUnavailable

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(429, text="PRIVATE_PROVIDER_BODY"),
    )) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        with pytest.raises(ModelProviderUnavailable):
            await provider.chat(messages=[ChatMessage(role="user", content="PRIVATE_PROMPT")],
                                timeout_seconds=5.0)
    assert "http_status=429" in caplog.text
    assert all(value not in caplog.text for value in (
        _KEY, "PRIVATE_PROVIDER_BODY", "PRIVATE_PROMPT",
    ))


@pytest.mark.asyncio
async def test_chat_rejects_bad_input_before_http() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        with pytest.raises(ModelInputInvalid):
            await provider.chat(messages=[], timeout_seconds=5.0)


@pytest.mark.asyncio
async def test_chat_rejects_oversized_or_malformed_provider_response() -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"x" * 1025),
            httpx.Response(200, json={"choices": [], "usage": {}}),
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return next(responses)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
            max_response_bytes=1024,
        )
        for _ in range(2):
            with pytest.raises(ModelProviderInvalidResponse):
                await provider.chat(
                    messages=[ChatMessage(role="user", content="x")], timeout_seconds=5.0
                )


@pytest.mark.asyncio
async def test_chat_rejects_truncated_length_completion() -> None:
    response = {
        "choices": [
            {"finish_reason": "length", "message": {"content": '{"partial": true'}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        from lawyer_agent.application.model_gateway import ModelProviderOutputTruncated

        with pytest.raises(ModelProviderOutputTruncated, match="truncated"):
            await provider.chat(
                messages=[ChatMessage(role="user", content="x")], timeout_seconds=5.0
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "finish_reason", ["content_filter", "tool_calls", "unknown", None, "stop"]
)
async def test_chat_accepts_only_complete_stop_finish_reason(
    finish_reason: str | None,
) -> None:
    response = {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {"content": "partial", "tool_calls": [{"id": "call-1"}]},
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response))
    ) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        with pytest.raises(ModelProviderInvalidResponse, match="incomplete"):
            await provider.chat(
                messages=[ChatMessage(role="user", content="x")], timeout_seconds=5.0
            )


def test_direct_settings_validation_error_does_not_disclose_key() -> None:
    with pytest.raises(ValueError) as caught:
        DashScopeSettings(api_key=_KEY, base_url="https://evil.example/v1")
    assert _KEY not in str(caught.value)


@pytest.mark.asyncio
async def test_chat_enforces_overall_deadline() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        with pytest.raises(ModelProviderTimeout):
            await provider.chat(
                messages=[ChatMessage(role="user", content="x")], timeout_seconds=0.01
            )


@pytest.mark.asyncio
async def test_chat_http_error_does_not_disclose_key_or_prompt() -> None:
    prompt = "private-client-text"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(401, text=_KEY))
    ) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        with pytest.raises(ModelProviderUnavailable) as caught:
            await provider.chat(
                messages=[ChatMessage(role="user", content=prompt)], timeout_seconds=5.0
            )
    assert _KEY not in str(caught.value)
    assert prompt not in str(caught.value)


@pytest.mark.asyncio
async def test_embed_and_rerank_are_explicitly_unsupported() -> None:
    async with httpx.AsyncClient() as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY}),
            client=client,
        )
        with pytest.raises(ModelProviderUnavailable, match="not supported"):
            await provider.embed(texts=["x"], dimension=2, timeout_seconds=1.0)
        with pytest.raises(ModelProviderUnavailable, match="not supported"):
            await provider.rerank(query="x", documents=["y"], timeout_seconds=1.0)


def test_provider_rejects_redirecting_client_and_excessive_max_tokens() -> None:
    settings = DashScopeSettings.load(environ={"DASHSCOPE_API_KEY": _KEY})
    with httpx.Client() as sync_client:
        assert sync_client is not None
    client = httpx.AsyncClient(follow_redirects=True)
    try:
        with pytest.raises(ValueError, match="redirect"):
            DashScopeChatProvider(settings=settings, client=client)
    finally:
        import asyncio

        asyncio.run(client.aclose())

    client = httpx.AsyncClient()
    try:
        with pytest.raises(ValueError, match="max_tokens"):
            DashScopeChatProvider(settings=settings, client=client, max_tokens=65537)
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_dashscope_config_template_contains_selected_defaults_and_blank_key() -> None:
    template = Path(__file__).resolve().parents[3] / "deploy/dashscope.config.example.json"
    payload = json.loads(template.read_text(encoding="utf-8"))
    assert payload == {
        "DASHSCOPE_API_KEY": "",
        "LAWYER_DASHSCOPE_BASE_URL": (
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        ),
        "LAWYER_DASHSCOPE_MODEL": "qwen3.6-flash",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize('tail', ['stop', 'length', 'missing'])
async def test_stream_requires_complete_answer_and_never_emits_reasoning(tail: str) -> None:
    frames = [
        {'choices': [{
            'delta': {'reasoning_content': 'hidden', 'content': '你好'}, 'finish_reason': None,
        }]},
    ]
    if tail != 'missing':
        frames.append({'choices': [{'delta': {}, 'finish_reason': tail}]})
    raw = ''.join('data: ' + json.dumps(frame) + '\n\n' for frame in frames)
    if tail != 'missing':
        raw += 'data: [DONE]\n\n'

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload['stream'] is True
        assert payload['model'] == 'qwen3.6-flash'
        assert payload['enable_thinking'] is False
        return httpx.Response(200, text=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        provider = DashScopeChatProvider(
            settings=DashScopeSettings.load(environ={'DASHSCOPE_API_KEY': _KEY}), client=client,
        )
        parts = []
        if tail == 'stop':
            parts = [part async for part in provider.chat_stream(
                messages=[ChatMessage(role='user', content='你好')], timeout_seconds=2,
            )]
            assert parts == ['你好']
        else:
            with pytest.raises(ModelProviderInvalidResponse):
                async for part in provider.chat_stream(
                    messages=[ChatMessage(role='user', content='你好')], timeout_seconds=2,
                ):
                    parts.append(part)
        assert 'hidden' not in ''.join(parts)


@pytest.mark.asyncio
async def test_chat_composition_uses_configured_contract_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from lawyer_agent.api.dependencies import _build_legal_chat_http_service

    monkeypatch.delenv('DASHSCOPE_API_KEY', raising=False)
    key_path = _config(tmp_path, {'DASHSCOPE_API_KEY': _KEY})
    config_path = tmp_path / 'contract.json'
    config_path.write_text(json.dumps({'dashscope_config_file': str(key_path)}), encoding='utf-8')
    cleanups = []
    service = _build_legal_chat_http_service(
        SimpleNamespace(contract_review_config_file=config_path, deepseek_api_key=None),
        cleanups=cleanups,
    )
    try:
        assert service.gateway_available
        assert service._model_ref == 'qwen3.6-flash'
        assert len(cleanups) == 1
    finally:
        for close in cleanups:
            await close()
