from __future__ import annotations

from collections.abc import Sequence

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
    EmbeddingVector,
    ModelCallRecord,
    ModelOperation,
    RankedDocument,
    TokenUsage,
    retry_delay_seconds,
)


class MemoryRecorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


class DeterministicProvider:
    """Synthetic provider for gateway tests (project-interface compliant)."""

    def __init__(
        self,
        *,
        fail_embed: Exception | None = None,
        fail_rerank: Exception | None = None,
        fail_chat: Exception | None = None,
        bad_dimension: bool = False,
        empty_chat: bool = False,
    ) -> None:
        self.fail_embed = fail_embed
        self.fail_rerank = fail_rerank
        self.fail_chat = fail_chat
        self.bad_dimension = bad_dimension
        self.empty_chat = empty_chat
        self.embed_timeout = 0.0
        self.rerank_timeout = 0.0
        self.chat_timeout = 0.0

    async def embed(
        self,
        *,
        texts: Sequence[str],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        self.embed_timeout = timeout_seconds
        if self.fail_embed is not None:
            raise self.fail_embed
        effective_dimension = dimension if not self.bad_dimension else dimension + 1

        def vector_for(text: str) -> EmbeddingVector:
            seed = sum((index + 1) * ord(char) for index, char in enumerate(text))
            values = tuple(
                float((seed * (position + 1)) % 97) / 97.0
                for position in range(effective_dimension)
            )
            return EmbeddingVector(values=values, dimension=effective_dimension)

        return tuple(vector_for(text) for text in texts)

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        timeout_seconds: float,
    ) -> tuple[RankedDocument, ...]:
        self.rerank_timeout = timeout_seconds
        if self.fail_rerank is not None:
            raise self.fail_rerank
        return tuple(
            RankedDocument(index=index, score=1.0 / (index + 1))
            for index in range(len(documents))
        )

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]:
        self.chat_timeout = timeout_seconds
        if self.fail_chat is not None:
            raise self.fail_chat
        if self.empty_chat:
            return "", TokenUsage()
        return "法律问答占位回复", TokenUsage(prompt_tokens=10, completion_tokens=5)


def _gateway(
    provider: DeterministicProvider, recorder: MemoryRecorder
) -> ModelGateway:
    return ModelGateway(provider, recorder, limits=CallLimits(timeout_seconds=12.5))


async def test_gateway_embed_success_records_call_and_forwards_timeout() -> None:
    provider = DeterministicProvider()
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    vectors = await gateway.embed(
        model_ref="yuan-embedding-2.0-zh", texts=["合同", "租赁"], dimension=8
    )
    assert len(vectors) == 2
    assert all(vector.dimension == 8 for vector in vectors)
    assert provider.embed_timeout == 12.5
    assert len(recorder.records) == 1
    record = recorder.records[0]
    assert record.operation is ModelOperation.EMBED
    assert record.model_ref == "yuan-embedding-2.0-zh"
    assert record.status == "success"
    assert record.vector_count == 2
    assert record.error_code is None


async def test_gateway_rejects_blank_model_ref_and_empty_inputs() -> None:
    provider = DeterministicProvider()
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    with pytest.raises(ModelInputInvalid):
        await gateway.embed(model_ref="  ", texts=["x"], dimension=8)
    with pytest.raises(ModelInputInvalid):
        await gateway.embed(model_ref="m", texts=[], dimension=8)
    with pytest.raises(ModelInputInvalid):
        await gateway.embed(model_ref="m", texts=[""], dimension=8)
    with pytest.raises(ModelInputInvalid):
        await gateway.embed(model_ref="m", texts=["x"], dimension=0)
    assert recorder.records == []


async def test_gateway_maps_provider_exceptions_without_fabricating_success() -> None:
    provider = DeterministicProvider(fail_embed=RuntimeError("backend down"))
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    with pytest.raises(ModelProviderUnavailable):
        await gateway.embed(model_ref="m", texts=["x"], dimension=8)
    assert len(recorder.records) == 1
    assert recorder.records[0].status == "error"
    assert recorder.records[0].error_code is None  # failure record carries no code yet


async def test_gateway_maps_timeout_to_typed_error() -> None:
    provider = DeterministicProvider(fail_embed=TimeoutError())
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    with pytest.raises(ModelProviderTimeout):
        await gateway.embed(model_ref="m", texts=["x"], dimension=8)
    assert recorder.records[0].status == "error"


async def test_gateway_rejects_dimension_mismatch_from_provider() -> None:
    provider = DeterministicProvider(bad_dimension=True)
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    with pytest.raises(ModelProviderInvalidResponse):
        await gateway.embed(model_ref="m", texts=["x"], dimension=8)
    assert recorder.records[0].status == "error"


async def test_gateway_rerank_and_chat_record_success() -> None:
    provider = DeterministicProvider()
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    ranked = await gateway.rerank(model_ref="reranker", query="违约金", documents=["a", "b"])
    assert [item.index for item in ranked] == [0, 1]
    text, usage = await gateway.chat(
        model_ref="deepseek",
        messages=[ChatMessage(role="user", content="第一百条是什么？")],
    )
    assert text
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 5
    assert provider.chat_timeout == 12.5
    assert [record.operation for record in recorder.records] == [
        ModelOperation.RERANK,
        ModelOperation.CHAT,
    ]
    assert all(record.status == "success" for record in recorder.records)
    assert recorder.records[1].usage is not None
    assert recorder.records[1].usage.prompt_tokens == 10


async def test_gateway_chat_empty_response_is_invalid() -> None:
    provider = DeterministicProvider(empty_chat=True)
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    with pytest.raises(ModelProviderInvalidResponse):
        await gateway.chat(
            model_ref="deepseek",
            messages=[ChatMessage(role="user", content="q")],
        )
    assert recorder.records[0].status == "error"


async def test_gateway_failure_record_and_success_record_both_counted() -> None:
    provider = DeterministicProvider(fail_rerank=RuntimeError("down"))
    recorder = MemoryRecorder()
    gateway = _gateway(provider, recorder)
    with pytest.raises(ModelProviderUnavailable):
        await gateway.rerank(model_ref="reranker", query="q", documents=["d"])
    await gateway.embed(model_ref="m", texts=["ok"], dimension=4)
    assert [record.status for record in recorder.records] == ["error", "success"]


def test_call_limits_validate_fields() -> None:
    with pytest.raises(ValueError):
        CallLimits(timeout_seconds=0)
    with pytest.raises(ValueError):
        CallLimits(max_attempts=0)
    with pytest.raises(ValueError):
        CallLimits(backoff_base_seconds=-1)


def test_retry_delay_is_bounded_and_exhausts_budget() -> None:
    limits = CallLimits(max_attempts=3, backoff_base_seconds=1.0, backoff_cap_seconds=10.0)
    assert retry_delay_seconds(1, limits) == 1.0
    assert retry_delay_seconds(2, limits) == 2.0
    with pytest.raises(ValueError):
        retry_delay_seconds(3, limits)  # attempt equals max_attempts -> exhausted
    with pytest.raises(ValueError):
        retry_delay_seconds(0, limits)


def test_retry_delay_respects_cap_and_zero_base() -> None:
    capped = CallLimits(max_attempts=6, backoff_base_seconds=8.0, backoff_cap_seconds=10.0)
    assert retry_delay_seconds(2, capped) == 10.0
    zero = CallLimits(max_attempts=3, backoff_base_seconds=0.0, backoff_cap_seconds=10.0)
    assert retry_delay_seconds(1, zero) == 0.0


def test_embedding_vector_validates_dimension_and_finite_values() -> None:
    vector = EmbeddingVector(values=(0.1, 0.2, 0.3), dimension=3)
    assert vector.dimension == 3
    with pytest.raises(ValueError):
        EmbeddingVector(values=(0.1, 0.2), dimension=3)
    with pytest.raises(ValueError):
        EmbeddingVector(values=(float("nan"), 0.2, 0.3), dimension=3)
    with pytest.raises(ValueError):
        EmbeddingVector(values=(1, 2, 3), dimension=3)  # ints are not floats


def test_chat_message_and_ranked_document_validate() -> None:
    message = ChatMessage(role="user", content="hi")
    assert message.content == "hi"
    with pytest.raises(ValueError):
        ChatMessage(role="admin", content="hi")
    ranked = RankedDocument(index=2, score=0.9)
    assert ranked.index == 2
    with pytest.raises(ValueError):
        RankedDocument(index=-1, score=0.9)


def test_token_usage_validates_non_negative() -> None:
    usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
    assert usage.total_tokens == 3
    with pytest.raises(ValueError):
        TokenUsage(prompt_tokens=-1)
