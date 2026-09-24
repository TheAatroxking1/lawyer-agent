from __future__ import annotations

import math
import sys
from types import SimpleNamespace

import pytest

from lawyer_agent.application.model_gateway import ModelProviderUnavailable
from lawyer_agent.infrastructure.providers.embedding import (
    LocalSentenceTransformerEmbeddingProvider,
)
from lawyer_agent.infrastructure.providers.embedding_windows import (
    WINDOW_BATCH_SIZE,
    parse_embedding_model_ref,
    token_windows,
)


def token_count(text: str) -> int:
    return 2 + sum(4 if char == "😀" else 1 for char in text)


def test_profile_identity_is_strict_and_separates_weight_path():
    assert parse_embedding_model_ref("weights") == ("weights", False)
    assert parse_embedding_model_ref("weights#token-window-mean-v1") == ("weights", True)
    for value in ("weights#unknown", "#token-window-mean-v1", "weights#x#token-window-mean-v1"):
        with pytest.raises(ValueError):
            parse_embedding_model_ref(value)


@pytest.mark.parametrize(
    "text", ["法" * 510, "法" * 511, "😀" * 400, "  法\n" * 400, "同文" * 900, "a" * 5000 + "尾"]
)
def test_windows_preserve_all_original_characters_without_overlap(text):
    pieces = tuple(token_windows(text, token_count))
    assert "".join(pieces) == text
    assert all(piece and token_count(piece) <= 512 for piece in pieces)
    if token_count(text) <= 512:
        assert pieces == (text,)


def test_unrepresentable_character_fails_closed():
    with pytest.raises(ValueError):
        tuple(token_windows("x", lambda text: 513))


class FakeModel:
    max_seq_length = 512
    default_prompt_name = None

    def __init__(self):
        self.calls = []

    def tokenizer(self, text, **kwargs):
        assert kwargs["truncation"] is False
        return {"input_ids": [1] * token_count(text)}

    def tokenize(self, texts):
        ids = [[1] * token_count(text) for text in texts]
        width = max(map(len, ids))
        return {
            "input_ids": [row + [0] * (width - len(row)) for row in ids],
            "attention_mask": [[1] * len(row) + [0] * (width - len(row)) for row in ids],
        }

    def encode(self, texts):
        self.calls.append(tuple(texts))
        return [[1.0, float(text.count("尾"))] for text in texts]


def install(monkeypatch, model):
    paths = []

    def constructor(path, **kwargs):
        paths.append(path)
        return model

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=constructor)
    )
    return paths


async def test_profile_encodes_tail_and_preserves_short_rows_with_bounded_batches(monkeypatch):
    model = FakeModel()
    paths = install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1"
    )
    try:
        texts = ("短", "法" * 520 + "尾", "法" * 9000)
        rows = await provider.embed(texts=texts, dimension=2, timeout_seconds=5.0)
        assert paths == ["weights"]
        assert len(rows) == 3 and rows[0].values == (1.0, 0.0)
        assert rows[1].values[1] > 0
        assert all(len(batch) <= 8 for batch in model.calls)
        assert "".join(text for batch in model.calls for text in batch) == "".join(texts)
        assert all(token_count(text) <= 512 for batch in model.calls for text in batch)
        assert all(math.isclose(sum(v * v for v in row.values), 1.0) for row in rows)
    finally:
        assert await provider.aclose()


async def test_window_batch_size_two_bounds_one_long_input(monkeypatch):
    model = FakeModel()
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1",
        window_batch_size=2,
    )
    try:
        await provider.embed(texts=("法" * 1800,), dimension=2, timeout_seconds=5.0)
        assert tuple(map(len, model.calls)) == (2, 2)
        assert "".join(text for batch in model.calls for text in batch) == "法" * 1800
    finally:
        await provider.aclose()


async def test_default_window_batch_size_remains_eight(monkeypatch):
    model = FakeModel()
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1",
    )
    try:
        await provider.embed(texts=("法" * 4500,), dimension=2, timeout_seconds=5.0)
        assert WINDOW_BATCH_SIZE == 8
        assert tuple(map(len, model.calls)) == (8, 1)
    finally:
        await provider.aclose()


async def test_window_batch_sizes_preserve_order_coverage_and_result(monkeypatch):
    texts = ("法" * 510 + "尾", "短", "法" * 1300 + "尾")
    outcomes = []
    call_orders = []
    for window_batch_size in (1, 2, 8):
        model = FakeModel()
        install(monkeypatch, model)
        provider = LocalSentenceTransformerEmbeddingProvider(
            model_name_or_path="weights#token-window-mean-v1",
            window_batch_size=window_batch_size,
        )
        try:
            outcomes.append(
                await provider.embed(texts=texts, dimension=2, timeout_seconds=5.0)
            )
            call_orders.append(tuple(text for batch in model.calls for text in batch))
        finally:
            await provider.aclose()

    assert call_orders[0] == call_orders[1] == call_orders[2]
    assert "".join(call_orders[0]) == "".join(texts)
    assert outcomes[0] == outcomes[1] == outcomes[2]


@pytest.mark.parametrize("value", [True, False, 0, -1, 9, 1.0, "2", None])
def test_window_batch_size_must_be_strict_integer_between_one_and_eight(value):
    with pytest.raises(ValueError, match="window_batch_size"):
        LocalSentenceTransformerEmbeddingProvider(
            model_name_or_path="weights#token-window-mean-v1",
            window_batch_size=value,
        )


async def test_window_pooling_is_equal_mean_of_unit_vectors(monkeypatch):
    model = FakeModel()
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1"
    )
    try:
        (row,) = await provider.embed(texts=("法" * 510 + "尾",), dimension=2, timeout_seconds=5.0)
        x, y = 1 + 1 / math.sqrt(2), 1 / math.sqrt(2)
        norm = math.sqrt(x * x + y * y)
        assert row.values == pytest.approx((x / norm, y / norm))
    finally:
        await provider.aclose()


async def test_late_window_failure_never_returns_partial_input(monkeypatch):
    model = FakeModel()
    original = model.encode

    def fail(texts):
        if model.calls:
            raise RuntimeError("late window")
        return original(texts)

    model.encode = fail
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1",
        window_batch_size=2,
    )
    try:
        with pytest.raises(ModelProviderUnavailable):
            await provider.embed(texts=("法" * 6000,), dimension=2, timeout_seconds=5.0)
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "attribute,value", [("max_seq_length", 1024), ("default_prompt_name", "query")]
)
async def test_profile_refuses_incompatible_model_configuration(monkeypatch, attribute, value):
    model = FakeModel()
    setattr(model, attribute, value)
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1"
    )
    try:
        with pytest.raises(ModelProviderUnavailable):
            await provider.embed(texts=("法",), dimension=2, timeout_seconds=5.0)
    finally:
        await provider.aclose()


@pytest.mark.parametrize("value", [True, -1, 0, 1.5])
def test_bad_token_count_is_rejected(value):
    with pytest.raises(ValueError):
        tuple(token_windows("x", lambda text: value))


def test_profile_rejects_raw_vectors_and_unknown_identity_before_load():
    with pytest.raises(ValueError):
        LocalSentenceTransformerEmbeddingProvider(
            model_name_or_path="weights#token-window-mean-v1",
            normalize_embeddings=False,
        )
    with pytest.raises(ValueError):
        LocalSentenceTransformerEmbeddingProvider(model_name_or_path="weights#future")


async def test_opposing_window_vectors_fail_instead_of_zero_pool(monkeypatch):
    model = FakeModel()
    model.encode = lambda texts: [[1.0, 0.0] if "尾" not in t else [-1.0, 0.0] for t in texts]
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1",
    )
    try:
        with pytest.raises(ModelProviderUnavailable):
            await provider.embed(texts=("法" * 510 + "尾",), dimension=2, timeout_seconds=5.0)
    finally:
        await provider.aclose()


async def test_actual_encode_preprocessing_must_match_nontruncating_count(monkeypatch):
    model = FakeModel()

    def altered(texts):
        return {
            "input_ids": [[9, 9] for text in texts],
            "attention_mask": [[1, 1] for text in texts],
        }

    model.tokenize = altered
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1",
    )
    try:
        with pytest.raises(ModelProviderUnavailable):
            await provider.embed(texts=("İ" * 510,), dimension=2, timeout_seconds=5.0)
        assert not model.calls
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "features",
    [
        {"input_ids": [], "attention_mask": []},
        {"input_ids": [[1, 1, 1]], "attention_mask": [[1, 1]]},
        {"input_ids": [[1.0, 1, 1]], "attention_mask": [[1, 1, 1]]},
        {"input_ids": [[1, 1, 1]], "attention_mask": [[True, 1, 1]]},
        {"input_ids": [[1, 1, 1]], "attention_mask": [[1, 2, 1]]},
    ],
)
async def test_invalid_preprocessing_shape_and_types_fail_closed(monkeypatch, features):
    model = FakeModel()
    model.tokenize = lambda texts: features
    install(monkeypatch, model)
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="weights#token-window-mean-v1",
    )
    try:
        with pytest.raises(ModelProviderUnavailable):
            await provider.embed(texts=("法",), dimension=2, timeout_seconds=5.0)
        assert not model.calls
    finally:
        await provider.aclose()
