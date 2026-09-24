import json
from hashlib import sha256
from pathlib import Path

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.documents import release_set_manifest as reader


def _report(path: Path, *, source: Path, dataset="dataset_v1", parser="docx-v1") -> bytes:
    version_id, instrument_id = new_uuid7(), new_uuid7()
    rows = [
        {
            "event": "run",
            "schema_version": "corpus-import-batch-v1",
            "run_id": "a" * 32,
            "manifest_sha256": "1" * 64,
            "conversion_manifest_sha256": None,
            "total": 1,
            "mode": "import",
            "dataset_version": dataset,
            "parser_version": parser,
            "chunk_parser_version": f"{parser}/hierarchical-v1",
        },
        {
            "event": "file",
            "row": 1,
            "source_path": str(source),
            "source_sha256": "2" * 64,
            "input_sha256": "3" * 64,
            "structure_sha256": "4" * 64,
            "source_hash_verified": True,
            "metadata_review_ref": "review",
            "date_review_ref": None,
            "content_mode": "articles",
            "provision_count": 1,
            "static_review_sha256": None,
            "quality_flags": [],
            "status": "imported",
            "code": "import_completed",
            "version_id": str(version_id),
            "instrument_id": str(instrument_id),
            "article_count": 1,
            "chunk_count": 1,
        },
        {
            "event": "summary",
            "complete": True,
            "total": 1,
            "processed": 1,
            "imported": 1,
            "replayed": 0,
            "preflighted": 0,
            "failed": 0,
            "report": str(path),
        },
    ]
    raw = b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows)
    path.write_bytes(raw)
    return raw


def _set(path: Path, reports: list[tuple[Path, bytes]], *, total=None, **changes) -> bytes:
    payload = {
        "schema_version": "legal-corpus-release-set-v1",
        "total": len(reports) if total is None else total,
        "reports": [
            {"path": str(item), "sha256": sha256(raw).hexdigest(), "selection_count": 1}
            for item, raw in reports
        ],
    }
    payload.update(changes)
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return raw


def test_two_complete_reports_form_ordered_selection_with_provenance(tmp_path):
    first, second = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    one = _report(first, source=tmp_path / "one.docx")
    two = _report(second, source=tmp_path / "two.docx")
    manifest = tmp_path / "set.json"
    raw = _set(manifest, [(first, one), (second, two)])

    loaded = reader.read_release_set_manifest(manifest)

    assert len(loaded.selections) == 2
    assert loaded.sha256 == sha256(raw).hexdigest()
    assert [member.selection_start for member in loaded.members] == [0, 1]
    assert [member.selection_count for member in loaded.members] == [1, 1]
    assert [member.report_sha256 for member in loaded.members] == [
        sha256(one).hexdigest(),
        sha256(two).hexdigest(),
    ]


def test_changed_member_bytes_are_rejected(tmp_path):
    report = tmp_path / "one.jsonl"
    raw = _report(report, source=tmp_path / "one.docx")
    manifest = tmp_path / "set.json"
    _set(manifest, [(report, raw)])
    report.write_bytes(raw.replace(b'"review"', b'"Review"'))
    with pytest.raises(reader.ReleaseSetManifestError, match="release_set_member_hash_mismatch"):
        reader.read_release_set_manifest(manifest)


def test_member_read_reuses_single_report_size_bound(tmp_path, monkeypatch):
    from lawyer_agent.infrastructure.documents import release_manifest

    report = tmp_path / "one.jsonl"
    raw = _report(report, source=tmp_path / "one.docx")
    manifest = tmp_path / "set.json"
    _set(manifest, [(report, raw)])
    monkeypatch.setattr(release_manifest, "_MAX_REPORT_BYTES", 0)
    with pytest.raises(reader.ReleaseSetManifestError, match="release_set_invalid_member"):
        reader.read_release_set_manifest(manifest)


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_path",
        "duplicate_source",
        "duplicate_version",
        "dataset",
        "parser",
        "count",
        "total",
        "empty",
    ],
)
def test_cross_member_invariants_and_counts_are_strict(tmp_path, case):
    first, second = tmp_path / "one.jsonl", tmp_path / "two.jsonl"
    source = tmp_path / "one.docx"
    one = _report(first, source=source)
    two = _report(
        second,
        source=source if case == "duplicate_source" else tmp_path / "two.docx",
        dataset="dataset_v2" if case == "dataset" else "dataset_v1",
        parser="docx-v2" if case == "parser" else "docx-v1",
    )
    if case == "duplicate_version":
        rows1 = one.decode().splitlines()
        rows2 = two.decode().splitlines()
        a, b = json.loads(rows1[1]), json.loads(rows2[1])
        b["version_id"] = a["version_id"]
        rows2[1] = json.dumps(b)
        two = ("\n".join(rows2) + "\n").encode()
        second.write_bytes(two)
    reports = (
        []
        if case == "empty"
        else [
            (first, one),
            (
                first if case == "duplicate_path" else second,
                one if case == "duplicate_path" else two,
            ),
        ]
    )
    manifest = tmp_path / "set.json"
    _set(manifest, reports, total=(3 if case == "total" else None))
    if case == "count":
        payload = json.loads(manifest.read_text())
        payload["reports"][0]["selection_count"] = 2
        manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(reader.ReleaseSetManifestError):
        reader.read_release_set_manifest(manifest)


@pytest.mark.parametrize(
    "raw",
    [b'{"schema_version":"legal-corpus-release-set-v1","total":1,"total":1,"reports":[]}', b"\xff"],
)
def test_duplicate_keys_and_invalid_utf8_are_rejected(tmp_path, raw):
    path = tmp_path / "set.json"
    path.write_bytes(raw)
    with pytest.raises(reader.ReleaseSetManifestError):
        reader.read_release_set_manifest(path)


@pytest.mark.parametrize("field,value", [("total", True), ("total", 0), ("extra", 1)])
def test_schema_is_strict(tmp_path, field, value):
    report = tmp_path / "one.jsonl"
    raw = _report(report, source=tmp_path / "one.docx")
    path = tmp_path / "set.json"
    _set(path, [(report, raw)], **{field: value})
    with pytest.raises(reader.ReleaseSetManifestError):
        reader.read_release_set_manifest(path)


@pytest.mark.parametrize("limit", ["_MAX_SET_BYTES", "_MAX_MEMBERS", "_MAX_SELECTIONS"])
def test_reader_is_bounded(tmp_path, monkeypatch, limit):
    report = tmp_path / "one.jsonl"
    raw = _report(report, source=tmp_path / "one.docx")
    path = tmp_path / "set.json"
    _set(path, [(report, raw)])
    monkeypatch.setattr(reader, limit, 0)
    with pytest.raises(reader.ReleaseSetManifestError):
        reader.read_release_set_manifest(path)
