from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config
from test_legal_corpus_chunks_repositories import _create_database, _drop_database

from alembic import command
from lawyer_agent.application.evidence import Citation, CitationGate
from lawyer_agent.application.legal_evidence_assembly import LegalEvidenceAssemblyService
from lawyer_agent.application.legal_index_chunks import select_index_leaves
from lawyer_agent.cli import corpus_publish
from lawyer_agent.config import Settings
from lawyer_agent.domain.legal_corpus import LegalChunk, LegalVersionStatus
from lawyer_agent.domain.legal_search import LegalSearchHit
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalInstrumentModel,
    LegalVersionModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
    SqlAlchemyLegalCorpusRepository,
)
from lawyer_agent.infrastructure.providers.embedding import (
    LocalSentenceTransformerEmbeddingProvider,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_LONG_TEXT = "第一条 " + "合成条文的第一款，用于验证原文恢复。" * 25
_SECOND_PARAGRAPH = "合成条文的第二款，应在检索子块后完整保留。" * 25
_SHORT_TEXT = "第二条 合成的短条全文。"


@pytest.fixture
def cli_mysql_url(mysql_url: URL) -> Iterator[URL]:
    # Keep multi-child rows away from migration tests that intentionally downgrade.
    database_name = f"lawyer_test_{uuid4().hex}"
    if re.fullmatch(r"lawyer_test_[a-f0-9]{32}", database_name) is None:
        raise RuntimeError("refusing to manage an unexpected database name")
    asyncio.run(_create_database(mysql_url, database_name))
    try:
        yield mysql_url.set(database=database_name)
    finally:
        asyncio.run(_drop_database(mysql_url, database_name))


async def _assert_readback(
    mysql_url: URL, *, old_chunks: tuple[LegalChunk, ...] = (),
) -> tuple[LegalChunk, ...]:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            versions = (await session.scalars(
                select(LegalVersionModel).join(LegalInstrumentModel).where(
                    LegalInstrumentModel.title == "层级接线合成法规"
                )
            )).all()
            assert len(versions) == 1
            version = versions[0]
            assert version.status == LegalVersionStatus.STATUS_UNKNOWN.value
            assert version.published_on is version.effective_on is None
            corpus = SqlAlchemyLegalCorpusRepository(session)
            provisions = await corpus.provisions_for_version(version.id)
            assert [p.full_text for p in provisions] == [
                _LONG_TEXT + _SECOND_PARAGRAPH, _SHORT_TEXT,
            ]
            chunks = await SqlAlchemyLegalCorpusChunkRepository(session).chunks_for_version(
                version.id
            )
            assert len(chunks) > len(provisions)
            assert all(c.parser_version == "corpus-docx-v2/hierarchical-v2" for c in chunks)
            short = [c for c in chunks if c.provision_id == provisions[1].id]
            assert len(short) == 1 and short[0].parent_chunk_id is None
            assert short[0].content == _SHORT_TEXT
            children: tuple[LegalChunk, ...] = tuple(
                c for c in select_index_leaves(chunks) if c.parent_chunk_id is not None
            )
            assert len(children) >= 2
            hits = tuple(LegalSearchHit(
                chunk_id=c.id, version_id=c.version_id, provision_id=c.provision_id, score=1.0,
            ) for c in children)
            if old_chunks:
                assert chunks == old_chunks
            bundle = await LegalEvidenceAssemblyService(corpus).assemble(hits=hits)
            assert bundle is not None and len(bundle.items) == 1
            assert bundle.items[0].provision_text == _LONG_TEXT + _SECOND_PARAGRAPH
            assert bundle.items[0].evidence_id == provisions[0].id
            verdict, = CitationGate().verify(
                bundle, (Citation(0, provisions[0].id),), target_date=date(2026, 1, 1),
            )
            assert not verdict.allowed and verdict.reason == "effective_status_unknown"
            return chunks
    finally:
        await engine.dispose()


def test_cli_rebuilds_hierarchical_chunks_and_restores_old_hits(
    cli_mysql_url: URL, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    mysql_url = cli_mysql_url
    command.upgrade(_alembic_config(mysql_url), "head")
    path = tmp_path / "合成长短条文.docx"
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>" + "".join(
            f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>"
            for p in (_LONG_TEXT, _SECOND_PARAGRAPH, _SHORT_TEXT)
        ) + "</w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    settings = Settings(
        environment="test", database_url=mysql_url.render_as_string(hide_password=False)
    )
    monkeypatch.setattr(corpus_publish, "Settings", lambda: settings)

    def forbid_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("import-only must not instantiate a model provider")

    monkeypatch.setattr(LocalSentenceTransformerEmbeddingProvider, "__init__", forbid_model)
    args = corpus_publish.build_parser().parse_args([
        "--docx", str(path), "--instrument-title", "层级接线合成法规",
        "--issuing-authority", "示例机关", "--parser-version", "corpus-docx-v2",
        "--import-only",
    ])
    asyncio.run(corpus_publish._run(args))
    chunks = asyncio.run(_assert_readback(mysql_url))
    asyncio.run(corpus_publish._run(args))
    asyncio.run(_assert_readback(mysql_url, old_chunks=chunks))
