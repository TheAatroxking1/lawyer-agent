"""Model Gateway application service (supplier-agnostic facade).

Hides concrete providers behind ``ModelProviderPort``. The gateway validates
inputs, times each call, maps provider failures to typed gateway errors and
records every attempt (success or failure) through ``ModelCallRecorderPort``.
With no fallback provider configured the gateway surfaces the provider error
directly; it never fabricates a result or a silent fallback.

Vendor-specific payloads and credentials belong in provider adapters, never
here or in the domain module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Protocol

from lawyer_agent.domain.model_gateway import (
    CallLimits,
    ChatMessage,
    EmbeddingVector,
    ModelCallRecord,
    ModelOperation,
    RankedDocument,
    TokenUsage,
)


class ModelGatewayError(Exception):
    """Base gateway error carrying a stable code."""

    code: str = "model_gateway_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ModelProviderUnavailable(ModelGatewayError):
    code = "model_provider_unavailable"


class ModelProviderTimeout(ModelGatewayError):
    code = "model_provider_timeout"


class ModelProviderInvalidResponse(ModelGatewayError):
    code = "model_provider_invalid_response"


class ModelInputInvalid(ModelGatewayError):
    code = "model_input_invalid"


class ModelCallRecorderPort(Protocol):
    async def append(self, record: ModelCallRecord) -> None: ...


class ModelProviderPort(Protocol):
    """Project-owned interface every concrete provider adapter implements."""

    async def embed(
        self,
        *,
        texts: Sequence[str],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]: ...

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        timeout_seconds: float,
    ) -> tuple[RankedDocument, ...]: ...

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]: ...


class ModelGateway:
    """Times, validates and records every model call through one provider."""

    def __init__(
        self,
        provider: ModelProviderPort,
        recorder: ModelCallRecorderPort,
        *,
        limits: CallLimits | None = None,
    ) -> None:
        if not hasattr(provider, "embed"):
            raise ValueError("model gateway requires a provider port")
        if not hasattr(recorder, "append"):
            raise ValueError("model gateway requires a call recorder")
        self._provider = provider
        self._recorder = recorder
        self._limits = limits if limits is not None else CallLimits()

    async def embed(
        self,
        *,
        model_ref: str,
        texts: Sequence[str],
        dimension: int,
    ) -> tuple[EmbeddingVector, ...]:
        _require_model_ref(model_ref)
        if not isinstance(texts, Sequence) or not texts:
            raise ModelInputInvalid("embed texts must be a non-empty sequence")
        if any(not isinstance(text, str) or not text for text in texts):
            raise ModelInputInvalid("embed texts must be non-empty strings")
        if (
            isinstance(dimension, bool)
            or not isinstance(dimension, int)
            or dimension <= 0
        ):
            raise ModelInputInvalid("embed dimension must be a positive integer")
        started = datetime.now(UTC)
        try:
            vectors = await self._provider.embed(
                texts=tuple(texts),
                dimension=dimension,
                timeout_seconds=self._limits.timeout_seconds,
            )
        except ModelGatewayError:
            await self._record_failure(model_ref, ModelOperation.EMBED, started)
            raise
        except TimeoutError as exc:
            await self._record_failure(model_ref, ModelOperation.EMBED, started)
            raise ModelProviderTimeout("model provider timed out") from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await self._record_failure(model_ref, ModelOperation.EMBED, started)
            raise ModelProviderUnavailable("model provider failed") from exc
        try:
            validated = tuple(
                EmbeddingVector(values=vector.values, dimension=dimension)
                for vector in vectors
            )
        except ValueError as exc:
            await self._record_failure(model_ref, ModelOperation.EMBED, started)
            raise ModelProviderInvalidResponse(str(exc)) from exc
        await self._record_success(
            model_ref,
            ModelOperation.EMBED,
            started,
            vector_count=len(validated),
        )
        return validated

    async def rerank(
        self,
        *,
        model_ref: str,
        query: str,
        documents: Sequence[str],
    ) -> tuple[RankedDocument, ...]:
        _require_model_ref(model_ref)
        if not isinstance(query, str) or not query:
            raise ModelInputInvalid("rerank query must be non-empty text")
        if not isinstance(documents, Sequence) or not documents:
            raise ModelInputInvalid("rerank documents must be a non-empty sequence")
        if any(not isinstance(doc, str) or not doc for doc in documents):
            raise ModelInputInvalid("rerank documents must be non-empty strings")
        started = datetime.now(UTC)
        try:
            ranked = await self._provider.rerank(
                query=query,
                documents=tuple(documents),
                timeout_seconds=self._limits.timeout_seconds,
            )
        except ModelGatewayError:
            await self._record_failure(model_ref, ModelOperation.RERANK, started)
            raise
        except TimeoutError as exc:
            await self._record_failure(model_ref, ModelOperation.RERANK, started)
            raise ModelProviderTimeout("model provider timed out") from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await self._record_failure(model_ref, ModelOperation.RERANK, started)
            raise ModelProviderUnavailable("model provider failed") from exc
        await self._record_success(model_ref, ModelOperation.RERANK, started)
        return tuple(ranked)

    async def chat(
        self,
        *,
        model_ref: str,
        messages: Sequence[ChatMessage],
    ) -> tuple[str, TokenUsage]:
        _require_model_ref(model_ref)
        if not isinstance(messages, Sequence) or not messages:
            raise ModelInputInvalid("chat messages must be a non-empty sequence")
        if any(not isinstance(message, ChatMessage) for message in messages):
            raise ModelInputInvalid("chat messages must be strongly typed")
        started = datetime.now(UTC)
        try:
            text, usage = await self._provider.chat(
                messages=tuple(messages),
                timeout_seconds=self._limits.timeout_seconds,
            )
        except ModelGatewayError:
            await self._record_failure(model_ref, ModelOperation.CHAT, started)
            raise
        except TimeoutError as exc:
            await self._record_failure(model_ref, ModelOperation.CHAT, started)
            raise ModelProviderTimeout("model provider timed out") from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await self._record_failure(model_ref, ModelOperation.CHAT, started)
            raise ModelProviderUnavailable("model provider failed") from exc
        if not isinstance(text, str) or not text:
            await self._record_failure(model_ref, ModelOperation.CHAT, started)
            raise ModelProviderInvalidResponse("chat returned no text")
        await self._record_success(model_ref, ModelOperation.CHAT, started, usage=usage)
        return text, usage

    async def chat_stream(
        self,
        *,
        model_ref: str,
        messages: Sequence[ChatMessage],
    ) -> AsyncIterator[str]:
        """Streams chat answer text deltas (token-level when the provider can).

        Validates inputs exactly like :meth:`chat` and records one call: success
        after a normal stream end (usage is unknown for streams, so it is left
        ``None``) or failure on any mid-stream provider error, which is mapped to
        a typed gateway error — nothing is fabricated, an empty stream is an
        invalid response.
        """
        _require_model_ref(model_ref)
        if not isinstance(messages, Sequence) or not messages:
            raise ModelInputInvalid("chat messages must be a non-empty sequence")
        if any(not isinstance(message, ChatMessage) for message in messages):
            raise ModelInputInvalid("chat messages must be strongly typed")
        stream_capable = getattr(self._provider, "chat_stream", None)
        if not callable(stream_capable):
            raise ModelProviderUnavailable(
                "chat streaming is not supported by the configured provider"
            )
        started = datetime.now(UTC)
        emitted = 0
        try:
            stream = stream_capable(
                messages=tuple(messages),
                timeout_seconds=self._limits.timeout_seconds,
            )
            async for text in stream:
                if not isinstance(text, str) or not text:
                    raise ModelProviderInvalidResponse("chat stream yielded no text")
                emitted += 1
                yield text
        except ModelGatewayError:
            await self._record_failure(model_ref, ModelOperation.CHAT_STREAM, started)
            raise
        except TimeoutError as exc:
            await self._record_failure(model_ref, ModelOperation.CHAT_STREAM, started)
            raise ModelProviderTimeout("model provider timed out") from exc
        except Exception as exc:  # noqa: BLE001 - provider boundary
            await self._record_failure(model_ref, ModelOperation.CHAT_STREAM, started)
            raise ModelProviderUnavailable("model provider failed") from exc
        if emitted == 0:
            await self._record_failure(model_ref, ModelOperation.CHAT_STREAM, started)
            raise ModelProviderInvalidResponse("chat stream returned no text")
        await self._record_success(model_ref, ModelOperation.CHAT_STREAM, started)

    async def _record_success(
        self,
        model_ref: str,
        operation: ModelOperation,
        started: datetime,
        *,
        usage: TokenUsage | None = None,
        vector_count: int | None = None,
    ) -> None:
        await self._recorder.append(
            ModelCallRecord(
                operation=operation,
                model_ref=model_ref,
                status="success",
                latency_ms=_elapsed_ms(started),
                usage=usage,
                vector_count=vector_count,
            )
        )

    async def _record_failure(
        self,
        model_ref: str,
        operation: ModelOperation,
        started: datetime,
    ) -> None:
        await self._recorder.append(
            ModelCallRecord(
                operation=operation,
                model_ref=model_ref,
                status="error",
                latency_ms=_elapsed_ms(started),
            )
        )


def _require_model_ref(model_ref: str) -> None:
    if not isinstance(model_ref, str) or not model_ref.strip():
        raise ModelInputInvalid("model_ref must be non-empty text")


def _elapsed_ms(started: datetime) -> int:
    elapsed = datetime.now(UTC) - started
    return int(elapsed.total_seconds() * 1000)
