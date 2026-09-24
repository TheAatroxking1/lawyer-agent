"""DeepSeek chat provider adapter (supplier-isolated, OpenAI-compatible).

Implements the project-owned ``ModelProviderPort.chat`` for DeepSeek's
chat/completions REST endpoint. All vendor specifics (base URL, model name,
Bearer credential, wire schema) live in this adapter; orchestration code only
sees ``(text, TokenUsage)``. The HTTP client is injectable so unit tests can
drive a MockTransport offline. Errors carry stable codes and never echo the
API key, the request body or raw provider text (spec 7.2).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from lawyer_agent.application.model_gateway import (
    ModelInputInvalid,
    ModelProviderInvalidResponse,
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

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


class DeepSeekChatProvider(ModelProviderPort):
    """Chat completions through the DeepSeek-compatible endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        model_name: str = DEFAULT_DEEPSEEK_MODEL,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("deepseek api key must be non-empty text")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("deepseek base url must be non-empty text")
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("deepseek model name must be non-empty text")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name.strip()
        self._client = client

    def _client_for(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        return httpx.AsyncClient(timeout=30.0)

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]:
        if not isinstance(messages, Sequence) or not messages:
            raise ModelInputInvalid("chat messages must be a non-empty sequence")
        if any(not isinstance(message, ChatMessage) for message in messages):
            raise ModelInputInvalid("chat messages must be strongly typed")
        if any(message.images for message in messages):
            raise ModelInputInvalid("images are not supported by this provider")
        payload = {
            "model": self._model_name,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": False,
        }
        try:
            response = await self._client_for().post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ModelProviderTimeout("deepseek chat provider timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelProviderUnavailable("deepseek chat provider failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise ModelProviderUnavailable(
                f"deepseek chat provider returned HTTP {response.status_code}"
            )
        return self._parse_response(response.text)

    async def chat_stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> AsyncIterator[str]:
        """Streams chat answer text deltas from the OpenAI-compatible endpoint.

        Same request shape as :meth:`chat` with ``stream: true``; each SSE
        ``data:`` line contributes the next ``choices[0].delta.content`` text.
        Malformed frames fail loudly (never silently dropped); ``[DONE]`` and a
        missing/empty delta finish the stream normally.
        """
        if not isinstance(messages, Sequence) or not messages:
            raise ModelInputInvalid("chat messages must be a non-empty sequence")
        if any(not isinstance(message, ChatMessage) for message in messages):
            raise ModelInputInvalid("chat messages must be strongly typed")
        if any(message.images for message in messages):
            raise ModelInputInvalid("images are not supported by this provider")
        payload = {
            "model": self._model_name,
            "messages": [
                {"role": message.role, "content": message.content}
                for message in messages
            ],
            "stream": True,
        }
        try:
            async with self._client_for().stream(
                "POST",
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=timeout_seconds,
            ) as response:
                if response.status_code < 200 or response.status_code >= 300:
                    raise ModelProviderUnavailable(
                        "deepseek chat provider returned HTTP "
                        f"{response.status_code}"
                    )
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    text = self._parse_stream_chunk(data)
                    if text:
                        yield text
        except httpx.TimeoutException as exc:
            raise ModelProviderTimeout("deepseek chat provider timed out") from exc
        except httpx.HTTPError as exc:
            raise ModelProviderUnavailable("deepseek chat provider failed") from exc

    @staticmethod
    def _parse_stream_chunk(data: str) -> str:
        try:
            body = json.loads(data)
        except json.JSONDecodeError as exc:
            raise ModelProviderInvalidResponse(
                "deepseek chat stream returned invalid JSON"
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
            raise ModelProviderInvalidResponse(
                "deepseek chat stream response is missing choices"
            )
        choices = body["choices"]
        # An empty-choices frame (e.g. a usage-only tail) ends a turn normally.
        if not choices:
            return ""
        first = choices[0]
        if not isinstance(first, dict):
            raise ModelProviderInvalidResponse(
                "deepseek chat stream choice is invalid"
            )
        delta = first.get("delta")
        if delta is None:
            return ""
        if not isinstance(delta, dict):
            raise ModelProviderInvalidResponse(
                "deepseek chat stream delta is invalid"
            )
        content = delta.get("content")
        if not isinstance(content, str):
            return ""
        return content

    def _parse_response(self, raw_text: str) -> tuple[str, TokenUsage]:
        try:
            body = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ModelProviderInvalidResponse(
                "deepseek chat provider returned invalid JSON"
            ) from exc
        if not isinstance(body, dict) or not isinstance(body.get("choices"), list):
            raise ModelProviderInvalidResponse(
                "deepseek chat provider response is missing choices"
            )
        choices = [choice for choice in body["choices"] if isinstance(choice, dict)]
        if not choices:
            raise ModelProviderInvalidResponse(
                "deepseek chat provider returned no choices"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise ModelProviderInvalidResponse(
                "deepseek chat provider response is missing message content"
            )
        text = message["content"]
        if not text.strip():
            raise ModelProviderInvalidResponse(
                "deepseek chat provider returned empty content"
            )
        usage = self._parse_usage(body.get("usage"))
        return text, usage

    @staticmethod
    def _parse_usage(raw: object) -> TokenUsage:
        if not isinstance(raw, dict):
            return TokenUsage()
        return TokenUsage(
            prompt_tokens=_as_token_count(raw.get("prompt_tokens")),
            completion_tokens=_as_token_count(raw.get("completion_tokens")),
            total_tokens=_as_token_count(raw.get("total_tokens")),
        )

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


def _as_token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    return 0
