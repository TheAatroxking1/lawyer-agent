from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config
from test_legal_corpus_chunks_repositories import _create_database, _drop_database

from alembic import command
from lawyer_agent.application.legal_corpus_import import LegalCorpusImportConflict
from lawyer_agent.cli import corpus_publish as cli
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.documents.word_conversion import ConversionRecord
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalChunkModel,
    LegalInstrumentModel,
    LegalProvisionModel,
    LegalVersionModel,
)
from lawyer_agent.infrastructure.persistence.models.legal_source_proof import (
    LegalVersionSourceProofModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_source_proof import (
    SqlAlchemyLegalSourceProofRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


@pytest.fixture
def proof_mysql_url(mysql_url: URL) -> Iterator[URL]:
    name = f"lawyer_test_{uuid4().hex}"
    asyncio.run(_create_database(mysql_url, name))
    url = mysql_url.set(database=name)
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        asyncio.run(_drop_database(mysql_url, name))


def _docx(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>第一条 合成证明测试正文。</w:t></w:r></w:p></w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def _args(tmp_path: Path, url: URL, monkeypatch: pytest.MonkeyPatch, *, legacy=False):
    source = tmp_path / "sources" / ("原件.doc" if legacy else "原件.docx")
    _docx(source)
    extra = []
    if legacy:
        source.write_bytes(b"synthetic legacy bytes")
        target = tmp_path / "derived" / "input.docx"
        _docx(target)
        record = ConversionRecord(
            source_path=str(source),
            source_sha256=sha256(source.read_bytes()).hexdigest(),
            status="converted",
            converted_path=str(target),
            converted_sha256=sha256(target.read_bytes()).hexdigest(),
            converter_version="synthetic-word-v1",
            converter_fingerprint="a" * 64,
        )
        manifest = target.parent / "manifest.jsonl"
        manifest.write_text(record.model_dump_json() + "\n", encoding="utf-8")
        extra = ["--conversion-manifest", str(manifest), "--converted-root", str(target.parent)]
    settings = Settings(environment="test", database_url=url.render_as_string(hide_password=False))
    monkeypatch.setattr(cli, "Settings", lambda: settings)
    return cli.build_parser().parse_args(
        [
            "--source",
            str(source),
            "--instrument-title",
            "合成来源证明法",
            "--issuing-authority",
            "示例机关",
            "--version-label",
            "合成固定版本",
            "--import-only",
            *extra,
        ]
    )


async def _state(url: URL):
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            ids = tuple(await session.scalars(select(LegalVersionModel.id)))
            chunks = tuple(await session.scalars(select(LegalChunkModel.id)))
            proofs = [
                await SqlAlchemyLegalSourceProofRepository(session).find_for_version(i) for i in ids
            ]
            return ids, chunks, proofs
    finally:
        await engine.dispose()


@pytest.mark.parametrize("legacy", [False, True])
def test_cli_persists_original_and_input_proof_and_replays(
    proof_mysql_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    args = _args(tmp_path, proof_mysql_url, monkeypatch, legacy=legacy)
    prepared = cli._prepare(args)
    asyncio.run(cli._run(args))
    ids, chunks, proofs = asyncio.run(_state(proof_mysql_url))
    assert len(ids) == len(proofs) == 1 and chunks
    proof = proofs[0]
    assert proof == cli._source_proof(prepared)
    assert proof.source_ref == prepared.source.source_path.as_uri()
    assert proof.input_ref == prepared.source.input_path.as_uri()
    assert (proof.source_sha256 != proof.input_sha256) is legacy
    asyncio.run(cli._run(args))
    replay_ids, _, replay_proofs = asyncio.run(_state(proof_mysql_url))
    assert replay_ids == ids and replay_proofs == proofs
    if legacy:
        changed = replace(prepared.source.conversion_provenance, converter_fingerprint="b" * 64)
        altered = replace(prepared, source=replace(prepared.source, conversion_provenance=changed))
        with pytest.raises(LegalCorpusImportConflict, match="source_proof_conflict"):
            asyncio.run(cli._write(args, altered))
        assert asyncio.run(_state(proof_mysql_url))[2] == proofs


@pytest.mark.parametrize("change", ["structure", "source", "missing"])
def test_replay_proof_conflicts_preserve_existing_chunks(
    proof_mysql_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    args = _args(tmp_path, proof_mysql_url, monkeypatch)
    prepared = cli._prepare(args)
    asyncio.run(cli._run(args))
    before = asyncio.run(_state(proof_mysql_url))
    if change == "structure":
        prepared = replace(prepared, structure_sha256="c" * 64)
    elif change == "source":
        prepared = replace(
            prepared,
            source=replace(
                prepared.source,
                source_sha256="d" * 64,
                input_sha256="d" * 64,
            ),
        )
    else:

        async def remove_proof():
            engine = create_async_engine(proof_mysql_url)
            try:
                async with engine.begin() as connection:
                    await connection.execute(delete(LegalVersionSourceProofModel))
            finally:
                await engine.dispose()

        asyncio.run(remove_proof())
    code = (
        "source_proof_missing_requires_review" if change == "missing" else "source_proof_conflict"
    )
    with pytest.raises(LegalCorpusImportConflict, match=code):
        asyncio.run(cli._write(args, prepared))
    after = asyncio.run(_state(proof_mysql_url))
    assert before[:2] == after[:2]
    assert after[2] == ([None] if change == "missing" else before[2])


@pytest.mark.parametrize("failure", ["proof", "chunks"])
def test_proof_or_chunk_failure_rolls_back_whole_import(
    proof_mysql_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
        SqlAlchemyLegalCorpusChunkRepository,
    )

    args = _args(tmp_path, proof_mysql_url, monkeypatch)
    cls, method = (
        (SqlAlchemyLegalSourceProofRepository, "create_for_version")
        if failure == "proof"
        else (SqlAlchemyLegalCorpusChunkRepository, "replace_chunks_for_version")
    )
    original = getattr(cls, method)

    async def fail_after_write(self, *args, **kwargs):
        await original(self, *args, **kwargs)
        raise RuntimeError("synthetic_write_failure")

    monkeypatch.setattr(cls, method, fail_after_write)
    with pytest.raises(RuntimeError, match="synthetic_write_failure"):
        asyncio.run(cli._run(args))

    async def counts():
        engine = create_async_engine(proof_mysql_url)
        try:
            async with async_sessionmaker(engine)() as session:
                for model in (
                    LegalInstrumentModel,
                    LegalVersionModel,
                    LegalProvisionModel,
                    LegalChunkModel,
                    LegalVersionSourceProofModel,
                ):
                    assert await session.scalar(select(func.count()).select_from(model)) == 0
        finally:
            await engine.dispose()

    asyncio.run(counts())


def test_source_proof_migration_keys_and_downgrade_guard(
    proof_mysql_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _alembic_config(proof_mysql_url)
    command.downgrade(config, "20260905_12")
    command.upgrade(config, "head")
    args = _args(tmp_path, proof_mysql_url, monkeypatch)
    asyncio.run(cli._run(args))
    ids, _, proofs = asyncio.run(_state(proof_mysql_url))

    async def verify_constraints():
        engine = create_async_engine(proof_mysql_url)
        try:
            async with async_sessionmaker(engine)() as session:
                repo = SqlAlchemyLegalSourceProofRepository(session)
                with pytest.raises(IntegrityError):
                    await repo.create_for_version(ids[0], proofs[0])
                await session.rollback()
                with pytest.raises(IntegrityError):
                    await repo.create_for_version(new_uuid7(), proofs[0])
                await session.rollback()
                for statement in (
                    "UPDATE legal_version_source_proofs SET converter_version='synthetic-word-v1'",
                    "UPDATE legal_version_source_proofs "
                    "SET recovery_reason='office_validation_failed'",
                    "UPDATE legal_version_source_proofs "
                    "SET quality_flags=JSON_OBJECT('wrong','shape')",
                ):
                    with pytest.raises(OperationalError) as captured:
                        await session.execute(text(statement))
                    assert captured.value.orig.args[0] == 3819
                    await session.rollback()
        finally:
            await engine.dispose()

    asyncio.run(verify_constraints())
    with pytest.raises(RuntimeError, match="source proofs"):
        command.downgrade(config, "20260905_12")
    assert asyncio.run(_state(proof_mysql_url))[2] == proofs

    async def version():
        engine = create_async_engine(proof_mysql_url)
        try:
            async with engine.connect() as connection:
                return await connection.scalar(text("SELECT version_num FROM alembic_version"))
        finally:
            await engine.dispose()

    # Empty newer tables can downgrade first; source-proof archival blocks at revision 13.
    assert asyncio.run(version()) == "20260906_13"


def test_upgrade_preserves_legacy_version_without_backfill(
    proof_mysql_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lawyer_agent.application.legal_corpus_import import LegalCorpusImportService
    from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
        SqlAlchemyLegalCorpusImportRepository,
    )

    config = _alembic_config(proof_mysql_url)
    command.downgrade(config, "20260905_12")
    args = _args(tmp_path, proof_mysql_url, monkeypatch)
    prepared = cli._prepare(args)

    async def legacy_import():
        engine = create_async_engine(proof_mysql_url)
        try:
            async with async_sessionmaker(engine)() as session, session.begin():
                return await LegalCorpusImportService(
                    SqlAlchemyLegalCorpusImportRepository(session),
                ).import_version(prepared.command)
        finally:
            await engine.dispose()

    imported = asyncio.run(legacy_import())
    command.upgrade(config, "head")
    ids, chunks, proofs = asyncio.run(_state(proof_mysql_url))
    assert ids == (imported.version_id,) and proofs == [None] and not chunks
    with pytest.raises(LegalCorpusImportConflict, match="source_proof_missing_requires_review"):
        asyncio.run(cli._run(args))
    assert asyncio.run(_state(proof_mysql_url)) == (ids, chunks, proofs)
