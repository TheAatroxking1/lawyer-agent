"""Local sentence-transformers embedding provider (supplier-isolated).

Implements the project-owned ``ModelProviderPort`` behind the Model Gateway.
All vendor specifics (sentence-transformers, torch, weight paths) stay in this
adapter; domain/orchestration code never sees them. The encoder callable is
injectable so the adapter can be unit-tested offline without loading a model.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from lawyer_agent.application.model_gateway import (
    ModelProviderPort,
    ModelProviderUnavailable,
)
from lawyer_agent.domain.model_gateway import (
    ChatMessage,
    EmbeddingVector,
    RankedDocument,
    TokenUsage,
)

_EncodeCallable = Callable[[Sequence[str]], Sequence[Sequence[float]]]


class LocalSentenceTransformerEmbeddingProvider(ModelProviderPort):
    """Embeddings from a locally loaded sentence-transformers model.

    The model is loaded lazily on the first embed call. Only the ``encode``
    behaviour is required, so tests can inject a fake encoder; a missing
    dependency or weight failure surfaces as ``ModelProviderUnavailable`` and is
    never fabricated into a result.
    """

    def __init__(
        self,
        *,
        model_name_or_path: str,
        encode: _EncodeCallable | None = None,
    ) -> None:
        if not isinstance(model_name_or_path, str) or not model_name_or_path.strip():
            raise ValueError("embedding model name or path must be non-empty")
        self._model_name_or_path = model_name_or_path
        self._encode = encode
        self._loaded: Any | None = None

    def _encoder(self) -> _EncodeCallable:
        if self._encode is not None:
            return self._encode
        if self._loaded is None:
            import importlib

            try:
                sentence_transformers = importlib.import_module(
                    "sentence_transformers"
                )
                encoder_class = sentence_transformers.SentenceTransformer
            except Exception as exc:  # noqa: BLE001 - dependency boundary
                raise ModelProviderUnavailable(
                    "sentence-transformers is not installed"
                ) from exc
            try:
                self._loaded = encoder_class(self._model_name_or_path)
            except Exception as exc:  # noqa: BLE001 - weight boundary
                raise ModelProviderUnavailable(
                    "embedding model could not be loaded"
                ) from exc
        from typing import cast

        return cast(_EncodeCallable, self._loaded.encode)

    async def embed(
        self,
        *,
        texts: Sequence[str],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        if not isinstance(texts, Sequence) or not texts:
            raise ModelProviderUnavailable("embed texts must not be empty")
        encoder = self._encoder()
        try:
            encoded = encoder(texts)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise ModelProviderUnavailable("embedding encode failed") from exc
        vectors: list[EmbeddingVector] = []
        for row in encoded:
            values = tuple(float(value) for value in row)
            vectors.append(
                EmbeddingVector(values=values, dimension=len(values))
            )
        if len(vectors) != len(texts):
            raise ModelProviderUnavailable(
                "embedding encode returned an unexpected row count"
            )
        return tuple(vectors)

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        timeout_seconds: float,
    ) -> tuple[RankedDocument, ...]:
        raise ModelProviderUnavailable("rerank is not configured for this provider")

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]:
        raise ModelProviderUnavailable("chat is not configured for this provider")
