from __future__ import annotations

import hashlib
import math
import os
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from lawyer_agent.application.model_gateway import ModelProviderUnavailable
from lawyer_agent.domain.model_gateway import EmbeddingVector
from lawyer_agent.infrastructure.providers.embedding_cache import LocalEmbeddingCacheProvider


class Provider:
    def __init__(self):
        self.calls = []
        self.closed = False

    async def embed(self, *, texts, dimension, timeout_seconds):
        self.calls.append(tuple(texts))
        return tuple(EmbeddingVector((0.6, 0.8), 2) for _ in texts)

    async def aclose(self, *, timeout_seconds=5.0):
        self.closed = True
        return True


def configuration(tmp_path):
    model = tmp_path / "snapshots" / ("a" * 40)
    model.mkdir(parents=True, exist_ok=True)
    return {
        "cache_directory": tmp_path / "cache",
        "model_ref": str(model) + "#token-window-mean-v1",
    }


async def test_restart_hits_are_exact_and_do_not_load_model_or_contain_text(tmp_path):
    config = configuration(tmp_path)
    first = Provider()
    cache = LocalEmbeddingCacheProvider(first, **config)
    assert not config["cache_directory"].exists()
    texts = ("正文不得写缓存", "another")
    rows = await cache.embed(texts=texts, dimension=2, timeout_seconds=5.0)
    assert len(first.calls) == 1
    assert await cache.aclose() and first.closed
    second = Provider()
    recovered = LocalEmbeddingCacheProvider(second, **config)
    assert await recovered.embed(texts=texts, dimension=2, timeout_seconds=5.0) == rows
    assert second.calls == []
    assert recovered.stats()["cache_hit_rows"] == 2
    assert recovered.stats()["provider_call_attempts"] == 0
    files = list(config["cache_directory"].rglob("*.emb"))
    assert len(files) == 2
    assert all(texts[0].encode() not in file.read_bytes() for file in files)


async def test_duplicate_misses_are_encoded_once_and_order_preserved(tmp_path):
    provider = Provider()
    cache = LocalEmbeddingCacheProvider(provider, **configuration(tmp_path))
    rows = await cache.embed(texts=("a", "b", "a"), dimension=2, timeout_seconds=5.0)
    assert len(rows) == 3 and provider.calls == [("a", "b")]
    assert cache.stats()["cache_miss_rows"] == 3
    assert cache.stats()["provider_submitted_rows"] == 2


@pytest.mark.parametrize("damage", ["truncated", "checksum", "nonfinite", "nonunit", "wrong_key"])
async def test_corrupt_entries_recompute_and_never_return_invalid_vectors(tmp_path, damage):
    config = configuration(tmp_path)
    cache = LocalEmbeddingCacheProvider(Provider(), **config)
    await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    (path,) = config["cache_directory"].rglob("*.emb")
    data = bytearray(path.read_bytes())
    if damage == "truncated":
        data = data[:-1]
    elif damage == "checksum":
        data[-1] ^= 1
    else:
        if damage == "wrong_key":
            data[8] ^= 1
        else:
            data[40:48] = struct.pack("<d", math.nan if damage == "nonfinite" else 100.0)
        data[-32:] = hashlib.sha256(data[:-32]).digest()
    path.write_bytes(data)
    provider = Provider()
    recovered = LocalEmbeddingCacheProvider(provider, **config)
    rows = await recovered.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    assert rows[0].values == (0.6, 0.8) and len(provider.calls) == 1
    assert recovered.stats()["corrupt_entries"] == 1


async def test_invalid_new_vectors_are_not_cached(tmp_path):
    provider = Provider()

    async def invalid(**kwargs):
        return (EmbeddingVector((2.0, 3.0), 2),)

    provider.embed = invalid
    config = configuration(tmp_path)
    cache = LocalEmbeddingCacheProvider(provider, **config)
    with pytest.raises(ModelProviderUnavailable):
        await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    assert not list(config["cache_directory"].rglob("*.emb"))


async def test_failed_atomic_replace_cleans_temp_and_returns_real_vectors(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    cache = LocalEmbeddingCacheProvider(Provider(), **config)

    def fail(*args):
        raise OSError("synthetic disk failure")

    monkeypatch.setattr(os, "replace", fail)
    rows = await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    assert rows[0].values == (0.6, 0.8)
    assert not list(config["cache_directory"].rglob("*.tmp"))
    assert cache.stats()["write_failures"] == 1


def test_cache_rejects_mutable_remote_and_relative_directory(tmp_path):
    config = configuration(tmp_path)
    with pytest.raises(ValueError):
        LocalEmbeddingCacheProvider(
            Provider(),
            cache_directory=tmp_path / "cache",
            model_ref="org/model#token-window-mean-v1",
        )
    with pytest.raises(ValueError):
        LocalEmbeddingCacheProvider(
            Provider(), cache_directory=Path("relative"), model_ref=config["model_ref"]
        )


def test_cache_rejects_windows_reparse_directory(tmp_path, monkeypatch):
    config = configuration(tmp_path)
    config["cache_directory"].mkdir()
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if os.fspath(path) == str(config["cache_directory"]):
            return SimpleNamespace(st_mode=value.st_mode, st_file_attributes=1024)
        return value

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(ValueError):
        LocalEmbeddingCacheProvider(Provider(), **config)


async def test_closed_wrapper_rejects_even_cached_hits(tmp_path):
    cache = LocalEmbeddingCacheProvider(Provider(), **configuration(tmp_path))
    await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    await cache.aclose()
    with pytest.raises(ModelProviderUnavailable):
        await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)


async def test_exact_text_dimension_and_snapshot_separate_cache_entries(tmp_path):
    config = configuration(tmp_path)
    provider = Provider()

    async def encode(*, texts, dimension, timeout_seconds):
        provider.calls.append(tuple(texts))
        return tuple(EmbeddingVector((1.0,) + (0.0,) * (dimension - 1), dimension) for _ in texts)

    provider.embed = encode
    cache = LocalEmbeddingCacheProvider(provider, **config)
    await cache.embed(texts=("é", "e\u0301"), dimension=2, timeout_seconds=5.0)
    await cache.embed(texts=("é",), dimension=3, timeout_seconds=5.0)
    other = tmp_path / "snapshots" / ("b" * 40)
    other.mkdir()
    changed = LocalEmbeddingCacheProvider(
        provider,
        cache_directory=config["cache_directory"],
        model_ref=str(other) + "#token-window-mean-v1",
    )
    await changed.embed(texts=("é",), dimension=2, timeout_seconds=5.0)
    assert provider.calls == [("é", "e\u0301"), ("é",), ("é",)]
    assert len(list(config["cache_directory"].rglob("*.emb"))) == 4


async def test_cancelled_call_does_not_write_and_close_is_forwarded(tmp_path):
    import asyncio

    provider = Provider()

    async def cancelled(**kwargs):
        raise asyncio.CancelledError

    provider.embed = cancelled
    config = configuration(tmp_path)
    cache = LocalEmbeddingCacheProvider(provider, **config)
    with pytest.raises(asyncio.CancelledError):
        await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    assert not config["cache_directory"].exists()
    assert await cache.aclose() and provider.closed


async def test_partial_hits_preserve_input_order(tmp_path):
    config = configuration(tmp_path)
    provider = Provider()

    async def encode(*, texts, **kwargs):
        provider.calls.append(tuple(texts))
        return tuple(EmbeddingVector((1.0, 0.0) if t == "a" else (0.0, 1.0), 2) for t in texts)

    provider.embed = encode
    cache = LocalEmbeddingCacheProvider(provider, **config)
    await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    rows = await cache.embed(texts=("b", "a", "b"), dimension=2, timeout_seconds=5.0)
    assert tuple(row.values for row in rows) == ((0.0, 1.0), (1.0, 0.0), (0.0, 1.0))
    assert provider.calls == [("a",), ("b",)]


async def test_busy_rejection_is_only_an_attempt_not_completed_model_work(tmp_path):
    from lawyer_agent.application.model_gateway import ModelProviderBusy

    provider = Provider()

    async def busy(**kwargs):
        raise ModelProviderBusy("busy")

    provider.embed = busy
    cache = LocalEmbeddingCacheProvider(provider, **configuration(tmp_path))
    with pytest.raises(ModelProviderBusy):
        await cache.embed(texts=("a",), dimension=2, timeout_seconds=5.0)
    assert cache.stats()["provider_call_attempts"] == 1
    assert cache.stats()["provider_completed_calls"] == 0
    assert cache.stats()["provider_completed_rows"] == 0


async def test_boolean_provider_dimension_is_not_a_valid_cache_value(tmp_path):
    provider = Provider()

    async def invalid(**kwargs):
        return (EmbeddingVector((1.0,), True),)

    provider.embed = invalid
    cache = LocalEmbeddingCacheProvider(provider, **configuration(tmp_path))
    with pytest.raises(ModelProviderUnavailable):
        await cache.embed(texts=("a",), dimension=1, timeout_seconds=5.0)
