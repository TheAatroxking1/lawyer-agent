"""Exercise batch report -> quality check -> reviewed durable release on real stores."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config
from test_legal_corpus_chunks_repositories import _create_database, _drop_database

from alembic import command
from lawyer_agent.cli import corpus_import_batch, corpus_publish, corpus_publish_set
from lawyer_agent.config import Settings
from lawyer_agent.domain.legal_corpus import DatasetState
from lawyer_agent.domain.legal_navigation import navigation_index_name
from lawyer_agent.infrastructure.persistence.models.legal_corpus import LegalVersionModel
from lawyer_agent.infrastructure.persistence.models.legal_dataset_publication import (
    LegalDatasetPublicationModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)
from lawyer_agent.infrastructure.providers.embedding import (
    LocalSentenceTransformerEmbeddingProvider,
)
from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


@pytest.fixture
def reviewed_mysql_url(mysql_url: URL) -> Iterator[URL]:
    if not os.getenv("LAWYER_TEST_PUBLICATION_OPENSEARCH_URL"):
        pytest.skip("explicit isolated publication OpenSearch URL is required")
    name = f"lawyer_test_{uuid4().hex}"
    asyncio.run(_create_database(mysql_url, name))
    url = mysql_url.set(database=name)
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        asyncio.run(_drop_database(mysql_url, name))


def _batch(tmp_path: Path, *, article_count: int = 1) -> Path:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    rows = []
    for number in range(2):
        source = source_root / f"合成法规{number}.docx"
        paragraphs = "".join(
            f"<w:p><w:r><w:t>第{article}章 合成章节</w:t></w:r></w:p>"
            f"<w:p><w:r><w:t>第{article}条 合成法规{number}的共同义务。</w:t></w:r></w:p>"
            for article in range(1, article_count + 1)
        )
        with ZipFile(source, "w") as archive:
            archive.writestr("word/document.xml", (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{paragraphs}</w:body></w:document>"
            ))
        rows.append({
            "schema_version": "legal-corpus-import-v1", "review_status": "reviewed",
            "metadata_review_ref": "synthetic-metadata-review", "source_path": str(source),
            "source_sha256": sha256(source.read_bytes()).hexdigest(),
            "title": source.stem, "issuing_authority": "示例机关", "jurisdiction": "national",
            "category": "law", "published_on": "2020-01-01", "effective_on": "2020-02-01",
            "status": "current",
        })
    manifest = tmp_path / "reviewed.jsonl"
    manifest.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    report = tmp_path / "import.jsonl"
    assert corpus_import_batch.main([
        "--manifest", str(manifest), "--source-root", str(source_root), "--report", str(report),
    ]) == 0
    return report


async def _state(url: URL, alias: str):
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            count = await session.scalar(select(func.count()).select_from(
                LegalDatasetPublicationModel,
            ))
            snapshot = await SqlAlchemyLegalCorpusInventoryRepository(session).find_dataset(alias)
            return count, snapshot
    finally:
        await engine.dispose()


def test_set_cli_review_binding_and_metadata_gate(
    reviewed_mysql_url: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    import lawyer_agent.config as config_module
    import lawyer_agent.infrastructure.providers.embedding as embedding_module

    base_url = os.environ["LAWYER_TEST_PUBLICATION_OPENSEARCH_URL"]
    settings = Settings(
        environment="test", database_url=reviewed_mysql_url.render_as_string(hide_password=False),
        opensearch_url=base_url, embedding_model_ref="synthetic-release", embedding_dimension=4,
    )
    monkeypatch.setattr(config_module, "Settings", lambda: settings)
    monkeypatch.setattr(corpus_publish_set, "Settings", lambda: settings)
    model_calls = []

    def synthetic_provider(**kwargs):
        assert kwargs["normalize_embeddings"] is True
        model_calls.append("provider-created")
        return LocalSentenceTransformerEmbeddingProvider(
            model_name_or_path="synthetic-release",
            encode=lambda texts: [(1.0,) * 4 for _ in texts],
            normalize_embeddings=True,
        )

    monkeypatch.setattr(embedding_module, "LocalSentenceTransformerEmbeddingProvider",
                        synthetic_provider)
    report_path = _batch(tmp_path, article_count=260)
    capsys.readouterr()
    token = uuid4().hex
    index, alias = f"lawyer_review_test_{token}", f"lawyer_review_alias_{token}"
    search = OpenSearchRestClient(base_url=base_url)
    common = ["--import-report", str(report_path), "--alias", alias,
              "--index-name", index, "--model-ref", "synthetic-release", "--dimension", "4",
              "--batch-size", "128"]
    try:
        assert corpus_publish_set.main([*common, "--check"]) == 0
        quality = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert quality["passed"] is True
        digest = quality["quality_sha256"]
        assert len(digest) == 64
        assert model_calls == []
        assert not asyncio.run(search.index_exists(index))
        assert asyncio.run(_state(reviewed_mysql_url, alias)) == (0, None)

        approval = ["--review-ref", "synthetic-professional-review",
                    "--expected-quality-sha256", "0" * 64]
        assert corpus_publish_set.main([*common, *approval]) != 0
        capsys.readouterr()
        assert model_calls == []
        assert not asyncio.run(search.index_exists(index))
        assert asyncio.run(_state(reviewed_mysql_url, alias)) == (0, None)

        async def change_status(status):
            engine = create_async_engine(reviewed_mysql_url)
            try:
                async with engine.begin() as connection:
                    await connection.execute(update(LegalVersionModel).values(status=status))
            finally:
                await engine.dispose()

        asyncio.run(change_status("status_unknown"))
        assert corpus_publish_set.main([*common, "--check"]) != 0
        capsys.readouterr()
        assert model_calls == []
        assert asyncio.run(_state(reviewed_mysql_url, alias)) == (0, None)
        asyncio.run(change_status("current"))

        assert corpus_publish_set.main([
            *common, "--review-ref", "synthetic-professional-review",
            "--expected-quality-sha256", digest,
        ]) == 0
        assert "publication_id" in capsys.readouterr().out
        assert model_calls == ["provider-created"]
        assert asyncio.run(search.resolve_alias(alias)) == index
        count, snapshot = asyncio.run(_state(reviewed_mysql_url, alias))
        assert count == 1 and snapshot is not None and snapshot.state is DatasetState.PUBLISHED
        assert snapshot.manifest["quality_sha256"] == digest
        assert snapshot.manifest["review_ref"] == "synthetic-professional-review"
        assert snapshot.manifest["normalization"] == "l2"
        assert snapshot.manifest["version_count"] == 2
        assert snapshot.manifest["indexed_documents"] == 520
        assert snapshot.manifest["navigation_documents"] == 522
        hits = asyncio.run(search.search_bm25(alias, query="共同义务", limit=600))
        assert len(hits) == 520
        assert len({hit.version_id for hit in hits}) == 2
    finally:
        asyncio.run(search.delete_index(navigation_index_name(index)))
        asyncio.run(search.delete_index(index))


def test_single_file_review_survives_chunk_uuid_replay(
    reviewed_mysql_url: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys,
) -> None:
    import lawyer_agent.infrastructure.providers.embedding as embedding_module

    base_url = os.environ["LAWYER_TEST_PUBLICATION_OPENSEARCH_URL"]
    settings = Settings(
        environment="test", database_url=reviewed_mysql_url.render_as_string(hide_password=False),
        opensearch_url=base_url, embedding_model_ref="synthetic-release", embedding_dimension=4,
    )
    monkeypatch.setattr(corpus_publish, "Settings", lambda: settings)
    model_calls = []

    def synthetic_provider(**kwargs):
        assert kwargs["normalize_embeddings"] is True
        model_calls.append("provider-created")
        return LocalSentenceTransformerEmbeddingProvider(
            model_name_or_path="synthetic-release",
            encode=lambda texts: [(1.0,) * 4 for _ in texts], normalize_embeddings=True,
        )

    monkeypatch.setattr(embedding_module, "LocalSentenceTransformerEmbeddingProvider",
                        synthetic_provider)
    source = tmp_path / "合成单文件.docx"
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>第一条 合成单文件的共同义务。</w:t>"
            "</w:r></w:p></w:body></w:document>"
        ))
    token = uuid4().hex
    index, alias = f"lawyer_single_test_{token}", f"lawyer_single_alias_{token}"
    search = OpenSearchRestClient(base_url=base_url)
    common = [
        "--source", str(source), "--instrument-title", "合成单文件",
        "--issuing-authority", "示例机关",
        "--category", "law", "--published-on", "2020-01-01", "--effective-on", "2020-02-01",
        "--status", "current", "--metadata-review-ref", "synthetic-metadata-review",
        "--alias", alias, "--index-name", index, "--model-ref", "synthetic-release",
        "--dimension", "4",
    ]
    try:
        assert corpus_publish.main([*common, "--quality-check"]) == 0
        quality = json.loads(capsys.readouterr().out.splitlines()[-1])
        assert quality["passed"] is True
        digest = quality["quality_sha256"]
        assert model_calls == []
        assert not asyncio.run(search.index_exists(index))
        assert asyncio.run(_state(reviewed_mysql_url, alias)) == (0, None)
        assert corpus_publish.main([
            *common, "--review-ref", "synthetic-professional-review",
            "--expected-quality-sha256", digest,
        ]) == 0
        assert model_calls == ["provider-created"]
        assert asyncio.run(search.resolve_alias(alias)) == index
        count, snapshot = asyncio.run(_state(reviewed_mysql_url, alias))
        assert count == 1 and snapshot is not None
        assert snapshot.manifest["quality_sha256"] == digest
        assert len(asyncio.run(search.search_bm25(alias, query="共同义务", limit=10))) == 1
    finally:
        asyncio.run(search.delete_index(navigation_index_name(index)))
        asyncio.run(search.delete_index(index))
