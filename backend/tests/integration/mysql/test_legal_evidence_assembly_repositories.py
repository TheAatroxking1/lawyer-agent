from __future__ import annotations

import asyncio
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.evidence import Citation, CitationGate
from lawyer_agent.application.legal_evidence_assembly import (
    LegalEvidenceAssemblyError,
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
_VERSION_OLD = UUID("01a06ae2-6100-7000-8000-0000000000c2")
_VERSION_NEW = UUID("01a06ae2-6200-7000-8000-0000000000c3")
_PROVISION_OLD = UUID("01a06ae2-6300-7000-8000-0000000000c4")
_PROVISION_NEW = UUID("01a06ae2-6400-7000-8000-0000000000c5")


async def _seed(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,version) "
                    "VALUES (:id,'中华人民共和国民法典','全国人民代表大会','national',1)"
                ),
                {"id": _INSTRUMENT.bytes},
            )
            rows = (
                (
                    _VERSION_OLD,
                    "2020 公布版",
                    "current",
                    "2020-05-28",
                    "2021-01-01",
                    None,
                    "主席令第四十五号",
                    "object://corpus/civil-code-2020.docx",
                    "dataset_v1",
                ),
                (
                    _VERSION_NEW,
                    "2020 修正版",
                    "repealed",
                    "2020-05-28",
                    "2021-01-01",
                    "2023-01-01",
                    "主席令第四十五号",
                    "object://corpus/civil-code-2020-rev.docx",
                    "dataset_v1",
                ),
            )
            for row in rows:
                (
                    version_id,
                    label,
                    status,
                    published,
                    effective,
                    repealed,
                    law_number,
                    source_ref,
                    dataset_version,
                ) = row
                await connection.execute(
                    text(
                        "INSERT INTO legal_versions "
                        "(id,instrument_id,version_label,status,published_on,effective_on,"
                        "repealed_on,law_number,source_ref,dataset_version,content_hash) "
                        "VALUES (:id,:instrument,:label,:status,:published,:effective,"
                        ":repealed,:law,:source,:dataset,:h)"
                    ),
                    {
                        "id": version_id.bytes,
                        "instrument": _INSTRUMENT.bytes,
                        "label": label,
                        "status": status,
                        "published": published,
                        "effective": effective,
                        "repealed": repealed,
                        "law": law_number,
                        "source": source_ref,
                        "dataset": dataset_version,
                        "h": bytes(32),
                    },
                )
            provisions = (
                (
                    _PROVISION_OLD,
                    _VERSION_OLD,
                    "第一条",
                    "第一条 为了保护民事主体的合法权益，制定本法。",
                ),
                (
                    _PROVISION_NEW,
                    _VERSION_NEW,
                    "第一条",
                    "第一条 为了保护民事主体的合法权益，制定本法。",
                ),
            )
            for provision_id, version_id, no, full_text in provisions:
                await connection.execute(
                    text(
                        "INSERT INTO legal_provisions "
                        "(id,version_id,provision_no,level,full_text,content_hash,"
                        "char_start,char_end) "
                        "VALUES (:id,:version,:no,'article',:text,:h,0,:end)"
                    ),
                    {
                        "id": provision_id.bytes,
                        "version": version_id.bytes,
                        "no": no,
                        "text": full_text,
                        "h": content_sha256(full_text),
                        "end": len(full_text),
                    },
                )
    finally:
        await engine.dispose()


def _hit(provision_id: UUID, version_id: UUID, score: float) -> LegalSearchHit:
    return LegalSearchHit(
        chunk_id=new_uuid7(),
        provision_id=provision_id,
        version_id=version_id,
        score=score,
    )


async def _run(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repo = SqlAlchemyLegalCorpusRepository(session)
            service = LegalEvidenceAssemblyService(repo)
            # Two hits: 2020 公布版 (current) and 2020 修正版 (repealed 2023-01-01).
            bundle = await service.assemble(
                hits=(
                    _hit(_PROVISION_NEW, _VERSION_NEW, 9.0),
                    _hit(_PROVISION_OLD, _VERSION_OLD, 6.0),
                )
            )
            assert bundle is not None
            assert [item.evidence_id for item in bundle.items] == [
                _PROVISION_NEW,
                _PROVISION_OLD,
            ]
            current_item = bundle.item(_PROVISION_OLD)
            assert current_item.instrument_title == "中华人民共和国民法典"
            assert current_item.version_label == "2020 公布版"
            assert current_item.status.value == "current"
            assert current_item.provision_text.startswith("第一条 为了保护")
            assert current_item.source_ref == "object://corpus/civil-code-2020.docx"
            assert current_item.dataset_version == "dataset_v1"
            assert current_item.authorized

            gate = CitationGate()
            verdicts = gate.verify(
                bundle,
                (
                    Citation(claim_index=0, evidence_id=_PROVISION_OLD),
                    Citation(claim_index=1, evidence_id=_PROVISION_NEW),
                ),
                target_date=date(2024, 6, 1),
            )
            # 2020 公布版 still current on 2024; 修正版 was repealed 2023-01-01.
            assert verdicts[0].allowed
            assert verdicts[0].reason == "allowed"
            assert not verdicts[1].allowed
            assert verdicts[1].reason == "repealed_on_target_date"

            # Before any version was effective, both are refused.
            early = gate.verify(
                bundle,
                (
                    Citation(claim_index=0, evidence_id=_PROVISION_OLD),
                    Citation(claim_index=1, evidence_id=_PROVISION_NEW),
                ),
                target_date=date(2020, 6, 1),
            )
            assert early[0].reason == "not_effective_on_target_date"
            assert early[1].reason == "not_effective_on_target_date"

            # Outside bundle citation is refused.
            outsider = new_uuid7()
            outside = gate.verify(
                bundle,
                (Citation(claim_index=0, evidence_id=outsider),),
                target_date=date(2022, 6, 1),
            )[0]
            assert outside.reason == "evidence_not_in_bundle"

            # Empty hits assemble to None (no evidence -> refuse upstream).
            assert await service.assemble(hits=()) is None

            # A hit referencing a provision outside the seeded version is refused.
            try:
                await service.assemble(hits=(_hit(new_uuid7(), _VERSION_NEW, 1.0),))
            except LegalEvidenceAssemblyError as exc:
                assert "provision" in str(exc)
            else:
                raise AssertionError("expected provision restore failure")
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


def test_legal_evidence_assembly_restores_and_gates(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    try:
        asyncio.run(_seed(mysql_url))
        asyncio.run(_run(mysql_url))
    finally:
        asyncio.run(_cleanup(mysql_url))
