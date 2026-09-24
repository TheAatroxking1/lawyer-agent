"""Real imports, mixed CLI quality and durable JSON recovery; no model or search."""

import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from zipfile import ZipFile

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_legal_dataset_publication_persistence import publication_mysql_url  # noqa: F401

from lawyer_agent.cli import corpus_import_batch, corpus_publish_set
from lawyer_agent.config import Settings
from lawyer_agent.domain.legal_dataset_publication import PublicationState
from lawyer_agent.domain.legal_release_provenance import (
    ReleaseEntry,
    ReleaseReplacement,
    ReleaseReportIdentity,
    selection_digest,
)
from lawyer_agent.infrastructure.documents.release_manifest import read_release_manifest
from lawyer_agent.infrastructure.documents.release_set_manifest import read_release_set_manifest
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalDatasetSnapshotModel,
    LegalVersionModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_dataset_publication import (
    SqlAlchemyDatasetPublicationStore,
)
from tests.unit.test_legal_dataset_publication import candidate

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def test_actual_mixed_import_quality_and_journal_recovery(
    publication_mysql_url,  # noqa: F811 - explicitly imported shared pytest fixture
    tmp_path,
    monkeypatch,
    capsys,
):
    import lawyer_agent.config as config_module
    import lawyer_agent.infrastructure.providers.embedding as embedding_module

    settings = Settings(
        environment="test",
        database_url=publication_mysql_url.render_as_string(hide_password=False),
        embedding_model_ref="synthetic",
        embedding_dimension=3,
    )
    monkeypatch.setattr(config_module, "Settings", lambda: settings)
    monkeypatch.setattr(corpus_publish_set, "Settings", lambda: settings)
    monkeypatch.setattr(
        embedding_module,
        "LocalSentenceTransformerEmbeddingProvider",
        lambda **kw: pytest.fail("read-only quality must not load a model"),
    )
    sources = tmp_path / "sources"
    sources.mkdir()
    rows = []
    for i in range(2):
        source = sources / f"合成法规{i}.docx"
        with ZipFile(source, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>第一条 合成义务{i}。</w:t>"
                "</w:r></w:p></w:body></w:document>",
            )
        rows.append(
            dict(
                schema_version="legal-corpus-import-v1",
                review_status="reviewed",
                metadata_review_ref="synthetic-review",
                source_path=str(source),
                source_sha256=sha256(source.read_bytes()).hexdigest(),
                title=source.stem,
                issuing_authority="合成机关",
                jurisdiction="national",
                category="law",
                published_on="2020-01-01",
                effective_on="2020-02-01",
                status="current",
            )
        )
    loaded = []
    for profile, selected in [("corpus-docx-v3", rows), ("corpus-docx-v4", rows[:1])]:
        manifest, report = tmp_path / f"{profile}.jsonl", tmp_path / f"{profile}-report.jsonl"
        manifest.write_text(
            "".join(
                json.dumps(dict(r, version_label=f"synthetic-{profile}")) + "\n" for r in selected
            ),
            encoding="utf-8",
        )
        assert (
            corpus_import_batch.main(
                [
                    "--manifest",
                    str(manifest),
                    "--source-root",
                    str(sources),
                    "--report",
                    str(report),
                    "--parser-version",
                    profile,
                ]
            )
            == 0
        )
        loaded.append(read_release_manifest(report))
    capsys.readouterr()
    reports = tuple(
        ReleaseReportIdentity(
            str(r.path),
            r.sha256,
            len(r.selections),
            r.dataset_version,
            r.parser_version,
            r.chunk_parser_version,
        )
        for r in loaded
    )
    old = ReleaseEntry(reports[0].report_sha256, 1, loaded[0].selections[0])
    extra = ReleaseEntry(reports[0].report_sha256, 2, loaded[0].selections[1])
    new = ReleaseEntry(reports[1].report_sha256, 1, loaded[1].selections[0])
    replacements, entries = (ReleaseReplacement(old, new),), (extra, new)
    release = tmp_path / "mixed-release.json"
    release.write_text(
        json.dumps(
            dict(
                schema_version="legal-corpus-release-set-v3",
                total=2,
                selection_sha256=selection_digest(reports, entries, replacements),
                numbering_reviews=[],
                reports=[
                    dict(
                        path=r.report_path,
                        sha256=r.report_sha256,
                        selection_count=r.selection_count,
                        dataset_version=r.dataset_version,
                        parser_version=r.parser_version,
                        chunk_parser_version=r.chunk_parser_version,
                    )
                    for r in reports
                ],
                replacements=[r.to_dict() for r in replacements],
            )
        ),
        encoding="utf-8",
    )
    assert (
        corpus_publish_set.main(["--release-set", str(release), "--alias", "laws", "--check"]) == 0
    )
    quality = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert quality["passed"] is True and quality["schema_version"] == "release-quality-v2"
    selected = read_release_set_manifest(release)
    configuration = corpus_publish_set.release_configuration(
        selected, alias="laws", model_ref="synthetic", dimension=3
    )
    snapshot = replace(candidate(), parser_version=configuration.parser_version)
    snapshot.manifest.update(
        version_ids=[str(e.selection.version_id) for e in entries],
        quality_sha256=quality["quality_sha256"],
        selection_sha256=configuration.selection_sha256,
        review_ref="synthetic-persistence-review",
        normalization="l2",
        **corpus_publish_set.release_provenance_fields(configuration),
    )
    snapshot.quality_metrics["release_quality"] = quality

    async def persist():
        engine = create_async_engine(publication_mysql_url)
        try:
            factory = async_sessionmaker(engine)
            store = SqlAlchemyDatasetPublicationStore(factory)
            saved = await store.create(snapshot, None)
            restored = await SqlAlchemyDatasetPublicationStore(factory).get(saved.id)
            assert restored is not None and restored.candidate == saved.candidate
            async with factory() as session:
                identifiers = set(await session.scalars(select(LegalVersionModel.id)))
            assert {
                old.selection.version_id,
                new.selection.version_id,
                extra.selection.version_id,
            } <= identifiers
        finally:
            await engine.dispose()

    asyncio.run(persist())


def test_large_publication_json_survives_all_journal_reads_and_snapshot_recovery(
    publication_mysql_url,  # noqa: F811
):
    snapshot = candidate()
    snapshot.quality_metrics["large_synthetic_field"] = "x" * (17 * 1024 * 1024) + "汉字😀"

    async def run():
        engine = create_async_engine(publication_mysql_url)
        try:
            factory = async_sessionmaker(engine)
            store = SqlAlchemyDatasetPublicationStore(factory)
            saved = await store.create(snapshot, None)
            restored = await asyncio.wait_for(store.get(saved.id), timeout=30)
            assert restored is not None and restored.candidate == saved.candidate
            active = await asyncio.wait_for(store.find_active("laws"), timeout=30)
            assert active is not None and active.candidate == saved.candidate
            await asyncio.wait_for(
                store.transition(saved.id, PublicationState.READY, PublicationState.SWITCHING),
                timeout=30,
            )
            await asyncio.wait_for(
                store.transition(
                    saved.id, PublicationState.SWITCHING, PublicationState.ACKNOWLEDGED
                ),
                timeout=30,
            )
            await asyncio.wait_for(store.complete(saved.id), timeout=30)
            async with factory() as session:
                loaded = await asyncio.wait_for(
                    SqlAlchemyLegalCorpusInventoryRepository(session).find_dataset("laws"),
                    timeout=30,
                )
                assert loaded is not None and loaded.quality_metrics == snapshot.quality_metrics
            from lawyer_agent.application.legal_corpus_read import LegalCorpusQueryService
            from lawyer_agent.infrastructure.persistence.legal_corpus_read_uow import (
                SqlAlchemyLegalCorpusReadUnitOfWork,
            )

            query = LegalCorpusQueryService(lambda: SqlAlchemyLegalCorpusReadUnitOfWork(factory))
            detail = await asyncio.wait_for(query.dataset(dataset_name="laws"), timeout=30)
            assert detail.quality_metrics == snapshot.quality_metrics
            listed = await asyncio.wait_for(query.datasets(), timeout=30)
            assert len(listed) == 1 and listed[0] == detail
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_snapshot_rejects_json_changed_after_metadata_in_read_committed(
    publication_mysql_url,  # noqa: F811
    monkeypatch,
):
    from lawyer_agent.infrastructure.persistence.repositories import (
        legal_corpus_inventory as module,
    )

    async def run():
        engine = create_async_engine(publication_mysql_url, isolation_level="READ COMMITTED")
        try:
            factory = async_sessionmaker(engine)
            snapshot = candidate()
            async with factory() as session, session.begin():
                await SqlAlchemyLegalCorpusInventoryRepository(session).upsert_dataset(snapshot)
            original_read = module.read_json_document
            changed = False

            async def change_then_read(session, statement, **kwargs):
                nonlocal changed
                if not changed:
                    changed = True
                    async with engine.begin() as other:
                        await other.execute(
                            update(LegalDatasetSnapshotModel)
                            .where(LegalDatasetSnapshotModel.id == snapshot.id)
                            .values(parser_version="concurrent", quality_metrics_json={"new": 1})
                        )
                return await original_read(session, statement, **kwargs)

            monkeypatch.setattr(module, "read_json_document", change_then_read)
            async with factory() as session:
                with pytest.raises(ValueError, match="json_document_changed"):
                    await SqlAlchemyLegalCorpusInventoryRepository(session).find_dataset("laws")
            assert changed
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_snapshot_refreshes_cached_metadata_with_the_same_read_as_its_json_digest(
    publication_mysql_url,  # noqa: F811
):
    async def run():
        engine = create_async_engine(publication_mysql_url, isolation_level="READ COMMITTED")
        try:
            factory = async_sessionmaker(engine)
            snapshot = candidate()
            async with factory() as session, session.begin():
                await SqlAlchemyLegalCorpusInventoryRepository(session).upsert_dataset(snapshot)
            async with factory() as session:
                held_model = await session.get(LegalDatasetSnapshotModel, snapshot.id)
                assert (
                    held_model is not None and held_model.parser_version == snapshot.parser_version
                )
                async with engine.begin() as other:
                    await other.execute(
                        update(LegalDatasetSnapshotModel)
                        .where(LegalDatasetSnapshotModel.id == snapshot.id)
                        .values(parser_version="new-parser", quality_metrics_json={"fresh": 1})
                    )
                result = await SqlAlchemyLegalCorpusInventoryRepository(session).find_dataset(
                    "laws"
                )
                assert result is not None
                assert result.parser_version == "new-parser" and result.quality_metrics == {
                    "fresh": 1
                }
        finally:
            await engine.dispose()

    asyncio.run(run())
