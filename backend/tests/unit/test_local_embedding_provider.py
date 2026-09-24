from __future__ import annotations

import math
from collections.abc import Sequence

import pytest

from lawyer_agent.application.model_gateway import ModelProviderUnavailable
from lawyer_agent.infrastructure.providers.embedding import (
    LocalSentenceTransformerEmbeddingProvider,
)


def _fake_encoder() -> object:
    def encode(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [
            [float(len(text)), float(text.count("条"))]
            for text in texts
        ]

    return encode


def _provider(fake: object) -> LocalSentenceTransformerEmbeddingProvider:
    return LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="fake-model", encode=fake  # type: ignore[arg-type]
    )


def _unit(row: tuple[float, ...]) -> tuple[float, ...]:
    norm = math.sqrt(sum(value * value for value in row))
    return tuple(value / norm for value in row)


async def test_provider_embed_returns_one_normalized_vector_per_text() -> None:
    provider = _provider(_fake_encoder())
    vectors = await provider.embed(
        texts=("第一条 内容。", "第二条 内容更长。"), dimension=2, timeout_seconds=5.0
    )
    assert len(vectors) == 2
    assert vectors[0].values == pytest.approx(_unit((7.0, 1.0)))
    assert vectors[1].values == pytest.approx(_unit((9.0, 1.0)))


async def test_provider_can_disable_normalization_for_raw_vectors() -> None:
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="fake-model",
        encode=_fake_encoder(),  # type: ignore[arg-type]
        normalize_embeddings=False,
    )
    vectors = await provider.embed(
        texts=("第一条 内容。",), dimension=2, timeout_seconds=5.0
    )
    assert vectors[0].values == (7.0, 1.0)


async def test_l2_profile_rejects_zero_vector_instead_of_claiming_normalization() -> None:
    provider = _provider(lambda texts: [(0.0, 0.0)])
    with pytest.raises(ModelProviderUnavailable):
        await provider.embed(texts=("x",), dimension=2, timeout_seconds=1.0)


@pytest.mark.parametrize("scale", [1e308, 1e-308])
async def test_l2_profile_handles_extreme_finite_values_without_zero_collapse(scale) -> None:
    provider = _provider(lambda texts: [(scale, scale)])
    vectors = await provider.embed(texts=("x",), dimension=2, timeout_seconds=1.0)
    assert vectors[0].values == pytest.approx((math.sqrt(0.5),) * 2)


async def test_provider_embed_rejects_empty_texts() -> None:
    provider = _provider(_fake_encoder())
    with pytest.raises(ModelProviderUnavailable):
        await provider.embed(texts=(), dimension=2, timeout_seconds=5.0)


async def test_provider_surfaces_encode_failure_without_fabrication() -> None:
    def exploding(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        raise RuntimeError("gpu oom")

    provider = _provider(exploding)
    with pytest.raises(ModelProviderUnavailable):
        await provider.embed(texts=("x",), dimension=2, timeout_seconds=5.0)


async def test_provider_rejects_row_count_mismatch() -> None:
    def wrong_rows(texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return [[1.0, 2.0]]

    provider = _provider(wrong_rows)
    with pytest.raises(ModelProviderUnavailable):
        await provider.embed(texts=("a", "b"), dimension=2, timeout_seconds=5.0)


async def test_provider_rerank_and_chat_are_explicitly_unavailable() -> None:
    provider = _provider(_fake_encoder())
    with pytest.raises(ModelProviderUnavailable):
        await provider.rerank(query="q", documents=("d",), timeout_seconds=1.0)
    with pytest.raises(ModelProviderUnavailable):
        await provider.chat(messages=(), timeout_seconds=1.0)


def test_provider_rejects_blank_model_name() -> None:
    with pytest.raises(ValueError):
        LocalSentenceTransformerEmbeddingProvider(model_name_or_path="   ")


def test_provider_missing_dependency_maps_to_stable_error(monkeypatch) -> None:
    import sys

    # importlib.import_module returns sys.modules entries verbatim; None makes
    # the lazy loader fail fast as if the dependency were absent.
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    provider = LocalSentenceTransformerEmbeddingProvider(model_name_or_path="fake")
    with pytest.raises(ModelProviderUnavailable, match="not installed"):
        import asyncio

        async def run() -> None:
            await provider.embed(texts=("x",), dimension=2, timeout_seconds=1.0)

        asyncio.run(run())


@pytest.mark.parametrize("options,device,local_only", [
    ({}, "cpu", True), ({"device": "cuda:1", "local_files_only": False}, "cuda:1", False),
])
async def test_lazy_model_load_explicitly_controls_device_offline_and_remote_code(
    monkeypatch, options, device, local_only,
):
    import sys
    from types import SimpleNamespace

    calls = []
    def model(model_ref, **kwargs):
        calls.append((model_ref, kwargs))
        return SimpleNamespace(encode=lambda texts: [[3.0, 4.0] for _ in texts])
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=model),
    )
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="synthetic-model", **options,
    )
    assert calls == []
    result = await provider.embed(texts=("synthetic",), dimension=2, timeout_seconds=1.0)
    await provider.embed(texts=("synthetic",), dimension=2, timeout_seconds=1.0)
    assert calls == [("synthetic-model", {
        "device": device, "local_files_only": local_only, "trust_remote_code": False,
    })]
    assert result[0].values == pytest.approx((0.6, 0.8))


@pytest.mark.parametrize("options", [
    {"device": ""}, {"device": "auto"}, {"device": "CUDA"}, {"device": "cuda:-1"},
    {"device": "cuda:"}, {"device": "cpu:0"}, {"device": "cpu\n"}, {"device": 0},
    {"local_files_only": "true"}, {"local_files_only": 1}, {"local_files_only": None},
])
def test_provider_rejects_invalid_runtime_options(options):
    with pytest.raises(ValueError):
        LocalSentenceTransformerEmbeddingProvider(model_name_or_path="fake", **options)


async def test_missing_local_weights_returns_unavailable_without_remote_retry(monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = []
    def missing(model_ref, **kwargs):
        calls.append(kwargs)
        raise OSError("synthetic private cache path")
    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=missing),
    )
    provider = LocalSentenceTransformerEmbeddingProvider(model_name_or_path="missing-local")
    with pytest.raises(ModelProviderUnavailable, match="could not be loaded") as captured:
        await provider.embed(texts=("synthetic",), dimension=2, timeout_seconds=1.0)
    assert "private cache" not in str(captured.value)
    assert len(calls) == 1 and calls[0]["local_files_only"] is True
