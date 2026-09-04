from __future__ import annotations

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


async def test_provider_embed_returns_one_vector_per_text() -> None:
    provider = _provider(_fake_encoder())
    vectors = await provider.embed(
        texts=("第一条 内容。", "第二条 内容更长。"), dimension=2, timeout_seconds=5.0
    )
    assert len(vectors) == 2
    assert [vector.values for vector in vectors] == [(7.0, 1.0), (9.0, 1.0)]


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
