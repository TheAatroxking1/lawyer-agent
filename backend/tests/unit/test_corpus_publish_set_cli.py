from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.parametrize("batch_size", [257, 0, True, 1.5])
def test_build_arguments_reject_unbounded_or_non_integer_batch(batch_size):
    cli = import_module("lawyer_agent.cli.corpus_publish_set")
    with pytest.raises(cli.ReleaseInputError, match="batch_size_invalid"):
        cli.require_build_arguments(index_name="fresh", alias="laws", batch_size=batch_size)


def test_set_cli_requires_review_before_reading_report(monkeypatch, capsys, tmp_path):
    cli = import_module("lawyer_agent.cli.corpus_publish_set")
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("must reject before settings"))
    assert cli.main(["--import-report", str(tmp_path / "absent.jsonl")]) == 2
    assert "review" in capsys.readouterr().err


def test_single_cli_requires_review_before_source_read(monkeypatch, capsys):
    from lawyer_agent.cli import corpus_publish as cli

    monkeypatch.setattr(cli, "_prepare", lambda _: pytest.fail("must reject before source"))
    assert (
        cli.main(
            [
                "--source",
                "absent.docx",
                "--instrument-title",
                "synthetic",
                "--issuing-authority",
                "synthetic",
            ]
        )
        == 2
    )
    assert "review" in capsys.readouterr().err


def test_set_cli_check_accepts_report_without_publish_attestation(tmp_path):
    cli = import_module("lawyer_agent.cli.corpus_publish_set")
    args = cli.build_parser().parse_args(
        ["--import-report", str(tmp_path / "report.jsonl"), "--check"]
    )
    cli.require_review_arguments(args, check=args.check)


def test_release_inputs_are_mutually_exclusive():
    cli = import_module("lawyer_agent.cli.corpus_publish_set")
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--check"])
    with pytest.raises(SystemExit):
        parser.parse_args(["--check", "--import-report", "a", "--release-set", "b"])


def test_release_set_cli_hands_input_to_run_without_constructing_db(monkeypatch, tmp_path):
    cli = import_module("lawyer_agent.cli.corpus_publish_set")
    seen = []

    async def run(args):
        seen.append(args.release_set)

    monkeypatch.setattr(cli, "_run", run)
    path = tmp_path / "set.json"
    assert cli.main(["--release-set", str(path), "--check"]) == 0
    assert seen == [path]


def test_release_set_input_preserves_all_selections_and_member_evidence(monkeypatch, tmp_path):
    cli = import_module("lawyer_agent.cli.corpus_publish_set")
    from lawyer_agent.domain.common import new_uuid7
    from lawyer_agent.domain.legal_dataset_quality import ReleaseReportMember, ReleaseSelection
    from lawyer_agent.infrastructure.documents import release_set_manifest

    selections = tuple(
        ReleaseSelection(
            new_uuid7(),
            f"file:///synthetic-{i}.docx",
            "a" * 64,
            "b" * 64,
            "c" * 64,
            "review",
            1,
            1,
            new_uuid7(),
        )
        for i in range(2)
    )
    members = (
        ReleaseReportMember(str(tmp_path / "one.jsonl"), "d" * 64, 0, 1),
        ReleaseReportMember(str(tmp_path / "two.jsonl"), "e" * 64, 1, 1),
    )
    loaded = SimpleNamespace(
        selections=selections,
        members=members,
        sha256="f" * 64,
        dataset_version="dataset_v1",
        parser_version="docx-v1",
        chunk_parser_version="docx-v1/hierarchical-v1",
    )
    monkeypatch.setattr(release_set_manifest, "read_release_set_manifest", lambda _: loaded)
    result = cli.load_release_input(
        SimpleNamespace(import_report=None, release_set=tmp_path / "set.json")
    )
    assert result.selections == selections and result.members == members


@pytest.mark.parametrize(
    "extra",
    [
        ["--index-name", "Bad Name"],
        ["--index-name", "dataset_v1"],
        ["--batch-size", "0"],
        ["--review-ref", "r" * 513],
    ],
)
def test_invalid_build_options_fail_before_report_read(monkeypatch, tmp_path, extra):
    cli = import_module("lawyer_agent.cli.corpus_publish_set")
    from lawyer_agent.infrastructure.documents import release_manifest

    monkeypatch.setattr(
        release_manifest,
        "read_release_manifest",
        lambda _: pytest.fail("invalid options must not read report"),
    )
    assert (
        cli.main(
            [
                "--import-report",
                str(tmp_path / "absent.jsonl"),
                "--review-ref",
                "r",
                "--expected-quality-sha256",
                "a" * 64,
                *extra,
            ]
        )
        == 2
    )


@pytest.mark.parametrize("mode", ["check", "blocked", "stale", "publish", "error", "cancel"])
async def test_shared_gate_is_repeatable_read_and_precedes_provider(monkeypatch, capsys, mode):
    import asyncio

    from sqlalchemy.ext import asyncio as sql_asyncio

    from lawyer_agent.application import legal_dataset_quality as quality
    from lawyer_agent.application import legal_dataset_set_publish as build
    from lawyer_agent.cli import corpus_publish_set as cli
    from lawyer_agent.domain.common import new_uuid7
    from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState
    from lawyer_agent.domain.legal_dataset_quality import (
        DatasetQualityError,
        ReleaseConfiguration,
        ReleaseReportMember,
        ReleaseSelection,
    )
    from lawyer_agent.infrastructure.providers import embedding

    events = []
    close = AsyncMock(return_value=True)

    class Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            events.append("closed")

    class Connection(Context):
        async def execution_options(self, **kwargs):
            assert kwargs == {"isolation_level": "REPEATABLE READ"}
            events.append("repeatable_read")
            return self

        def begin(self):
            assert events == ["repeatable_read"]
            events.append("transaction")
            return Context()

    connection = Connection()
    engine = SimpleNamespace(connect=lambda: connection)
    monkeypatch.setattr(sql_asyncio, "AsyncSession", lambda **kwargs: Context())
    report = SimpleNamespace(
        passed=mode != "blocked",
        digest="d" * 64,
        to_dict=lambda: {"passed": mode != "blocked", "digest": "d" * 64},
    )

    async def check(self, selections, configuration):
        events.append("quality")
        return report

    monkeypatch.setattr(quality.ReleaseQualityService, "check", check)

    def provider(**kwargs):
        assert mode in ("publish", "error", "cancel")
        assert kwargs["normalize_embeddings"] is True
        assert kwargs["device"] == "cuda:2"
        assert kwargs["local_files_only"] is False
        assert events[-1] == "quality"
        events.append("provider")
        return SimpleNamespace(embed=AsyncMock(), aclose=close)

    monkeypatch.setattr(embedding, "LocalSentenceTransformerEmbeddingProvider", provider)
    candidate = DatasetSnapshot(
        id=new_uuid7(),
        dataset_name="laws",
        parser_version="test",
        state=DatasetState.PENDING,
        manifest={},
        quality_metrics={},
    )

    async def build_set(self, **kwargs):
        assert "closed" not in events
        events.append("build")
        if mode == "error":
            raise RuntimeError("build failed")
        if mode == "cancel":
            raise asyncio.CancelledError
        return candidate

    monkeypatch.setattr(build.LegalDatasetSetPublishService, "build_set", build_set)
    selection = ReleaseSelection(
        new_uuid7(),
        "file:///synthetic.docx",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "meta",
        1,
        1,
        new_uuid7(),
    )
    member = ReleaseReportMember("C:/reports/one.jsonl", "f" * 64, 0, 1)
    config = ReleaseConfiguration(
        "laws", "fake", 4, "test/hierarchical-v1", "a" * 64, report_members=(member,)
    )
    from pathlib import Path

    from lawyer_agent.infrastructure.providers import embedding_cache

    cache_directory = (
        Path(cli.__file__).parents[4] / "artifacts/legal-corpus/vectors/test-no-create"
    )
    if mode in ("check", "blocked", "stale"):
        monkeypatch.setattr(
            embedding_cache,
            "LocalEmbeddingCacheProvider",
            lambda *a, **k: pytest.fail("quality check must not construct cache"),
        )

    call = cli.check_and_build(
        engine,
        SimpleNamespace(
            opensearch_url="http://unused",
            embedding_device="cuda:2",
            embedding_local_files_only=False,
        ),
        (selection,),
        config,
        check=mode in ("check", "blocked"),
        review_ref="review",
        expected_quality_sha256=("e" if mode == "stale" else "d") * 64,
        index_name="fresh",
        batch_size=2,
        embedding_cache_directory=cache_directory
        if mode in ("check", "blocked", "stale")
        else None,
    )
    if mode in ("error", "cancel"):
        with pytest.raises(RuntimeError if mode == "error" else asyncio.CancelledError):
            await call
    elif mode in ("blocked", "stale"):
        with pytest.raises(DatasetQualityError):
            await call
    else:
        result = await call
        if mode == "publish":
            assert result.manifest["quality_sha256"] == "d" * 64
            assert result.manifest["review_ref"] == "review"
            assert result.manifest["normalization"] == "l2"
            assert result.manifest["release_reports"] == [
                {
                    "report_path": member.report_path,
                    "report_sha256": member.report_sha256,
                    "selection_start": 0,
                    "selection_count": 1,
                }
            ]
            assert result.quality_metrics["release_quality"] == report.to_dict()
        else:
            assert result is None
    assert events[-1] == "closed"
    if mode in ("publish", "error", "cancel"):
        close.assert_awaited_once_with(timeout_seconds=5.0)
    else:
        close.assert_not_awaited()
