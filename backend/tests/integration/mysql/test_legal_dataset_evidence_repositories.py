from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.evidence import EvidenceBundle
from lawyer_agent.application.legal_dataset_evidence import (
    LegalDatasetEvidenceService,
)
from lawyer_agent.application.legal_evidence_assembly import (
    LegalEvidenceAssemblyService,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import content_sha256
from lawyer_agent.domain.legal_search import LegalSearchHit
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_INSTRUMENT = UUID("01a06ae2-6000-7000-8000-0000000000c1")
_VERSION = UUID("01a06ae2-6200-7000-8000-0000000000c3")
_PROVISION = UUID("01a06ae2-6400-7000-8000-0000000000c5")
_FULL_TEXT = "第一条 承租人逾期支付租金应当支付违约金。"


async def _seed(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,version) "
                    "VALUES (:id,'示范法','机关','national',1)"
                ),
                {"id": _INSTRUMENT.bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_versions "
                    "(id,instrument_id,version_label,status,published_on,"
                    "effective_on,content_hash,source_ref,dataset_version,"
                    "parser_version) "
                    "VALUES (:id,:inst,'2024版','current',"
                    "'2024-01-01','2024-03-01',:hash,"
                    "'corpus/sample.docx','dataset_v1','docx-zip-v1')"
                ),
                {
                    "id": _VERSION.bytes,
                    "inst": _INSTRUMENT.bytes,
                    "hash": content_sha256(_FULL_TEXT),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_provisions "
                    "(id,version_id,provision_no,level,full_text,content_hash,"
                    "char_start,char_end) "
                    "VALUES (:id,:ver,'第一条','article',:text,:hash,0,:len)"
                ),
                {
                    "id": _PROVISION.bytes,
                    "ver": _VERSION.bytes,
                    "text": _FULL_TEXT,
                    "hash": content_sha256(_FULL_TEXT),
                    "len": len(_FULL_TEXT),
                },
            )
    finally:
        await engine.dispose()


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusRepository(session)
            assembly = LegalEvidenceAssemblyService(repo)

            class HitSearch:
                def __init__(self, hits: tuple[LegalSearchHit, ...]) -> None:
                    self._hits = hits

                async def search_dataset(self, **kwargs: object) -> tuple[LegalSearchHit, ...]:
                    return self._hits

            hit = LegalSearchHit(
                chunk_id=new_uuid7(),
                provision_id=_PROVISION,
                version_id=_VERSION,
                score=1.0,
            )
            service = LegalDatasetEvidenceService(
                dataset_search=HitSearch((hit,)),  # type: ignore[arg-type]
                evidence_assembly=assembly,
            )
            bundle = await service.search_evidence(
                alias="dataset_v1",
                query="逾期支付租金",
                model_ref="synthetic-v1",
                dimension=8,
            )
            assert bundle is not None
            assert isinstance(bundle, EvidenceBundle)
            assert len(bundle.items) == 1
            item = bundle.items[0]
            assert item.evidence_id == _PROVISION
            assert item.instrument_title == "示范法"
            assert item.provision_text == _FULL_TEXT
            assert item.dataset_version == "dataset_v1"
            assert item.authorized

            empty = LegalDatasetEvidenceService(
                dataset_search=HitSearch(()),  # type: ignore[arg-type]
                evidence_assembly=assembly,
            )
            assert (
                await empty.search_evidence(
                    alias="dataset_v1", query="无此内容", model_ref="m", dimension=8
                )
                is None
            )
    finally:
        await engine.dispose()


async def _cleanup(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM legal_provisions"))
            await connection.execute(text("DELETE FROM legal_versions"))
            await connection.execute(text("DELETE FROM legal_instruments"))
    finally:
        await engine.dispose()


def test_legal_dataset_evidence_mysql(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_seed(mysql_url))
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
