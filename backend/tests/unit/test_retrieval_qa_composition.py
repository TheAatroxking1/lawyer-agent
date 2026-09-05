from __future__ import annotations

import types

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
