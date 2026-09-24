import asyncio
import json
from dataclasses import replace
from hashlib import sha256
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from lawyer_agent.application.legal_corpus_import import LegalImportResult
from lawyer_agent.cli import corpus_import_batch as batch
from lawyer_agent.cli.corpus_publish import CorpusFileImportResult
from lawyer_agent.domain.common import new_uuid7


def document(path, *, valid=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    if valid:
        with ZipFile(path, "w") as archive:
            archive.writestr(
                "word/document.xml",
                (
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    "<w:body><w:p><w:r><w:t>第一条 合成正文。</w:t></w:r></w:p>"
                    "</w:body></w:document>"
                ),
            )
    else:
        path.write_bytes(b"synthetic invalid zip")
    return {
        "schema_version": "legal-corpus-import-v1",
        "review_status": "reviewed",
        "metadata_review_ref": "synthetic-review-1",
        "source_path": str(path),
        "source_sha256": sha256(path.read_bytes()).hexdigest(),
        "title": path.stem,
        "issuing_authority": "示例机关",
        "jurisdiction": "national",
    }


@pytest.fixture
def inputs(tmp_path):
    root = tmp_path / "source"
    rows = [
        document(root / "甲.docx"),
        document(root / "乙.docx", valid=False),
        document(root / "丙.docx"),
    ]
    manifest = tmp_path / "approved.jsonl"
    manifest.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    report = tmp_path / "result.jsonl"
    return root, rows, manifest, report


def args(inputs, *extra):
    root, _, manifest, report = inputs
    return [
        "--source-root",
        str(root),
        "--manifest",
        str(manifest),
        "--report",
        str(report),
        *extra,
    ]


def records(inputs):
    return [json.loads(line) for line in inputs[3].read_text(encoding="utf-8").splitlines()]


def test_date_review_recorded_even_on_file_failure(inputs):
    for row in inputs[1]:
        row.update(status="current", date_review_ref="review:dates")
    inputs[2].write_text(
        "\n".join(json.dumps(row) for row in inputs[1]), encoding="utf-8",
    )
    assert batch.main(args(inputs, "--preflight")) == 1
    assert all(row["date_review_ref"] == "review:dates" for row in records(inputs)[1:-1])


def test_preflight_records_partial_failure_without_database(inputs, monkeypatch, capsys):
    monkeypatch.setattr(batch, "_open_database", AsyncMock(side_effect=AssertionError("database")))
    assert batch.main(args(inputs, "--preflight")) == 1
    events = records(inputs)
    assert [r["event"] for r in events] == ["run", "file", "file", "file", "summary"]
    assert events[0]["manifest_sha256"] == sha256(inputs[2].read_bytes()).hexdigest()
    assert [r["status"] for r in events[1:-1]] == ["preflighted", "failed", "preflighted"]
    assert all(r["metadata_review_ref"] == "synthetic-review-1" for r in events[1:-1])
    assert events[2]["code"] == "invalid_docx_zip"
    assert events[-1]["processed"] == 3 and events[-1]["failed"] == 1
    assert events[-1]["complete"] is False
    assert "合成正文" not in inputs[3].read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["complete"] is False


def test_batch_continues_after_file_and_import_failures(inputs, monkeypatch):
    engine = type("Engine", (), {"dispose": AsyncMock()})()
    monkeypatch.setattr(batch, "_open_database", AsyncMock(return_value=(engine, object())))
    imported = LegalImportResult(new_uuid7(), new_uuid7(), True)
    importer = AsyncMock(
        side_effect=[RuntimeError("SECRET SQL BODY"), CorpusFileImportResult(imported, 1, 1)]
    )
    monkeypatch.setattr(batch, "_import_prepared", importer)
    assert batch.main(args(inputs)) == 1
    assert importer.await_count == 2
    events = records(inputs)
    assert [r["status"] for r in events[1:-1]] == ["failed", "failed", "replayed"]
    assert events[3]["version_id"] == str(imported.version_id)
    assert events[1]["code"] == "import_failed"
    assert "SECRET" not in inputs[3].read_text(encoding="utf-8")
    engine.dispose.assert_awaited_once()


def test_report_uses_prepared_source_path_matching_persisted_proof(inputs, monkeypatch):
    original = batch.corpus_publish._prepare
    canonical_root = inputs[3].parent / "canonical"

    def prepare(*args, **kwargs):
        prepared = original(*args, **kwargs)
        source = replace(
            prepared.source, source_path=canonical_root / prepared.source.source_path.name,
        )
        return replace(prepared, source=source)

    monkeypatch.setattr(batch.corpus_publish, "_prepare", prepare)
    assert batch.main(args(inputs, "--preflight")) == 1
    events = records(inputs)
    assert events[1]["source_path"] == str(canonical_root / "甲.docx")
    assert events[3]["source_path"] == str(canonical_root / "丙.docx")
    assert events[2]["source_path"] == inputs[1][1]["source_path"]


@pytest.mark.parametrize("invalid", ["pending", "late_bad_row", "existing_report", "source_report"])
def test_invalid_global_inputs_never_open_database(inputs, monkeypatch, invalid):
    root, rows, manifest, report = inputs
    database = AsyncMock(side_effect=AssertionError("database"))
    monkeypatch.setattr(batch, "_open_database", database)
    if invalid == "pending":
        rows[0]["review_status"] = "pending"
        manifest.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    elif invalid == "late_bad_row":
        with manifest.open("a", encoding="utf-8") as stream:
            stream.write('{"wrong":"shape"}\n')
    elif invalid == "existing_report":
        report.write_text("keep existing", encoding="utf-8")
    else:
        inputs = (root, rows, manifest, root / "forbidden.jsonl")
    assert batch.main(args(inputs)) == 2
    database.assert_not_awaited()
    if invalid == "existing_report":
        assert report.read_text(encoding="utf-8") == "keep existing"
    else:
        assert not inputs[3].exists()


def test_report_failure_stops_before_next_import_and_has_no_summary(inputs, monkeypatch):
    engine = type("Engine", (), {"dispose": AsyncMock()})()
    monkeypatch.setattr(batch, "_open_database", AsyncMock(return_value=(engine, object())))
    importer = AsyncMock(
        return_value=CorpusFileImportResult(
            LegalImportResult(new_uuid7(), new_uuid7(), False),
            1,
            1,
        )
    )
    monkeypatch.setattr(batch, "_import_prepared", importer)
    original = batch._write_record

    def write(stream, record):
        if record["event"] == "file":
            raise OSError("synthetic full disk")
        original(stream, record)

    monkeypatch.setattr(batch, "_write_record", write)
    assert batch.main(args(inputs)) == 1
    assert importer.await_count == 1
    assert [r["event"] for r in records(inputs)] == ["run"]
    engine.dispose.assert_awaited_once()


def test_cancellation_is_not_converted_to_a_failed_file_or_complete_report(inputs, monkeypatch):
    engine = type("Engine", (), {"dispose": AsyncMock()})()
    monkeypatch.setattr(batch, "_open_database", AsyncMock(return_value=(engine, object())))
    importer = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(batch, "_import_prepared", importer)
    with pytest.raises(asyncio.CancelledError):
        batch.main(args(inputs))
    assert importer.await_count == 1
    assert [r["event"] for r in records(inputs)] == ["run"]
    engine.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    "kind",
    ["source_ads", "manifest_ads", "file_ads", "device", "dot", "space"],
)
def test_report_rejects_windows_streams_and_aliases_before_open(inputs, kind):
    root, _, manifest, report = inputs
    paths = {
        "source_ads": str(root) + ":report",
        "manifest_ads": str(manifest) + ":report",
        "file_ads": str(report) + ":payload.jsonl",
        "device": str(report.parent / "NUL.jsonl"),
        "dot": str(report) + ".",
        "space": str(report) + " ",
    }
    namespace = batch.build_parser().parse_args(
        [
            "--source-root",
            str(root),
            "--manifest",
            str(manifest),
            "--report",
            paths[kind],
        ]
    )
    with pytest.raises(ValueError):
        batch._report_path(namespace, manifest)


def test_reviewed_manifest_cannot_override_static_recovery_gate(inputs, monkeypatch):
    from lawyer_agent.infrastructure.documents.word_conversion import ConversionRecord

    root, rows, manifest, report = inputs
    source = root / "静态恢复.doc"
    source.write_bytes(b"synthetic legacy source")
    derived = report.parent / "derived"
    target = derived / "static.docx"
    document(target)
    conversion = ConversionRecord(
        source_path=str(source),
        source_sha256=sha256(source.read_bytes()).hexdigest(),
        status="converted",
        converted_path=str(target),
        converted_sha256=sha256(target.read_bytes()).hexdigest(),
        converter_version="binary-word-static-text-v1",
        converter_fingerprint="a" * 64,
        recovery_reason="office_validation_failed",
    )
    conversion_manifest = derived / "manifest.jsonl"
    conversion_manifest.write_text(conversion.model_dump_json() + "\n", encoding="utf-8")
    row = dict(rows[0], source_path=str(source), source_sha256=conversion.source_sha256)
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    database = AsyncMock(side_effect=AssertionError("database"))
    monkeypatch.setattr(batch, "_open_database", database)
    assert (
        batch.main(
            args(
                inputs,
                "--preflight",
                "--conversion-manifest",
                str(conversion_manifest),
                "--converted-root",
                str(derived),
            )
        )
        == 1
    )
    file_result = records(inputs)[1]
    assert file_result["code"] == "static_recovery_requires_quality_review"
    assert "legacy_binary_static_text_recovery" in file_result["quality_flags"]
    database.assert_not_awaited()
