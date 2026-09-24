from __future__ import annotations

import types
from datetime import date
from unittest.mock import AsyncMock

import pytest

from lawyer_agent.api.dependencies import (
    _build_legal_retrieval_qa_http_service,
)


def _settings(**overrides: object) -> object:
    values: dict[str, object] = {
        "deepseek_api_key": None,
        "opensearch_url": "http://127.0.0.1:9200",
    }
    values.update(overrides)
    return types.SimpleNamespace(**values)


def _session_factory() -> object:
    raise AssertionError("construction must not open a database session")


def test_builder_returns_none_without_deepseek_key() -> None:
    service = _build_legal_retrieval_qa_http_service(
        _settings(deepseek_api_key=None),
        _session_factory,
    )
    assert service is None


async def test_builder_registers_owned_provider_cleanup(monkeypatch):
    from lawyer_agent.infrastructure.providers import embedding

    close = AsyncMock(return_value=True)
    provider = types.SimpleNamespace(embed=AsyncMock(), aclose=close)
    monkeypatch.setattr(
        embedding, "LocalSentenceTransformerEmbeddingProvider", lambda **_: provider
    )
    cleanups = []
    assert (
        _build_legal_retrieval_qa_http_service(
            _settings(deepseek_api_key="synthetic"),
            _session_factory,
            cleanups=cleanups,
        )
        is not None
    )
    assert len(cleanups) == 1
    await cleanups[0]()
    close.assert_awaited_once_with(timeout_seconds=5.0)


@pytest.mark.parametrize("mode", ["normal", "error", "cancel", "startup", "close_cancel"])
async def test_lifespan_cleanup_keeps_db_and_redis_cleanup_on_embedding_failure(monkeypatch, mode):
    import asyncio

    from lawyer_agent.api import dependencies
    from lawyer_agent.config import Settings

    events = []

    async def close():
        events.append("embedding")
        if mode == "close_cancel":
            raise asyncio.CancelledError
        raise RuntimeError("private cleanup failure")

    async def redis_close():
        events.append("redis")

    async def dispose():
        events.append("engine")

    monkeypatch.setattr(
        dependencies, "create_engine", lambda _: types.SimpleNamespace(dispose=dispose)
    )
    monkeypatch.setattr(dependencies, "create_session_factory", lambda _: object())
    monkeypatch.setattr(
        dependencies.RedisAsyncioAdapter,
        "from_url",
        lambda *args, **kwargs: types.SimpleNamespace(aclose=redis_close),
    )

    def build(settings, factory, *, cleanups):
        cleanups.append(close)
        if mode == "startup":
            raise ValueError("original")
        return None

    monkeypatch.setattr(dependencies, "_build_legal_retrieval_qa_http_service", build)

    async def run():
        async with dependencies.application_services(Settings(environment="test")):
            if mode == "error":
                raise ValueError("original")
            if mode == "cancel":
                raise asyncio.CancelledError

    if mode in ("error", "startup", "cancel", "close_cancel"):
        with pytest.raises(asyncio.CancelledError if "cancel" in mode else ValueError):
            await run()
    else:
        await run()
    assert events == ["embedding", "redis", "engine"]


def test_builder_returns_none_without_opensearch_url() -> None:
    service = _build_legal_retrieval_qa_http_service(
        _settings(deepseek_api_key="sk-test", opensearch_url=""),
        _session_factory,
    )
    assert service is None


def test_builder_composes_full_chain_with_prerequisites() -> None:
    service = _build_legal_retrieval_qa_http_service(
        _settings(deepseek_api_key="sk-test"),
        _session_factory,
    )
    assert service is not None
    assert hasattr(service, "answer")
    assert hasattr(service, "_dataset_evidence")
    assert hasattr(service, "_chat")


@pytest.mark.parametrize(
    "options,expected",
    [
        ({}, {"device": "cpu", "local_files_only": True}),
        (
            {"embedding_device": "cuda:3", "embedding_local_files_only": False},
            {"device": "cuda:3", "local_files_only": False},
        ),
    ],
)
def test_http_embedding_composition_passes_runtime_settings(monkeypatch, options, expected):
    from lawyer_agent.infrastructure.providers import embedding

    original = embedding.LocalSentenceTransformerEmbeddingProvider
    captured = {}

    def provider(**kwargs):
        captured.update(kwargs)
        return original(**kwargs, encode=lambda texts: [(1.0, 0.0) for _ in texts])

    monkeypatch.setattr(embedding, "LocalSentenceTransformerEmbeddingProvider", provider)
    assert (
        _build_legal_retrieval_qa_http_service(
            _settings(deepseek_api_key="synthetic", **options),
            _session_factory,
        )
        is not None
    )
    assert {key: captured.get(key) for key in expected} == expected


async def test_built_service_navigates_before_one_embedding_without_opening_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lawyer_agent.domain.common import new_uuid7
    from lawyer_agent.domain.legal_navigation import NavigationSearchHit
    from lawyer_agent.domain.model_gateway import EmbeddingVector
    from lawyer_agent.infrastructure.providers.embedding import (
        LocalSentenceTransformerEmbeddingProvider,
    )
    from lawyer_agent.infrastructure.search.legal_navigation import OpenSearchNavigationClient
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    version_id = new_uuid7()
    events: list[str] = []

    async def resolve_alias(self, alias: str) -> str:
        events.append("alias")
        return "synthetic_main"

    async def schema(self, main_index_name: str) -> int:
        assert main_index_name == "synthetic_main"
        events.append("schema")
        return 1

    async def navigate(self, index_name: str, *, query: str, limit: int):
        events.append("navigation")
        return (NavigationSearchHit(version_id, "合成示例法", 2.0),)

    async def embed(self, *, texts, dimension: int, timeout_seconds: float):
        events.append("embedding")
        return (EmbeddingVector(values=(0.5, 0.5, 0.5, 0.5), dimension=4),)

    async def bm25(self, index_name: str, **kwargs):
        assert kwargs["version_ids"] == (version_id,)
        events.append("bm25")
        return ()

    async def knn(self, index_name: str, **kwargs):
        assert kwargs["version_ids"] == (version_id,)
        events.append("knn")
        return ()

    monkeypatch.setattr(OpenSearchRestClient, "resolve_alias", resolve_alias)
    monkeypatch.setattr(OpenSearchRestClient, "search_bm25", bm25)
    monkeypatch.setattr(OpenSearchRestClient, "search_knn", knn)
    monkeypatch.setattr(OpenSearchNavigationClient, "navigation_schema", schema)
    monkeypatch.setattr(OpenSearchNavigationClient, "search_navigation", navigate)
    monkeypatch.setattr(LocalSentenceTransformerEmbeddingProvider, "embed", embed)
    service = _build_legal_retrieval_qa_http_service(
        _settings(
            deepseek_api_key="sk-test",
            embedding_model_ref="synthetic",
            embedding_dimension=4,
        ),
        _session_factory,
    )
    assert service is not None
    answer = await service.answer(
        alias="dataset_v1",
        question="《合成示例法》的规则",
        target_date=date(2026, 1, 1),
    )
    assert answer.refused and answer.reason == "no_evidence"
    assert events == ["alias", "schema", "navigation", "embedding", "bm25", "knn"]
