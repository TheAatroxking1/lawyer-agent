"""Model Gateway domain: supplier-agnostic primitives (no provider payloads).

The Gateway hides concrete providers (Embedding/Rerank/DeepSeek ...) behind the
application's own interface. This module only holds pure values: capabilities,
dimension-validated embedding vectors, bounded-retry math and call records.
Provider-specific schemas, credentials and model names must never live here.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

_LARGE = 1_000_000_000


class ModelOperation(StrEnum):
    EMBED = "embed"
    RERANK = "rerank"
    CHAT = "chat"
    CHAT_STREAM = "chat_stream"


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """Dense vector with a fixed declared dimension."""

    values: tuple[float, ...]
    dimension: int

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, int) or self.dimension <= 0:
            raise ValueError("embedding dimension must be a positive integer")
        if not isinstance(self.values, tuple) or len(self.values) != self.dimension:
            raise ValueError(
                "embedding vector length must match the declared dimension"
            )
        for value in self.values:
            if not isinstance(value, float) or not math.isfinite(value):
                raise ValueError("embedding values must be finite floats")


@dataclass(frozen=True, slots=True)
class RankedDocument:
    """Rerank result: index into the original document list + its score."""

    index: int
    score: float

    def __post_init__(self) -> None:
        if isinstance(self.index, bool) or not isinstance(self.index, int):
            raise ValueError("ranked index must be an integer")
        if self.index < 0:
            raise ValueError("ranked index must not be negative")
        if not isinstance(self.score, float) or not math.isfinite(self.score):
            raise ValueError("ranked score must be a finite float")


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str

    def __post_init__(self) -> None:
        if self.role not in {"system", "user", "assistant"}:
            raise ValueError("chat role must be system, user or assistant")
        if not isinstance(self.content, str):
            raise ValueError("chat content must be text")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token accounting; vendor fields map in the provider adapter only."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        for value, name in (
            (self.prompt_tokens, "prompt_tokens"),
            (self.completion_tokens, "completion_tokens"),
            (self.total_tokens, "total_tokens"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class CallLimits:
    """Bounded timeout/retry limits; retries are always finite."""

    timeout_seconds: float = 30.0
    max_attempts: int = 3
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 8.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.timeout_seconds, float)
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be a positive finite float")
        if (
            isinstance(self.max_attempts, bool)
            or not isinstance(self.max_attempts, int)
            or self.max_attempts < 1
        ):
            raise ValueError("max_attempts must be a positive integer")
        if (
            not isinstance(self.backoff_base_seconds, float)
            or not math.isfinite(self.backoff_base_seconds)
            or self.backoff_base_seconds < 0
        ):
            raise ValueError("backoff_base_seconds must be a non-negative finite float")
        if (
            not isinstance(self.backoff_cap_seconds, float)
            or not math.isfinite(self.backoff_cap_seconds)
            or self.backoff_cap_seconds < 0
        ):
            raise ValueError("backoff_cap_seconds must be a non-negative finite float")


def retry_delay_seconds(attempt: int, limits: CallLimits) -> float:
    """Backoff delay before the given 1-based retry attempt (exponential, capped).

    ``attempt`` counts the retries already performed (1 = first retry). The
    delay never grows without bound; ``max_attempts`` caps the retry count
    itself, so callers can never schedule more than a bounded series.
    """
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if not isinstance(limits, CallLimits):
        raise ValueError("retry limits must be strongly typed")
    if attempt >= limits.max_attempts:
        raise ValueError("retry attempt exceeds the configured retry budget")
    if limits.backoff_base_seconds == 0:
        return 0.0
    raw = limits.backoff_base_seconds * float(2 ** (attempt - 1))
    return min(raw, limits.backoff_cap_seconds)


@dataclass(frozen=True, slots=True)
class ModelCallRecord:
    """Observability row produced for every gateway call (success or failure)."""

    operation: ModelOperation
    model_ref: str
    status: Literal["success", "error"]
    latency_ms: int
    error_code: str | None = None
    usage: TokenUsage | None = None
    vector_count: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ModelOperation):
            raise ValueError("call record operation must be strongly typed")
        if not isinstance(self.model_ref, str) or not self.model_ref:
            raise ValueError("call record model_ref must be non-empty text")
        if self.status not in {"success", "error"}:
            raise ValueError("call record status must be success or error")
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise ValueError("call record latency_ms must be a non-negative integer")
        if self.vector_count is not None and (
            isinstance(self.vector_count, bool)
            or not isinstance(self.vector_count, int)
            or self.vector_count < 0
        ):
            raise ValueError("call record vector_count must be a non-negative integer")
