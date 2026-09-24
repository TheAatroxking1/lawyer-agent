from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState
from lawyer_agent.domain.legal_navigation import navigation_index_name


def _candidate():
    return DatasetSnapshot(
        id=new_uuid7(), dataset_name="laws", parser_version="test",
        state=DatasetState.PENDING, manifest={
            "alias": "laws", "index_name": "fresh", "version_ids": [str(new_uuid7())],
            "version_count": 1, "dimension": 4, "model_ref": "fake",
            "indexed_documents": 1, "navigation_documents": 1,
            "navigation_schema_version": 1, "navigation_index": navigation_index_name("fresh"),
        }, quality_metrics={"indexed_documents": 1},
    )


def _reviewed_candidate():
    value = _candidate()
    value.manifest.update(quality_sha256="a" * 64, selection_sha256="b" * 64,
                          review_ref="synthetic-review", normalization="l2")
    value.quality_metrics["release_quality"] = {
        "schema_version": "release-quality-v1", "passed": True,
        "quality_sha256": "a" * 64, "source_text_coverage": "requires_source_review",
        "configuration": {
            "alias": value.dataset_name, "model_ref": value.manifest["model_ref"],
            "dimension": value.manifest["dimension"], "parser_version": value.parser_version,
            "selection_sha256": "b" * 64, "normalization": "l2",
        },
        "versions": [{"version_id": value.manifest["version_ids"][0], "blockers": []}],
    }
    return value


def test_recovery_cli_accepts_only_uuid7_and_no_source_options():
    module = import_module("lawyer_agent.cli.corpus_publication")
    parser = module.build_parser()
    publication_id = new_uuid7()
    args = parser.parse_args(["resume", "--publication-id", str(publication_id)])
    assert args.publication_id == publication_id
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "--publication-id", str(uuid4())])
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "--publication-id", str(publication_id),
                           "--source", "secret.docx"])


def test_status_can_locate_active_publication_by_alias_but_resume_requires_id():
    module = import_module("lawyer_agent.cli.corpus_publication")
    parser = module.build_parser()
    assert parser.parse_args(["status", "--alias", "laws"]).alias == "laws"
    for args in (["resume", "--alias", "laws"], ["status", "--alias", "a" * 65],
                 ["status", "--alias", "../private"]):
        with pytest.raises(SystemExit):
            parser.parse_args(args)


def test_recovery_cli_does_not_print_raw_failure(monkeypatch, capsys):
    module = import_module("lawyer_agent.cli.corpus_publication")
    monkeypatch.setattr(module, "_run", AsyncMock(side_effect=RuntimeError("secret-token")))
    assert module.main(["status", "--publication-id", str(new_uuid7())]) == 1
    captured = capsys.readouterr()
    assert "secret-token" not in captured.err
    assert "publication_failed" in captured.err


def test_publish_cli_preserves_stable_unknown_outcome_code(monkeypatch, capsys):
    from lawyer_agent.cli import corpus_publish as cli
    from lawyer_agent.domain.legal_dataset_publication import PublicationError
    monkeypatch.setattr(cli, "_run", AsyncMock(
        side_effect=PublicationError("publication_outcome_unknown"),
    ))
    assert cli.main(["--source", "unused.docx", "--instrument-title", "synthetic",
                     "--issuing-authority", "synthetic", "--metadata-review-ref", "meta",
                     "--review-ref", "review", "--expected-quality-sha256", "a" * 64]) == 1
    assert "publication_outcome_unknown" in capsys.readouterr().err


async def test_single_file_write_uses_shared_gate_before_durable_publish(monkeypatch, tmp_path):
    from lawyer_agent.cli import corpus_publish as cli
    from lawyer_agent.cli import corpus_publish_set as reviewed
    from lawyer_agent.infrastructure.persistence import engine as engines

    events = []
    class Session:
        async def __aenter__(self):
            events.append("read_open")
            return self
        async def __aexit__(self, *args):
            events.append("read_closed")
        def begin(self):
            return self
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(engines, "create_engine", lambda _: engine)
    monkeypatch.setattr(engines, "create_session_factory", lambda _: Session)
    monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace(
        embedding_model_ref="fake", embedding_dimension=4, opensearch_url="http://unused"))
    imported = SimpleNamespace(instrument_id=new_uuid7(), version_id=new_uuid7(), replayed=False)
    monkeypatch.setattr(cli, "_import_prepared", AsyncMock(return_value=SimpleNamespace(
        imported=imported, article_count=1, provision_count=1, chunk_count=1)))
    candidate = SimpleNamespace(manifest={"index_name": "fresh", "indexed_documents": 1})
    async def check_and_build(engine, settings, selections, configuration, **kwargs):
        assert tuple(item.version_id for item in selections) == (imported.version_id,)
        assert selections[0].metadata_review_ref == "metadata-review"
        assert selections[0].expected_instrument_id == imported.instrument_id
        assert selections[0].content_mode == "articles"
        assert selections[0].expected_provision_count == 1
        assert selections[0].static_review_sha256 is None
        assert configuration.parser_version == "test/hierarchical-v2"
        assert configuration.normalization == "l2"
        assert kwargs["expected_quality_sha256"] == "a" * 64
        events.append("read_closed")
        return candidate
    monkeypatch.setattr(reviewed, "check_and_build", check_and_build)
    async def publish_candidate(factory, alias, received):
        assert received is candidate
        assert events[-1] == "read_closed"
        events.append("durable_publish")
        return SimpleNamespace(id=new_uuid7(), previous_target=None)
    monkeypatch.setattr(cli, "_publish_candidate", publish_candidate, raising=False)
    args = SimpleNamespace(index_name="fresh", alias="dataset_v1", model_ref=None,
                           dimension=None, batch_size=2, import_only=False, quality_check=False,
                           metadata_review_ref="metadata-review", review_ref="review",
                           expected_quality_sha256="a" * 64)
    await cli._write(args, SimpleNamespace(command=SimpleNamespace(parser_version="test"),
        source=SimpleNamespace(source_path=tmp_path / "synthetic.docx",
                               source_sha256="a" * 64, input_sha256="b" * 64),
        structure_sha256="c" * 64, content_mode="articles", static_review_sha256=None))
    assert events[-1] == "durable_publish"
    engine.dispose.assert_awaited_once()


async def test_status_by_alias_reads_active_record_without_mutation(monkeypatch, capsys):
    import json

    from lawyer_agent.cli import corpus_publication as cli
    from lawyer_agent.domain.legal_dataset_publication import DatasetPublication, PublicationState
    from lawyer_agent.infrastructure.persistence import engine as engines
    from lawyer_agent.infrastructure.persistence.repositories import legal_dataset_publication

    record = DatasetPublication(new_uuid7(), _candidate(), "old", PublicationState.SWITCHING)
    store = SimpleNamespace(find_active=AsyncMock(return_value=record))
    monkeypatch.setattr(legal_dataset_publication, "SqlAlchemyDatasetPublicationStore",
                        lambda _: store)
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(engines, "create_engine", lambda _: engine)
    monkeypatch.setattr(engines, "create_session_factory", lambda _: object())
    monkeypatch.setattr(cli, "Settings", lambda: object())
    await cli._run(cli.build_parser().parse_args(["status", "--alias", "laws"]))
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "switching"
    assert report["publication_id"] == str(record.id)
    store.find_active.assert_awaited_once_with("laws")
    engine.dispose.assert_awaited_once()


async def test_candidate_id_is_visible_before_switch_failure(monkeypatch, capsys):
    from lawyer_agent.application import legal_dataset_publication as application
    from lawyer_agent.cli import corpus_publish as cli
    from lawyer_agent.infrastructure.persistence.repositories import (
        legal_dataset_publication as persistence,
    )
    run_id = new_uuid7()
    store = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(id=run_id)))
    monkeypatch.setattr(persistence, "SqlAlchemyDatasetPublicationStore", lambda _: store)
    async def resume(self, received):
        assert received == run_id
        assert str(run_id) in capsys.readouterr().out
        raise RuntimeError("simulated switch failure")
    monkeypatch.setattr(application.LegalDatasetPublicationService, "resume", resume)
    candidate = _reviewed_candidate()
    alias = SimpleNamespace(active_dataset_index=AsyncMock(return_value="old"))
    with pytest.raises(RuntimeError, match="simulated"):
        await cli._publish_candidate(None, alias, candidate)


@pytest.mark.parametrize("action", ["status", "resume"])
async def test_recovery_composes_store_and_service_without_source_or_embedding(
    monkeypatch, capsys, action,
):
    import json

    from lawyer_agent.cli import corpus_publication as cli
    from lawyer_agent.cli import corpus_publish
    from lawyer_agent.domain.legal_dataset_publication import DatasetPublication, PublicationState
    from lawyer_agent.infrastructure.persistence import engine as engines
    from lawyer_agent.infrastructure.persistence.repositories import legal_dataset_publication
    from lawyer_agent.infrastructure.providers import embedding

    candidate = _candidate()
    record = DatasetPublication(new_uuid7(), candidate, "old", PublicationState.COMPLETED)
    store = SimpleNamespace(get=AsyncMock(return_value=record))
    monkeypatch.setattr(legal_dataset_publication, "SqlAlchemyDatasetPublicationStore",
                        lambda factory: store)
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(engines, "create_engine", lambda _: engine)
    monkeypatch.setattr(engines, "create_session_factory", lambda _: object())
    monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace(opensearch_url="http://unused"))
    def forbidden(*args, **kwargs):
        pytest.fail("recovery must not parse source or construct embedding provider")
    monkeypatch.setattr(corpus_publish, "_prepare", forbidden)
    monkeypatch.setattr(embedding, "LocalSentenceTransformerEmbeddingProvider", forbidden)
    await cli._run(SimpleNamespace(action=action, publication_id=record.id))
    report = json.loads(capsys.readouterr().out)
    assert report["state"] == "completed"
    assert report["publication_id"] == str(record.id)
    assert "manifest" not in report
    store.get.assert_awaited_once_with(record.id)
    engine.dispose.assert_awaited_once()


async def test_recovery_cli_refuses_unreviewed_legacy_ready_before_alias_access(monkeypatch):
    from lawyer_agent.cli import corpus_publication as cli
    from lawyer_agent.domain.legal_dataset_publication import (
        DatasetPublication,
        PublicationError,
        PublicationState,
    )
    from lawyer_agent.infrastructure.persistence import engine as engines
    from lawyer_agent.infrastructure.persistence.repositories import legal_dataset_publication
    from lawyer_agent.infrastructure.search.opensearch import OpenSearchRestClient

    record = DatasetPublication(new_uuid7(), _candidate(), "old", PublicationState.READY)
    store = SimpleNamespace(get=AsyncMock(return_value=record), transition=AsyncMock())
    monkeypatch.setattr(legal_dataset_publication, "SqlAlchemyDatasetPublicationStore",
                        lambda _: store)
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(engines, "create_engine", lambda _: engine)
    monkeypatch.setattr(engines, "create_session_factory", lambda _: object())
    monkeypatch.setattr(cli, "Settings", lambda: SimpleNamespace(opensearch_url="http://unused"))
    alias_read = AsyncMock(side_effect=AssertionError("unreviewed record reached OpenSearch"))
    monkeypatch.setattr(OpenSearchRestClient, "resolve_alias", alias_read)
    with pytest.raises(PublicationError, match="publication_quality_review_required"):
        await cli._run(SimpleNamespace(action="resume", publication_id=record.id))
    store.transition.assert_not_awaited()
    alias_read.assert_not_awaited()
