"""Aliyun Model Studio chat adapter with isolated credential loading."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math
import os
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from lawyer_agent.application.model_gateway import (
    ModelInputInvalid,
    ModelProviderInvalidResponse,
    ModelProviderJsonGenerationError,
    ModelProviderOutputTruncated,
    ModelProviderPort,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from lawyer_agent.domain.model_gateway import (
    ChatMessage,
    EmbeddingVector,
    RankedDocument,
    TokenUsage,
)

DEFAULT_DASHSCOPE_BASE_URL = (
    "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
)
DEFAULT_DASHSCOPE_MODEL = "qwen3.6-flash"
_MAX_CONFIG_BYTES = 64 * 1024
_MAX_ERROR_BYTES = 64 * 1024
_MAX_TOKENS = 65_536
_JSON_GENERATION_FAILURE_MESSAGE = (
    "Model output became abnormal while generating a JSON response for response_format"
)


class DashScopeConfigurationError(ValueError):
    """Safe configuration failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DashScopeSettings(BaseModel):
    """Validated DashScope credential and non-secret endpoint settings."""

    model_config = ConfigDict(
        frozen=True, extra="forbid", hide_input_in_errors=True
    )

    api_key: SecretStr = Field(repr=False, exclude=True)
    base_url: str = DEFAULT_DASHSCOPE_BASE_URL
    model_name: str = DEFAULT_DASHSCOPE_MODEL

    @model_validator(mode="after")
    def validate_endpoint_and_model(self) -> DashScopeSettings:
        if not _is_trusted_base_url(self.base_url) or not self.model_name.strip():
            raise ValueError("DashScope endpoint or model is invalid")
        return self

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> DashScopeSettings:
        env = os.environ if environ is None else environ
        env_key = env.get("DASHSCOPE_API_KEY", "").strip()
        configured_path = path
        if configured_path is None and env.get("LAWYER_DASHSCOPE_API_KEY_FILE"):
            configured_path = Path(env["LAWYER_DASHSCOPE_API_KEY_FILE"])
        if configured_path is not None and env_key:
            raise DashScopeConfigurationError(
                "dashscope_key_sources_conflict",
                "DashScope API key file and environment value cannot both be configured",
            )
        if configured_path is not None:
            api_key, file_base_url, file_model_name = _read_config_file(configured_path)
        elif env_key:
            api_key = env_key
            file_base_url = None
            file_model_name = None
        else:
            raise DashScopeConfigurationError(
                "dashscope_api_key_missing", "DashScope API key is not configured"
            )

        base_url = env.get(
            "LAWYER_DASHSCOPE_BASE_URL", file_base_url or DEFAULT_DASHSCOPE_BASE_URL
        ).strip()
        model_name = env.get(
            "LAWYER_DASHSCOPE_MODEL", file_model_name or DEFAULT_DASHSCOPE_MODEL
        ).strip()
        if not _is_trusted_base_url(base_url) or not model_name:
            raise DashScopeConfigurationError(
                "dashscope_config_invalid", "DashScope configuration is invalid"
            )
        return cls(api_key=SecretStr(api_key), base_url=base_url.rstrip("/"), model_name=model_name)


def _message_payload(message: ChatMessage) -> dict[str, object]:
    if not message.images:
        return {"role": message.role, "content": message.content}
    content: list[dict[str, object]] = [{"type": "text", "text": message.content}]
    for image in message.images:
        encoded = base64.b64encode(image.data).decode("ascii")
        content.append({"type": "image_url", "image_url": {
            "url": f"data:{image.media_type};base64,{encoded}",
        }})
    return {"role": message.role, "content": content}


class DashScopeChatProvider(ModelProviderPort):
    """Bounded Qwen chat through DashScope's OpenAI-compatible API."""

    def __init__(
        self,
        *,
        settings: DashScopeSettings,
        client: httpx.AsyncClient,
        max_tokens: int | None = 4096,
        max_response_bytes: int = 1024 * 1024,
        json_mode: bool = False,
        stream_response: bool = False,
        max_images: int = 4,
    ) -> None:
        if client.follow_redirects:
            raise ValueError("DashScope HTTP client must disable redirects")
        if max_tokens is not None and (
            isinstance(max_tokens, bool) or not 1 <= max_tokens <= _MAX_TOKENS
        ):
            raise ValueError("max_tokens must be None or between 1 and 65536")
        if isinstance(max_response_bytes, bool) or max_response_bytes < 256:
            raise ValueError("max_response_bytes must be at least 256")
        self._settings = settings
        self._client = client
        self._max_tokens = max_tokens
        self._max_response_bytes = max_response_bytes
        self._json_mode = json_mode
        self._stream_response = stream_response
        if type(max_images) is not int or not 1 <= max_images <= 30:
            raise ValueError("max_images must be between 1 and 30")
        self._max_images = max_images

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]:
        if (
            isinstance(messages, (str, bytes, bytearray))
            or not isinstance(messages, Sequence)
            or not messages
            or any(
                not isinstance(message, ChatMessage) or not message.content.strip()
                for message in messages
            )
        ):
            raise ModelInputInvalid("chat messages must be a non-empty typed sequence")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ModelInputInvalid("chat timeout must be a positive finite number")
        if sum(len(message.images) for message in messages) > self._max_images:
            raise ModelInputInvalid("too many chat images")
        payload = {
            "model": self._settings.model_name,
            "messages": [
                _message_payload(message) for message in messages
            ],
            "stream": self._stream_response,
            "enable_thinking": False,
        }
        if self._stream_response:
            payload["stream_options"] = {"include_usage": True}
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        if self._json_mode:
            payload["response_format"] = {"type": "json_object"}
            # Structured records reuse keys, identifiers and exact quotations.
            # Do not penalize those required repetitions as creative prose.
            payload["presence_penalty"] = 0.0
        try:
            async with asyncio.timeout(float(timeout_seconds)):
                async with self._client.stream(
                    "POST",
                    f"{self._settings.base_url}/chat/completions",
                    headers={
                        "Authorization": (
                            f"Bearer {self._settings.api_key.get_secret_value()}"
                        )
                    },
                    json=payload,
                    timeout=float(timeout_seconds),
                    follow_redirects=False,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        logging.getLogger(__name__).warning(
                            "dashscope_chat_failed http_status=%d", response.status_code,
                        )
                        if response.status_code == 400:
                            await _check_json_generation_error_response(
                                response, max_bytes=min(_MAX_ERROR_BYTES, self._max_response_bytes),
                            )
                        raise ModelProviderUnavailable(
                            f"dashscope chat provider returned HTTP {response.status_code}"
                        )
                    if self._stream_response:
                        return await self._collect_stream(response)
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > self._max_response_bytes:
                            raise ModelProviderInvalidResponse(
                                "dashscope chat provider response is too large"
                            )
        except TimeoutError as exc:
            raise ModelProviderTimeout("dashscope chat provider timed out") from exc
        except httpx.TimeoutException as exc:
            raise ModelProviderTimeout("dashscope chat provider timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelProviderUnavailable("dashscope chat provider failed") from exc
        return _parse_response(bytes(raw))

    async def _collect_stream(self, response: httpx.Response) -> tuple[str, TokenUsage]:
        """Receive incrementally; release only complete content with actual usage."""
        parts: list[str] = []
        usage: object = None
        finish: str | None = None
        buffer = b""
        content_bytes = wire_bytes = 0
        async for chunk in response.aiter_bytes():
            wire_bytes += len(chunk)
            if wire_bytes > self._max_response_bytes * 8:
                raise ModelProviderInvalidResponse("dashscope stream exceeds wire limit")
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if not line.startswith(b"data:"):
                    continue
                data = line[5:].strip()
                if data == b"[DONE]":
                    if finish is None:
                        raise ModelProviderInvalidResponse("dashscope stream incomplete")
                    return _parse_response(json.dumps({
                        "choices": [{"finish_reason": finish,
                                     "message": {"content": "".join(parts)}}],
                        "usage": usage,
                    }, ensure_ascii=False).encode("utf-8"))
                try:
                    frame = json.loads(data)
                    _raise_json_generation_error(frame)
                    choices = frame["choices"]
                    if not isinstance(choices, list) or len(choices) > 1:
                        raise ValueError
                    if frame.get("usage") is not None:
                        usage = frame["usage"]
                    if not choices:
                        continue
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    content = delta.get("content")
                    reason = choice.get("finish_reason")
                    if reason not in (None, "stop", "length") or delta.get("tool_calls"):
                        raise ValueError
                    if content is not None and not isinstance(content, str):
                        raise ValueError
                    if finish is not None and (content or reason is not None):
                        raise ValueError
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    raise ModelProviderInvalidResponse("dashscope stream invalid") from exc
                if content:
                    content_bytes += len(content.encode("utf-8"))
                    if content_bytes > self._max_response_bytes:
                        raise ModelProviderInvalidResponse("dashscope stream content too large")
                    parts.append(content)
                if reason is not None:
                    finish = reason
            if len(buffer) > self._max_response_bytes:
                raise ModelProviderInvalidResponse("dashscope stream frame too large")
        raise ModelProviderInvalidResponse("dashscope stream incomplete")

    async def chat_stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> AsyncIterator[str]:
        if (
            isinstance(messages, (str, bytes)) or not messages
            or any(
                not isinstance(item, ChatMessage) or not item.content.strip() for item in messages
            )
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ModelInputInvalid("invalid chat stream input")
        if sum(len(message.images) for message in messages) > self._max_images:
            raise ModelInputInvalid("too many chat images")
        payload = {
            "model": self._settings.model_name,
            "messages": [_message_payload(item) for item in messages],
            "stream": True, "enable_thinking": False,
        }
        if self._max_tokens is not None:
            payload["max_tokens"] = self._max_tokens
        stopped = False
        has_text = False
        size = 0
        buffer = b""
        try:
            async with asyncio.timeout(timeout_seconds):
                async with self._client.stream(
                    "POST", f"{self._settings.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._settings.api_key.get_secret_value()}"
                    },
                    json=payload, timeout=timeout_seconds, follow_redirects=False,
                ) as response:
                    if not 200 <= response.status_code < 300:
                        raise ModelProviderUnavailable("dashscope chat stream unavailable")
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_response_bytes:
                            raise ModelProviderInvalidResponse("dashscope stream exceeds limit")
                        buffer += chunk
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if not line.startswith(b"data:"):
                                continue
                            data = line[5:].strip()
                            if data == b"[DONE]":
                                if not stopped or not has_text:
                                    raise ModelProviderInvalidResponse(
                                        "dashscope stream incomplete"
                                    )
                                return
                            try:
                                frame = json.loads(data)
                                choices = frame["choices"]
                                if not isinstance(choices, list):
                                    raise ValueError
                                if not choices:
                                    continue
                                choice = choices[0]
                                finish = choice.get("finish_reason")
                                delta = choice.get("delta", {})
                                content = delta.get("content")
                                if finish not in (None, "stop") or delta.get("tool_calls"):
                                    raise ValueError
                                if content is not None and not isinstance(content, str):
                                    raise ValueError
                                if content and stopped:
                                    raise ValueError
                            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                                raise ModelProviderInvalidResponse(
                                    "dashscope stream invalid"
                                ) from exc
                            if content:
                                has_text = has_text or bool(content.strip())
                                yield content
                            stopped = stopped or finish == "stop"
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ModelProviderTimeout("dashscope chat stream timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelProviderUnavailable("dashscope chat stream failed") from exc
        raise ModelProviderInvalidResponse("dashscope stream incomplete")

    async def embed(
        self,
        *,
        texts: Sequence[str],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        del texts, dimension, timeout_seconds
        raise ModelProviderUnavailable("embed is not supported by this provider")

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        timeout_seconds: float,
    ) -> tuple[RankedDocument, ...]:
        del query, documents, timeout_seconds
        raise ModelProviderUnavailable("rerank is not supported by this provider")


async def _check_json_generation_error_response(
    response: httpx.Response, *, max_bytes: int,
) -> None:
    raw = bytearray()
    async for chunk in response.aiter_bytes():
        if len(raw) + len(chunk) > max_bytes:
            return
        raw.extend(chunk)
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        return
    _raise_json_generation_error(payload)


def _raise_json_generation_error(payload: object) -> None:
    if not isinstance(payload, dict):
        return
    error = payload.get("error")
    if not isinstance(error, dict):
        return
    message = error.get("message")
    if (
        error.get("code") == "invalid_parameter_error"
        and isinstance(message, str)
        and _JSON_GENERATION_FAILURE_MESSAGE in message
    ):
        raise ModelProviderJsonGenerationError("dashscope JSON generation failed")


def _read_config_file(path: Path) -> tuple[str, str | None, str | None]:
    if not path.is_absolute() or not path.is_file():
        raise DashScopeConfigurationError(
            "dashscope_config_invalid", "DashScope key path must be an absolute regular file"
        )
    try:
        if path.stat().st_size > _MAX_CONFIG_BYTES:
            raise DashScopeConfigurationError(
                "dashscope_config_invalid", "DashScope key file is too large"
            )
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw, object_pairs_hook=_unique_object)
    except DashScopeConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise DashScopeConfigurationError(
            "dashscope_config_invalid", "DashScope key file is invalid"
        ) from exc
    allowed_fields = {
        "DASHSCOPE_API_KEY",
        "LAWYER_DASHSCOPE_BASE_URL",
        "LAWYER_DASHSCOPE_MODEL",
    }
    if (
        not isinstance(parsed, dict)
        or "DASHSCOPE_API_KEY" not in parsed
        or not set(parsed) <= allowed_fields
    ):
        raise DashScopeConfigurationError(
            "dashscope_config_invalid", "DashScope key file is invalid"
        )
    api_key = parsed["DASHSCOPE_API_KEY"]
    if not isinstance(api_key, str) or not api_key.strip():
        raise DashScopeConfigurationError(
            "dashscope_config_invalid", "DashScope key file is invalid"
        )
    base_url = parsed.get("LAWYER_DASHSCOPE_BASE_URL")
    model_name = parsed.get("LAWYER_DASHSCOPE_MODEL")
    if (base_url is not None and not isinstance(base_url, str)) or (
        model_name is not None and not isinstance(model_name, str)
    ):
        raise DashScopeConfigurationError(
            "dashscope_config_invalid", "DashScope key file is invalid"
        )
    return api_key.strip(), base_url, model_name


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _is_trusted_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        trusted_host = (
            host == "dashscope.aliyuncs.com"
            or (host.startswith("dashscope-") and host.endswith(".aliyuncs.com"))
            or (host.startswith("dashscope.") and host.endswith(".aliyuncs.com"))
            or host.endswith(".dashscope.aliyuncs.com")
            or host.endswith(".maas.aliyuncs.com")
        )
        return bool(
            parsed.scheme == "https"
            and trusted_host
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def _parse_response(raw: bytes) -> tuple[str, TokenUsage]:
    try:
        body = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ModelProviderInvalidResponse(
            "dashscope chat provider returned invalid JSON"
        ) from exc
    if not isinstance(body, dict):
        raise ModelProviderInvalidResponse("dashscope chat provider response is invalid")
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelProviderInvalidResponse(
            "dashscope chat provider response is missing choices"
        )
    message = choices[0].get("message")
    if choices[0].get("finish_reason") == "length":
        raise ModelProviderOutputTruncated(
            "dashscope chat provider returned truncated content"
        )
    if not isinstance(message, dict):
        raise ModelProviderInvalidResponse(
            "dashscope chat provider response is missing message"
        )
    if choices[0].get("finish_reason") != "stop" or message.get("tool_calls") not in (
        None,
        [],
    ):
        raise ModelProviderInvalidResponse(
            "dashscope chat provider returned incomplete content"
        )
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ModelProviderInvalidResponse(
            "dashscope chat provider response is missing content"
        )
    usage = body.get("usage")
    if not isinstance(usage, dict):
        raise ModelProviderInvalidResponse(
            "dashscope chat provider response is missing usage"
        )
    counts = tuple(
        _token_count(usage.get(name))
        for name in ("prompt_tokens", "completion_tokens", "total_tokens")
    )
    if any(value is None for value in counts):
        raise ModelProviderInvalidResponse(
            "dashscope chat provider response has invalid usage"
        )
    return content, TokenUsage(counts[0], counts[1], counts[2])  # type: ignore[arg-type]


def _token_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
