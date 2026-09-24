"""Local sentence-transformers embedding provider (supplier-isolated).

Implements the project-owned ``ModelProviderPort`` behind the Model Gateway.
All vendor specifics (sentence-transformers, torch, weight paths) stay in this
adapter; domain/orchestration code never sees them. The encoder callable is
injectable so the adapter can be unit-tested offline without loading a model.
"""

from __future__ import annotations

import math
import re
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
from lawyer_agent.infrastructure.providers.embedding_execution import BoundedEmbeddingRunner
from lawyer_agent.infrastructure.providers.embedding_windows import (
    TOKEN_LIMIT,
    WINDOW_BATCH_SIZE,
    parse_embedding_model_ref,
    token_windows,
)

_EncodeCallable = Callable[[Sequence[str]], Sequence[Sequence[float]]]


class LocalSentenceTransformerEmbeddingProvider(ModelProviderPort):
    """Embeddings from a locally loaded sentence-transformers model.

    The model is loaded lazily on the first embed call. Only the ``encode``
    behaviour is required, so tests can inject a fake encoder; a missing
    dependency or weight failure surfaces as ``ModelProviderUnavailable`` and is
    never fabricated into a result. Vectors are L2-normalised by default so the
    OpenSearch ``l2`` k-NN space ranks by cosine — the retrieval convention for
    BGE and Yuan embeddings.
    """

    def __init__(
        self,
        *,
        model_name_or_path: str,
        encode: _EncodeCallable | None = None,
        normalize_embeddings: bool = True,
        device: str = "cpu",
        local_files_only: bool = True,
        window_batch_size: int = WINDOW_BATCH_SIZE,
    ) -> None:
        if not isinstance(model_name_or_path, str) or not model_name_or_path.strip():
            raise ValueError("embedding model name or path must be non-empty")
        if not isinstance(normalize_embeddings, bool):
            raise ValueError("normalize_embeddings must be boolean")
        if not isinstance(device, str) or re.fullmatch(r"cpu|cuda(?::[0-9]+)?|mps", device) is None:
            raise ValueError("embedding device must be cpu, cuda, cuda:N or mps")
        if type(local_files_only) is not bool:
            raise ValueError("local_files_only must be boolean")
        if type(window_batch_size) is not int or not 1 <= window_batch_size <= WINDOW_BATCH_SIZE:
            raise ValueError(f"window_batch_size must be an integer from 1 to {WINDOW_BATCH_SIZE}")
        self._model_name_or_path, self._window_profile = parse_embedding_model_ref(
            model_name_or_path,
        )
        if self._window_profile and not normalize_embeddings:
            raise ValueError("token-window-mean-v1 requires L2 normalization")
        self._encode = encode
        self._normalize = normalize_embeddings
        self._device = device
        self._local_files_only = local_files_only
        self._window_batch_size = window_batch_size
        self._loaded: Any | None = None
        self._runner = BoundedEmbeddingRunner()

    def _encoder(self) -> _EncodeCallable:
        if self._encode is not None:
            return self._encode
        if self._loaded is None:
            import importlib

            try:
                sentence_transformers = importlib.import_module("sentence_transformers")
                encoder_class = sentence_transformers.SentenceTransformer
            except Exception as exc:  # noqa: BLE001 - dependency boundary
                raise ModelProviderUnavailable("sentence-transformers is not installed") from exc
            try:
                self._loaded = encoder_class(
                    self._model_name_or_path,
                    device=self._device,
                    local_files_only=self._local_files_only,
                    trust_remote_code=False,
                )
            except Exception as exc:  # noqa: BLE001 - weight boundary
                raise ModelProviderUnavailable("embedding model could not be loaded") from exc
        from typing import cast

        return cast(_EncodeCallable, self._loaded.encode)

    async def embed(
        self,
        *,
        texts: Sequence[str],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes, bytearray)):
            raise ModelProviderUnavailable("embed texts must be a non-empty sequence of strings")
        snapshot = tuple(texts)
        if not snapshot or any(not isinstance(text, str) or not text for text in snapshot):
            raise ModelProviderUnavailable("embed texts must be a non-empty sequence of strings")
        return await self._runner.run(
            lambda: self._embed_sync(snapshot),
            timeout_seconds=timeout_seconds,
        )

    async def aclose(self, *, timeout_seconds: float = 5.0) -> bool:
        """Stop admission and wait finitely; False does not mean computation stopped."""
        return await self._runner.aclose(timeout_seconds=timeout_seconds)

    def _embed_sync(self, texts: tuple[str, ...]) -> tuple[EmbeddingVector, ...]:
        # Loading, encoding and all potentially expensive vector conversion run
        # in the same bounded worker, including after the request stops waiting.
        encoder = self._encoder()
        try:
            if self._window_profile:
                return self._embed_windowed(texts, encoder)
            encoded = encoder(texts)
        except Exception as exc:  # noqa: BLE001 - provider boundary
            raise ModelProviderUnavailable("embedding encode failed") from exc
        try:
            return self._vectors(encoded, expected_rows=len(texts))
        except ModelProviderUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - vendor output boundary
            raise ModelProviderUnavailable("embedding vector conversion failed") from exc

    def _embed_windowed(
        self,
        texts: tuple[str, ...],
        encoder: _EncodeCallable,
    ) -> tuple[EmbeddingVector, ...]:
        model = self._loaded
        if (
            model is None
            or type(model.max_seq_length) is not int
            or model.max_seq_length != TOKEN_LIMIT
            or model.default_prompt_name is not None
        ):
            raise ModelProviderUnavailable("unsupported token-window model configuration")

        def count_tokens(text: str) -> int:
            encoded = model.tokenizer(text, add_special_tokens=True, truncation=False)
            return len(encoded["input_ids"])

        # Only one microbatch of window strings/vectors is retained. Accumulators
        # have the same cardinality as the required original-input output rows.
        sums: list[list[float]] = [[] for _ in texts]
        counts = [0 for _ in texts]
        pending: list[str] = []
        owners: list[int] = []

        def flush() -> None:
            self._validate_window_preprocessing(model, pending)
            vectors = self._vectors(encoder(tuple(pending)), expected_rows=len(pending))
            for owner, vector in zip(owners, vectors, strict=True):
                if not counts[owner]:
                    sums[owner] = list(vector.values)
                else:
                    if len(sums[owner]) != vector.dimension:
                        raise ModelProviderUnavailable("embedding window dimension mismatch")
                    for index, value in enumerate(vector.values):
                        sums[owner][index] += value
                counts[owner] += 1
            pending.clear()
            owners.clear()

        for owner, text in enumerate(texts):
            for piece in token_windows(text, count_tokens):
                pending.append(piece)
                owners.append(owner)
                if len(pending) == self._window_batch_size:
                    flush()
        if pending:
            flush()
        result: list[EmbeddingVector] = []
        for count, values in zip(counts, sums, strict=True):
            if count == 1:
                result.append(EmbeddingVector(values=tuple(values), dimension=len(values)))
            else:
                # L2(sum(unit windows)) equals L2(their equal arithmetic mean).
                result.extend(self._vectors((values,), expected_rows=1))
        return tuple(result)

    @staticmethod
    def _validate_window_preprocessing(model: Any, texts: list[str]) -> None:
        """Reject model preprocessing that changes/truncates the counted tokens."""
        actual = model.tokenize(texts)
        ids = actual["input_ids"]
        masks = actual["attention_mask"]
        if not isinstance(ids, list):
            ids = ids.tolist()
        if not isinstance(masks, list):
            masks = masks.tolist()
        if not isinstance(ids, list) or not isinstance(masks, list):
            raise ModelProviderUnavailable("invalid embedding preprocessing")
        if len(ids) != len(texts) or len(masks) != len(texts):
            raise ModelProviderUnavailable("invalid embedding preprocessing rows")
        for text, row, mask in zip(texts, ids, masks, strict=True):
            expected = model.tokenizer(text, add_special_tokens=True, truncation=False)["input_ids"]
            if (
                not isinstance(row, list)
                or not isinstance(mask, list)
                or not isinstance(expected, list)
                or not 0 < len(row) <= TOKEN_LIMIT
                or len(mask) != len(row)
                or not expected
                or len(expected) > TOKEN_LIMIT
                or any(type(value) is not int or value < 0 for value in row + expected)
                or any(type(value) is not int or value not in (0, 1) for value in mask)
                or [value for value, active in zip(row, mask, strict=True) if active] != expected
            ):
                raise ModelProviderUnavailable("unsupported embedding preprocessing")

    def _vectors(
        self,
        encoded: Sequence[Sequence[float]],
        *,
        expected_rows: int,
    ) -> tuple[EmbeddingVector, ...]:
        vectors: list[EmbeddingVector] = []
        for row in encoded:
            values = tuple(float(value) for value in row)
            if self._normalize:
                if not values or not all(math.isfinite(value) for value in values):
                    raise ModelProviderUnavailable("embedding vector cannot be L2-normalized")
                scale = max(abs(value) for value in values)
                if scale == 0.0:
                    raise ModelProviderUnavailable("embedding vector cannot be L2-normalized")
                # Scale before squaring to avoid overflow/underflow of finite model output.
                scaled = tuple(value / scale for value in values)
                norm = math.sqrt(sum(value * value for value in scaled))
                values = tuple(value / norm for value in scaled)
            vectors.append(EmbeddingVector(values=values, dimension=len(values)))
        if len(vectors) != expected_rows:
            raise ModelProviderUnavailable("embedding encode returned an unexpected row count")
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
