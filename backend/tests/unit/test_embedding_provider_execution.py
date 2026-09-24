import asyncio
import sys
import threading
from types import SimpleNamespace

import pytest

from lawyer_agent.application.model_gateway import (
    ModelProviderBusy,
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from lawyer_agent.infrastructure.providers.embedding import (
    LocalSentenceTransformerEmbeddingProvider,
)


async def test_load_encode_and_vector_conversion_all_run_outside_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    calls = []

    class Value:
        def __float__(self):
            calls.append(("convert", threading.get_ident()))
            return 1.0

    def encode(texts):
        calls.append(("encode", threading.get_ident()))
        return [[Value(), Value()] for _ in texts]

    def model(*args, **kwargs):
        calls.append(("load", threading.get_ident()))
        return SimpleNamespace(encode=encode)

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=model),
    )
    provider = LocalSentenceTransformerEmbeddingProvider(model_name_or_path="synthetic")
    result = await provider.embed(texts=["x"], dimension=2, timeout_seconds=1)
    assert {name for name, _ in calls} == {"load", "encode", "convert"}
    assert all(thread_id != loop_thread for _, thread_id in calls)
    assert len(result) == 1
    assert await provider.aclose()


async def test_provider_snapshots_input_before_background_encoding():
    texts = ["original"]
    seen = []
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def encode(items):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(1)
        seen.append(items)
        return [[3.0, 4.0] for _ in items]

    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="synthetic", encode=encode,
    )
    task = asyncio.create_task(provider.embed(texts=texts, dimension=2, timeout_seconds=2))
    try:
        await asyncio.wait_for(started.wait(), 2)
        texts[0] = "mutated"
    finally:
        release.set()
    assert len(await task) == 1
    assert seen == [("original",)]
    assert await provider.aclose()


@pytest.mark.parametrize("texts", [None, "abc", b"abc", [], [None], [""]])
async def test_invalid_inputs_never_reach_encoder(texts):
    calls = []
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="synthetic", encode=lambda items: calls.append(items) or [[1.0]],
    )
    with pytest.raises(ModelProviderUnavailable):
        await provider.embed(texts=texts, dimension=1, timeout_seconds=1)
    assert calls == []


@pytest.mark.parametrize("budget", [0, -1, True, float("nan"), float("inf"), "1"])
async def test_invalid_timeout_never_starts_encoding(budget):
    calls = []
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="synthetic", encode=lambda items: calls.append(items) or [[1.0]],
    )
    with pytest.raises(ValueError):
        await provider.embed(texts=["x"], dimension=1, timeout_seconds=budget)
    assert calls == []


@pytest.mark.parametrize("ending", ["timeout", "cancel"])
async def test_provider_request_end_keeps_busy_until_real_work_finishes(ending):
    started, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def encode(items):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2)
        return [[3.0, 4.0]]

    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="synthetic", encode=encode,
    )
    task = asyncio.create_task(provider.embed(
        texts=["x"], dimension=2, timeout_seconds=0.05 if ending == "timeout" else 1,
    ))
    try:
        await asyncio.wait_for(started.wait(), 1)
        if ending == "cancel":
            task.cancel()
        with pytest.raises(ModelProviderTimeout if ending == "timeout" else asyncio.CancelledError):
            await task
        with pytest.raises(ModelProviderBusy):
            await provider.embed(texts=["y"], dimension=2, timeout_seconds=1)
        assert not await provider.aclose(timeout_seconds=0)
    finally:
        release.set()
    assert await provider.aclose(timeout_seconds=1)
    with pytest.raises(ModelProviderUnavailable, match="closed"):
        await provider.embed(texts=["z"], dimension=2, timeout_seconds=1)


async def test_malformed_vendor_values_have_stable_error_without_payload():
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path="synthetic", encode=lambda texts: [["private-provider-payload"]],
    )
    with pytest.raises(ModelProviderUnavailable) as captured:
        await provider.embed(texts=["x"], dimension=1, timeout_seconds=1)
    assert "private-provider-payload" not in str(captured.value)
    assert await provider.aclose()
