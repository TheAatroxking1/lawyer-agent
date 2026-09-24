import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.documents import release_manifest as reader


def events(path):
    return [
        {"event": "run", "schema_version": "corpus-import-batch-v1",
         "run_id": uuid4().hex, "manifest_sha256": "a" * 64,
         "conversion_manifest_sha256": None, "total": 1, "mode": "import",
         "dataset_version": "dataset_v1", "parser_version": "docx-v1"},
        {"event": "file", "row": 1, "source_path": str(path.parent / "不存在.docx"),
         "source_sha256": "b" * 64, "input_sha256": "c" * 64,
         "structure_sha256": "d" * 64, "source_hash_verified": True,
         "metadata_review_ref": "合成审核/1", "quality_flags": [],
         "status": "imported", "code": "import_completed",
         "version_id": str(new_uuid7()), "instrument_id": str(new_uuid7()),
         "article_count": 1, "chunk_count": 1},
        {"event": "summary", "complete": True, "total": 1, "processed": 1,
         "imported": 1, "replayed": 0, "preflighted": 0, "failed": 0,
         "report": str(path)},
    ]


def write(path, records):
    path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
                    encoding="utf-8")


def test_non_article_report_has_zero_articles_and_one_provision(tmp_path):
    path = tmp_path / "report.jsonl"
    records = events(path)
    records[1].update(content_mode="non_article_document", article_count=0,
                      provision_count=1, quality_flags=["non_article_document"])
    write(path, records)
    selection, = reader.read_release_manifest(path).selections
    assert selection.content_mode == "non_article_document"
    assert selection.expected_article_count == 0 and selection.expected_provision_count == 1


def test_static_review_digest_is_preserved(tmp_path):
    path = tmp_path / "report.jsonl"
    records = events(path)
    records[1]["static_review_sha256"] = "e" * 64
    write(path, records)
    assert reader.read_release_manifest(path).selections[0].static_review_sha256 == "e" * 64


@pytest.mark.parametrize("version", [None, "docx-v1/hierarchical-v1", "docx-v1/hierarchical-v2"])
def test_chunk_parser_identity_separate_from_source_parser(tmp_path, version):
    path = tmp_path / "report.jsonl"
    records = events(path)
    if version is not None:
        records[0]["chunk_parser_version"] = version
    write(path, records)
    loaded = reader.read_release_manifest(path)
    assert loaded.parser_version == "docx-v1"
    assert loaded.chunk_parser_version == (version or "docx-v1/hierarchical-v1")


def test_chunk_parser_cannot_claim_another_source_parser(tmp_path):
    path = tmp_path / "report.jsonl"
    records = events(path)
    records[0]["chunk_parser_version"] = "other/hierarchical-v2"
    write(path, records)
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)


@pytest.mark.parametrize("changes", [
    {"article_count": 1}, {"provision_count": 2}, {"provision_count": None},
    {"quality_flags": []}, {"content_mode": "articles"},
])
def test_non_article_report_rejects_inconsistent_mode_counts(tmp_path, changes):
    path = tmp_path / "report.jsonl"
    records = events(path)
    records[1].update(content_mode="non_article_document", article_count=0,
                      provision_count=1, quality_flags=["non_article_document"])
    records[1].update(changes)
    write(path, records)
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)


@pytest.mark.parametrize("ref", [None, "review:dates"])
def test_date_review_reference_is_propagated(tmp_path, ref):
    path = tmp_path / "report.jsonl"
    records = events(path)
    if ref is not None:
        records[1]["date_review_ref"] = ref
    write(path, records)
    assert reader.read_release_manifest(path).selections[0].date_review_ref == ref


def test_complete_report_preserves_exact_hash_and_does_not_read_sources(tmp_path, monkeypatch):
    path = tmp_path / "report.jsonl"
    records = events(path)
    write(path, records)
    original = Path.open
    original_stat = Path.stat

    def guarded_open(self, *args, **kwargs):
        assert self == path
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)

    def guarded_stat(self, *args, **kwargs):
        assert self.suffix.lower() not in {".doc", ".docx", ".docm"}
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", guarded_stat)
    loaded = reader.read_release_manifest(path)
    assert loaded.path == path and loaded.sha256 == sha256(path.read_bytes()).hexdigest()
    assert loaded.parser_version == "docx-v1" and loaded.dataset_version == "dataset_v1"
    assert len(loaded.selections) == 1
    item = loaded.selections[0]
    assert item.source_ref == Path(records[1]["source_path"]).as_uri()
    assert item.metadata_review_ref == "合成审核/1"
    assert str(item.version_id) == records[1]["version_id"]
    assert str(item.expected_instrument_id) == records[1]["instrument_id"]
    assert item.expected_article_count == 1 and item.expected_chunk_count == 1


@pytest.mark.parametrize("event,field,value", [
    (0, "mode", "preflight"), (0, "total", True), (0, "total", 0),
    (0, "manifest_sha256", "A" * 64), (0, "parser_version", " "),
    (0, "unexpected", "payload"), (1, "row", 2), (1, "row", True),
    (1, "status", "failed"), (1, "code", "source_preflight_passed"),
    (1, "source_hash_verified", 1), (1, "source_sha256", "b" * 63),
    (1, "input_sha256", "B" * 64), (1, "structure_sha256", "d" * 64 + "\n"),
    (1, "version_id", str(uuid4())), (1, "instrument_id", str(uuid4())),
    (1, "article_count", 0), (1, "chunk_count", True),
    (1, "metadata_review_ref", " "), (1, "metadata_review_ref", "x" * 513),
    (1, "quality_flags", [2]), (1, "source_path", "relative.docx"),
    (2, "complete", False), (2, "complete", 1), (2, "processed", 2),
    (2, "imported", 0), (2, "replayed", 1), (2, "failed", 1),
    (2, "preflighted", 1), (2, "extra", None),
])
def test_malformed_fields_rejected(tmp_path, event, field, value):
    path = tmp_path / "report.jsonl"
    records = events(path)
    records[event][field] = value
    write(path, records)
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)


@pytest.mark.parametrize("change", ["no_summary", "no_header", "no_file", "extra", "old"])
def test_partial_and_legacy_reports_rejected(tmp_path, change):
    path = tmp_path / "report.jsonl"
    records = events(path)
    if change == "no_summary":
        records.pop()
    elif change == "no_header":
        records.pop(0)
    elif change == "no_file":
        records.pop(1)
    elif change == "extra":
        records.append(records[-1])
    else:
        del records[1]["metadata_review_ref"]
    write(path, records)
    with pytest.raises(reader.ReleaseManifestError) as captured:
        reader.read_release_manifest(path)
    if change == "old":
        assert captured.value.code == "release_metadata_review_missing"


@pytest.mark.parametrize("duplicate", ["source", "version"])
def test_duplicate_selections_rejected(tmp_path, duplicate):
    path = tmp_path / "report.jsonl"
    records = events(path)
    second = deepcopy(records[1])
    second["row"] = 2
    if duplicate == "source":
        second["version_id"] = str(new_uuid7())
    else:
        second["source_path"] = str(tmp_path / "another.doc")
    records.insert(2, second)
    records[0]["total"] = 2
    records[-1].update(total=2, processed=2, imported=2)
    write(path, records)
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)


@pytest.mark.parametrize("source", [
    "NUL.docx", "source.docx:payload", "source.docx.", "source.docx ",
    "~$source.docx", "source.exe", "../source.docx",
])
def test_unsafe_source_path_rejected_without_opening_it(tmp_path, source):
    path = tmp_path / "report.jsonl"
    records = events(path)
    records[1]["source_path"] = str(tmp_path / source)
    write(path, records)
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)


@pytest.mark.parametrize("raw", [b"\xff", b"{}\n", b"\n", b'{"event":"run","event":"file"}\n'])
def test_bad_encoding_json_and_duplicate_keys_rejected(tmp_path, raw):
    path = tmp_path / "report.jsonl"
    path.write_bytes(raw)
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)


@pytest.mark.parametrize("limit", ["bytes", "line", "rows"])
def test_reader_bounds(tmp_path, monkeypatch, limit):
    path = tmp_path / "report.jsonl"
    write(path, events(path))
    key = {
        "bytes": "_MAX_REPORT_BYTES", "line": "_MAX_REPORT_LINE", "rows": "_MAX_REPORT_ROWS",
    }[limit]
    monkeypatch.setattr(reader, key, 0)
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)


def test_relative_and_missing_report_paths_have_sanitized_errors(tmp_path):
    for path in (Path("relative.jsonl"), tmp_path / "SECRET_missing.jsonl"):
        with pytest.raises(reader.ReleaseManifestError) as captured:
            reader.read_release_manifest(path)
        assert "SECRET" not in str(captured.value)


def test_imported_and_replayed_records_match_separate_counts(tmp_path):
    path = tmp_path / "report.jsonl"
    records = events(path)
    second = deepcopy(records[1])
    second.update(row=2, version_id=str(new_uuid7()), status="replayed",
                  source_path=str(tmp_path / "second.docm"))
    records.insert(2, second)
    records[0]["total"] = 2
    records[-1].update(total=2, processed=2, replayed=1)
    write(path, records)
    assert len(reader.read_release_manifest(path).selections) == 2


def test_report_redirect_rejected_before_open(tmp_path, monkeypatch):
    def redirected(path):
        raise ValueError("SECRET redirected path")

    monkeypatch.setattr(reader, "unredirected_path", redirected)
    with pytest.raises(reader.ReleaseManifestError) as captured:
        reader.read_release_manifest(tmp_path / "report.jsonl")
    assert "SECRET" not in str(captured.value)


def test_duplicate_keys_inside_otherwise_valid_report_rejected(tmp_path):
    path = tmp_path / "report.jsonl"
    write(path, events(path))
    path.write_text(path.read_text(encoding="utf-8").replace(
        '"source_hash_verified": true',
        '"source_hash_verified": false, "source_hash_verified": true',
    ), encoding="utf-8")
    with pytest.raises(reader.ReleaseManifestError):
        reader.read_release_manifest(path)
