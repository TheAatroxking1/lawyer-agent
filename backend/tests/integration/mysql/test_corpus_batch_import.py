from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config
from test_legal_corpus_chunks_repositories import _create_database, _drop_database

from alembic import command
from lawyer_agent.cli import corpus_import_batch as batch
from lawyer_agent.config import Settings
from lawyer_agent.infrastructure.documents.word_conversion import ConversionRecord
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalChunkModel,
    LegalInstrumentModel,
    LegalProvisionModel,
    LegalVersionModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_source_proof import (
    SqlAlchemyLegalSourceProofRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


@pytest.fixture
def batch_mysql_url(mysql_url: URL, monkeypatch: pytest.MonkeyPatch) -> Iterator[URL]:
    name = f"lawyer_test_{uuid4().hex}"
    asyncio.run(_create_database(mysql_url, name))
    url = mysql_url.set(database=name)
    try:
        command.upgrade(_alembic_config(url), "head")
        import lawyer_agent.config as config_module

        settings = Settings(
            environment="test", database_url=url.render_as_string(hide_password=False)
        )
        monkeypatch.setattr(config_module, "Settings", lambda: settings)
        yield url
    finally:
        asyncio.run(_drop_database(mysql_url, name))


def _docx(path: Path, label: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>第一条 {label}。</w:t></w:r></w:p></w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def _row(path: Path, title: str) -> dict[str, object]:
    return {
        "schema_version": "legal-corpus-import-v1",
        "review_status": "reviewed",
        "metadata_review_ref": "synthetic-operator-review",
        "source_path": str(path),
        "source_sha256": sha256(path.read_bytes()).hexdigest(),
        "title": title,
        "issuing_authority": "示例机关",
        "jurisdiction": "national",
    }


def _manifest(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    path = tmp_path / "reviewed.jsonl"
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    return path


def _run(tmp_path: Path, manifest: Path, name: str, *extra: str):
    report = tmp_path / f"{name}.jsonl"
    code = batch.main(
        [
            "--source-root",
            str(tmp_path / "sources"),
            "--manifest",
            str(manifest),
            "--report",
            str(report),
            *extra,
        ]
    )
    return code, [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]


async def _database_state(url: URL):
    engine = create_async_engine(url)
    try:
        async with async_sessionmaker(engine)() as session:
            rows = (
                await session.execute(
                    select(
                        LegalInstrumentModel.title,
                        LegalVersionModel,
                    ).join(
                        LegalVersionModel,
                        LegalVersionModel.instrument_id == LegalInstrumentModel.id,
                    )
                )
            ).all()
            result = {}
            for title, version in rows:
                assert version.status == "status_unknown"
                assert version.published_on is version.effective_on is None
                proof = await SqlAlchemyLegalSourceProofRepository(session).find_for_version(
                    version.id
                )
                chunks = await SqlAlchemyLegalCorpusChunkRepository(session).chunks_for_version(
                    version.id
                )
                assert proof is not None and chunks
                result[title] = (version.id, proof, tuple(c.id for c in chunks))
            return result
    finally:
        await engine.dispose()


def test_non_article_body_persists_replays_and_passes_release_quality(batch_mysql_url, tmp_path):
    from lawyer_agent.application.legal_dataset_quality import ReleaseQualityService
    from lawyer_agent.domain.legal_corpus import ProvisionLevel
    from lawyer_agent.domain.legal_dataset_quality import ReleaseConfiguration
    from lawyer_agent.infrastructure.documents.release_manifest import read_release_manifest
    from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
        SqlAlchemyLegalCorpusRepository,
    )

    source = tmp_path / "sources" / "批复.docx"
    source.parent.mkdir()
    text = "合成批复。经研究，答复如下。此复。"
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
        ))
    row = _row(source, "批复")
    row.update(content_mode="non_article_document", status="current",
               category="judicial_interpretation",
               date_review_ref="synthetic-date-review")
    manifest = _manifest(tmp_path, [row])
    code, events = _run(tmp_path, manifest, "body")
    assert code == 0
    assert events[1]["article_count"] == 0 and events[1]["provision_count"] == 1
    loaded = read_release_manifest(tmp_path / "body.jsonl")

    async def verify():
        engine = create_async_engine(batch_mysql_url)
        try:
            async with async_sessionmaker(engine)() as session:
                corpus = SqlAlchemyLegalCorpusRepository(session)
                provisions = await corpus.provisions_for_version(loaded.selections[0].version_id)
                assert len(provisions) == 1 and provisions[0].level is ProvisionLevel.PARAGRAPH
                assert provisions[0].provision_no == "正文" and provisions[0].full_text == text
                service = ReleaseQualityService(
                    corpus, SqlAlchemyLegalCorpusChunkRepository(session),
                    SqlAlchemyLegalSourceProofRepository(session),
                )
                report = await service.check(loaded.selections, ReleaseConfiguration(
                    "synthetic", "fake", 3,
                    loaded.chunk_parser_version, loaded.sha256,
                ))
                assert report.passed, report.to_dict()
        finally:
            await engine.dispose()

    asyncio.run(verify())
    code, replay = _run(tmp_path, manifest, "body-replay")
    assert code == 0 and replay[1]["status"] == "replayed"
    assert replay[1]["version_id"] == events[1]["version_id"]


def test_batch_commits_good_files_rolls_back_bad_file_and_replays(
    batch_mysql_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "sources"
    paths = [root / f"{name}.docx" for name in ("成功甲", "无效文件", "写入故障")]
    for path in paths:
        _docx(path, "故障样本" if path == paths[2] else "合成正文")
    paths[1].write_bytes(b"synthetic bad zip")
    rows = [_row(path, path.stem) for path in paths]
    conversion = []
    derived = tmp_path / "derived"
    for label in ("派生乙", "派生丙"):
        source = root / f"{label}.doc"
        source.write_bytes(("synthetic legacy " + label).encode("utf-8"))
        target = derived / f"{label}.docx"
        _docx(target, "合成转换正文")
        rows.append(_row(source, label))
        conversion.append(
            ConversionRecord(
                source_path=str(source),
                source_sha256=sha256(source.read_bytes()).hexdigest(),
                status="converted",
                converted_path=str(target),
                converted_sha256=sha256(target.read_bytes()).hexdigest(),
                converter_version="synthetic-word-v1",
                converter_fingerprint="a" * 64,
            )
        )
    conversion_path = derived / "manifest.jsonl"
    conversion_path.write_text(
        "".join(r.model_dump_json() + "\n" for r in conversion), encoding="utf-8"
    )
    manifest = _manifest(tmp_path, rows)
    original = SqlAlchemyLegalCorpusChunkRepository.replace_chunks_for_version

    async def fail_after_chunks(self, version_id, chunks):
        await original(self, version_id, chunks)
        if any("故障样本" in c.content for c in chunks):
            raise RuntimeError("synthetic transaction failure")

    monkeypatch.setattr(
        SqlAlchemyLegalCorpusChunkRepository, "replace_chunks_for_version", fail_after_chunks
    )
    extra = ("--conversion-manifest", str(conversion_path), "--converted-root", str(derived))
    code, first = _run(tmp_path, manifest, "first", *extra)
    assert code == 1
    assert [r["status"] for r in first[1:-1]] == [
        "imported",
        "failed",
        "failed",
        "imported",
        "imported",
    ]
    state = asyncio.run(_database_state(batch_mysql_url))
    assert set(state) == {"成功甲", "派生乙", "派生丙"}
    assert state["派生乙"][1].source_sha256 != state["派生乙"][1].input_sha256
    assert (
        first[0]["conversion_manifest_sha256"] == sha256(conversion_path.read_bytes()).hexdigest()
    )
    code, second = _run(tmp_path, manifest, "second", *extra)
    assert code == 1 and second[-1]["replayed"] == 3 and second[-1]["failed"] == 2
    replay = asyncio.run(_database_state(batch_mysql_url))
    assert state == replay
    assert [r.get("version_id") for r in first[1:-1]] == [r.get("version_id") for r in second[1:-1]]


def test_interrupted_report_recovers_committed_file_from_database_on_rerun(
    batch_mysql_url: URL,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [tmp_path / "sources" / f"{label}.docx" for label in ("甲", "乙")]
    for path in paths:
        _docx(path, "合成正文")
    manifest = _manifest(tmp_path, [_row(p, p.stem) for p in paths])
    original = batch._write_record

    def interrupted(stream, record):
        if record["event"] == "file":
            raise OSError("synthetic report failure after transaction commit")
        original(stream, record)

    monkeypatch.setattr(batch, "_write_record", interrupted)
    code, partial = _run(tmp_path, manifest, "partial")
    assert code == 1 and [r["event"] for r in partial] == ["run"]
    before = asyncio.run(_database_state(batch_mysql_url))
    assert set(before) == {"甲"}
    monkeypatch.setattr(batch, "_write_record", original)
    code, complete = _run(tmp_path, manifest, "complete")
    assert code == 0 and complete[-1]["complete"] is True
    assert [r["status"] for r in complete[1:-1]] == ["replayed", "imported"]
    assert complete[1]["version_id"] == str(before["甲"][0])
    after = asyncio.run(_database_state(batch_mysql_url))
    assert set(after) == {"甲", "乙"}
    assert after["甲"] == before["甲"]


@pytest.mark.parametrize("mutation", ["content", "parent"])
def test_replay_rejects_tampered_chunk_graph_without_replacing_existing_rows(
    batch_mysql_url: URL,
    tmp_path: Path,
    mutation: str,
) -> None:
    from lawyer_agent.domain.legal_corpus import content_sha256

    source = tmp_path / "sources" / "篡改回放.docx"
    _docx(source, "长段落" * 200)
    manifest = _manifest(tmp_path, [_row(source, "篡改回放")])
    code, first = _run(tmp_path, manifest, "initial")
    assert code == 0 and first[1]["status"] == "imported"

    async def tamper_and_snapshot() -> tuple[tuple[object, ...], ...]:
        engine = create_async_engine(batch_mysql_url)
        try:
            async with async_sessionmaker(engine)() as session:
                rows = (await session.scalars(
                    select(LegalChunkModel).order_by(LegalChunkModel.id)
                )).all()
                child = next(row for row in rows if row.parent_chunk_id is not None)
                values = (
                    {"content": "篡改后内容", "content_hash": content_sha256("篡改后内容")}
                    if mutation == "content" else {"parent_chunk_id": None}
                )
                await session.execute(update(LegalChunkModel).where(
                    LegalChunkModel.id == child.id
                ).values(**values))
                await session.commit()
                reread = (await session.scalars(
                    select(LegalChunkModel).order_by(LegalChunkModel.id)
                )).all()
                return tuple((
                    row.id, row.version_id, row.provision_id, row.parent_chunk_id,
                    row.chunk_type, row.quality, row.content, row.content_hash,
                    row.parser_version, row.parent_relative_char_start,
                    row.parent_relative_char_end,
                ) for row in reread)
        finally:
            await engine.dispose()

    async def snapshot() -> tuple[tuple[object, ...], ...]:
        engine = create_async_engine(batch_mysql_url)
        try:
            async with async_sessionmaker(engine)() as session:
                rows = (await session.scalars(
                    select(LegalChunkModel).order_by(LegalChunkModel.id)
                )).all()
                return tuple((
                    row.id, row.version_id, row.provision_id, row.parent_chunk_id,
                    row.chunk_type, row.quality, row.content, row.content_hash,
                    row.parser_version, row.parent_relative_char_start,
                    row.parent_relative_char_end,
                ) for row in rows)
        finally:
            await engine.dispose()

    tampered = asyncio.run(tamper_and_snapshot())
    code, replay = _run(tmp_path, manifest, f"replay-{mutation}")
    assert code == 1
    assert replay[1]["status"] == "failed" and replay[1]["code"] == "import_failed"
    assert asyncio.run(snapshot()) == tampered


@pytest.mark.parametrize("mutation", ["rewrite", "delete"])
def test_replay_rejects_changed_database_provision_without_repairing_it(
    batch_mysql_url: URL,
    tmp_path: Path,
    mutation: str,
) -> None:
    from lawyer_agent.domain.legal_corpus import content_sha256

    source = tmp_path / "sources" / "条文篡改.docx"
    _docx(source, "原始正文")
    manifest = _manifest(tmp_path, [_row(source, "条文篡改")])
    code, first = _run(tmp_path, manifest, "provision-initial")
    assert code == 0 and first[1]["status"] == "imported"

    async def mutate_or_read(*, mutate: bool) -> tuple[object, ...] | None:
        engine = create_async_engine(batch_mysql_url)
        try:
            async with async_sessionmaker(engine)() as session:
                provision = await session.scalar(select(LegalProvisionModel))
                if provision is None:
                    return None
                if mutate and mutation == "delete":
                    await session.execute(delete(LegalChunkModel).where(
                        LegalChunkModel.provision_id == provision.id
                    ))
                    await session.execute(delete(LegalProvisionModel).where(
                        LegalProvisionModel.id == provision.id
                    ))
                    await session.commit()
                    return None
                if mutate:
                    text = "第一条 数据库内容被改写。"
                    await session.execute(update(LegalProvisionModel).where(
                        LegalProvisionModel.id == provision.id
                    ).values(full_text=text, content_hash=content_sha256(text)))
                    await session.commit()
                    provision = await session.scalar(select(LegalProvisionModel))
                    assert provision is not None
                return (
                    provision.id, provision.version_id, provision.provision_no,
                    provision.level, tuple(provision.structure_path_json), provision.title,
                    provision.full_text, provision.content_hash,
                    provision.char_start, provision.char_end,
                )
        finally:
            await engine.dispose()

    tampered = asyncio.run(mutate_or_read(mutate=True))
    code, replay = _run(tmp_path, manifest, f"provision-replay-{mutation}")
    assert code == 1
    assert replay[1]["status"] == "failed" and replay[1]["code"] == "import_failed"
    assert asyncio.run(mutate_or_read(mutate=False)) == tampered
