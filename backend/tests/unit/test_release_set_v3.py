import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_dataset_quality import ArticleNumberingReview
from lawyer_agent.domain.legal_release_provenance import (
    MIXED_PARSER_VERSION,
    ReleaseEntry,
    ReleaseReplacement,
    ReleaseReportIdentity,
    selection_digest,
)
from lawyer_agent.infrastructure.documents import release_set_manifest as reader
from lawyer_agent.infrastructure.documents.release_manifest import read_release_manifest
from tests.unit.test_release_set_manifest import _report


def _write(path, payload):
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    path.write_bytes(raw)
    return raw


def _fixture(tmp_path):
    old_path, new_path = tmp_path / "old.jsonl", tmp_path / "new.jsonl"
    old_raw = _report(old_path, source=tmp_path / "same.docx", parser="corpus-docx-v3")
    old_rows = [json.loads(line) for line in old_raw.splitlines()]
    other = dict(old_rows[1], row=2, source_path=str(tmp_path / "other.docx"),
                 version_id=str(new_uuid7()), instrument_id=str(new_uuid7()))
    old_rows.insert(2, other)
    old_rows[0]["total"] = 2
    old_rows[-1].update(total=2, processed=2, imported=2)
    old_path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in old_rows))
    new_raw = _report(new_path, source=tmp_path / "same.docx", parser="corpus-docx-v4")
    new_rows = [json.loads(line) for line in new_raw.splitlines()]
    new_rows[1]["instrument_id"] = old_rows[1]["instrument_id"]
    new_rows[1]["structure_sha256"] = "5" * 64
    new_path.write_bytes(b"".join((json.dumps(row) + "\n").encode() for row in new_rows))
    loaded = [read_release_manifest(path) for path in (old_path, new_path)]
    reports = tuple(ReleaseReportIdentity(str(item.path), item.sha256, len(item.selections),
                    item.dataset_version, item.parser_version, item.chunk_parser_version)
                    for item in loaded)
    old = ReleaseEntry(loaded[0].sha256, 1, loaded[0].selections[0])
    retained = ReleaseEntry(loaded[0].sha256, 2, loaded[0].selections[1])
    new = ReleaseEntry(loaded[1].sha256, 1, loaded[1].selections[0])
    replacements = (ReleaseReplacement(old, new),)
    entries = (retained, new)
    payload = {
        "schema_version": "legal-corpus-release-set-v3",
        "total": 2,
        "selection_sha256": selection_digest(reports, entries, replacements),
        "reports": [{"path": item.report_path, "sha256": item.report_sha256,
                     "selection_count": item.selection_count,
                     "dataset_version": item.dataset_version, "parser_version": item.parser_version,
                     "chunk_parser_version": item.chunk_parser_version} for item in reports],
        "replacements": [item.to_dict() for item in replacements],
        "numbering_reviews": [],
    }
    path = tmp_path / "mixed.json"
    _write(path, payload)
    return path, payload, reports, entries, replacements


def test_v3_dispatch_binds_all_report_rows_and_explicit_replacement(tmp_path):
    path, _, reports, entries, replacements = _fixture(tmp_path)
    loaded = reader.read_release_set_manifest(path)
    assert loaded.parser_version == loaded.chunk_parser_version == MIXED_PARSER_VERSION
    assert loaded.dataset_version == "dataset_v1"
    assert loaded.path == path
    assert loaded.sha256 == sha256(path.read_bytes()).hexdigest()
    assert loaded.selections == tuple(item.selection for item in entries)
    assert loaded.provenance.entries == entries
    assert loaded.provenance.reports == reports
    assert loaded.provenance.replacements == replacements
    assert loaded.provenance.manifest_sha256 == loaded.sha256


@pytest.mark.parametrize("case", [
    "hash", "header_parser", "header_dataset", "header_chunk", "count", "total", "digest",
    "duplicate_report", "unknown_report", "unknown_row", "row_bool", "old_selection",
    "new_selection", "new_inline_review", "no_replacement", "duplicate_replacement",
    "unknown_top", "unknown_member", "unknown_replacement", "reorder_reports",
])
def test_v3_rejects_tampered_binding_without_any_db_access(tmp_path, case):
    path, payload, *_ = _fixture(tmp_path)
    if case == "hash":
        payload["reports"][0]["sha256"] = "a" * 64
    elif case.startswith("header_"):
        field = {"header_parser": "parser_version", "header_dataset": "dataset_version",
                 "header_chunk": "chunk_parser_version"}[case]
        payload["reports"][0][field] = "wrong"
    elif case == "count":
        payload["reports"][0]["selection_count"] = 1
    elif case == "total":
        payload["total"] = 1
    elif case == "digest":
        payload["selection_sha256"] = "b" * 64
    elif case == "duplicate_report":
        payload["reports"].append(payload["reports"][0])
    elif case == "unknown_report":
        payload["replacements"][0]["old"]["report_sha256"] = "a" * 64
    elif case == "unknown_row":
        payload["replacements"][0]["old"]["row"] = 3
    elif case == "row_bool":
        payload["replacements"][0]["old"]["row"] = True
    elif case in ("old_selection", "new_selection"):
        side = case.split("_")[0]
        payload["replacements"][0][side]["selection"]["metadata_review_ref"] = "other"
    elif case == "new_inline_review":
        payload["replacements"][0]["new"]["selection"]["numbering_review"] = {
            "first_article": 2, "last_article": 2, "review_ref": "review",
            "evidence_ref": "https://example.test/evidence", "evidence_sha256": "e" * 64,
        }
    elif case == "no_replacement":
        payload["replacements"] = []
    elif case == "duplicate_replacement":
        payload["replacements"] *= 2
    elif case == "unknown_top":
        payload["extra"] = "unapproved"
    elif case == "unknown_member":
        payload["reports"][0]["extra"] = "unapproved"
    elif case == "unknown_replacement":
        payload["replacements"][0]["extra"] = "unapproved"
    elif case == "reorder_reports":
        payload["reports"].reverse()
    _write(path, payload)
    with pytest.raises(reader.ReleaseSetManifestError):
        reader.read_release_set_manifest(path)


def test_v3_numbering_review_is_bound_to_final_selection_and_replacement(tmp_path):
    path, payload, reports, entries, replacements = _fixture(tmp_path)
    evidence = tmp_path / "evidence.txt"
    evidence.write_text("人工逐条核对", encoding="utf-8")
    evidence_sha = sha256(evidence.read_bytes()).hexdigest()
    selected = entries[-1].selection
    review = ArticleNumberingReview(2, 2, "approved numbering", "https://example.test/evidence",
                                   evidence_sha)
    reviewed = replace(entries[-1], selection=replace(selected, numbering_review=review))
    entries = (entries[0], reviewed)
    replacements = (replace(replacements[0], new=reviewed),)
    payload["numbering_reviews"] = [{
        "version_id": str(selected.version_id), "source_sha256": selected.source_sha256,
        "input_sha256": selected.input_sha256, "structure_sha256": selected.structure_sha256,
        "first_article": 2, "last_article": 2, "review_ref": review.review_ref,
        "evidence_ref": review.evidence_ref, "evidence_path": str(evidence),
        "evidence_sha256": evidence_sha,
    }]
    payload["replacements"] = [item.to_dict() for item in replacements]
    payload["selection_sha256"] = selection_digest(reports, entries, replacements)
    _write(path, payload)
    loaded = reader.read_release_set_manifest(path)
    assert loaded.provenance.entries == entries
    assert loaded.provenance.replacements == replacements
    evidence.write_text("changed", encoding="utf-8")
    with pytest.raises(reader.ReleaseSetManifestError, match="evidence_hash_mismatch"):
        reader.read_release_set_manifest(path)


def test_v3_old_report_rows_cannot_receive_numbering_approval(tmp_path):
    path, payload, _, _, replacements = _fixture(tmp_path)
    old = replacements[0].old.selection
    payload["numbering_reviews"] = [{
        "version_id": str(old.version_id), "source_sha256": old.source_sha256,
        "input_sha256": old.input_sha256, "structure_sha256": old.structure_sha256,
        "first_article": 2, "last_article": 2, "review_ref": "review",
        "evidence_ref": "evidence", "evidence_path": str(tmp_path / "unused.txt"),
        "evidence_sha256": "e" * 64,
    }]
    _write(path, payload)
    with pytest.raises(reader.ReleaseSetManifestError, match="unknown_numbering_version"):
        reader.read_release_set_manifest(path)


def test_v3_aggregate_report_bytes_are_bounded(tmp_path, monkeypatch):
    from lawyer_agent.infrastructure.documents import release_set_v3

    path, payload, *_ = _fixture(tmp_path)
    size = sum(Path(item["path"]).stat().st_size for item in payload["reports"])
    monkeypatch.setattr(release_set_v3, "_MAX_TOTAL_REPORT_BYTES", size)
    reader.read_release_set_manifest(path)
    monkeypatch.setattr(release_set_v3, "_MAX_TOTAL_REPORT_BYTES", size - 1)
    with pytest.raises(reader.ReleaseSetManifestError, match="total_report_size_limit"):
        reader.read_release_set_manifest(path)


def test_old_set_size_limit_is_not_relaxed_for_v3(tmp_path, monkeypatch):
    path, payload, *_ = _fixture(tmp_path)
    monkeypatch.setattr(reader, "_MAX_SET_BYTES", 1)
    reader.read_release_set_manifest(path)
    payload["schema_version"] = "legal-corpus-release-set-v1"
    _write(path, payload)
    with pytest.raises(reader.ReleaseSetManifestError, match="size_limit"):
        reader.read_release_set_manifest(path)


def test_v3_accepts_complete_reports_without_overlaps_or_replacements(tmp_path):
    path, payload, reports, entries, _ = _fixture(tmp_path)
    payload["reports"] = payload["reports"][:1]
    complete = read_release_manifest(Path(reports[0].report_path))
    selected = tuple(ReleaseEntry(complete.sha256, row, item)
                     for row, item in enumerate(complete.selections, 1))
    payload["replacements"] = []
    payload["selection_sha256"] = selection_digest(reports[:1], selected, ())
    _write(path, payload)
    assert reader.read_release_set_manifest(path).provenance.entries == selected


def test_v3_checks_real_parser_header_even_when_declared_pair_is_valid(tmp_path):
    path, payload, *_ = _fixture(tmp_path)
    payload["reports"][0].update(parser_version="different",
                                 chunk_parser_version="different/hierarchical-v1")
    _write(path, payload)
    with pytest.raises(reader.ReleaseSetManifestError, match="version_mismatch"):
        reader.read_release_set_manifest(path)


@pytest.mark.parametrize("case", ["failed_summary", "bad_order", "duplicate_key", "invalid_utf8",
                                  "nonfinite", "unknown_field", "incomplete", "wrong_bytes"])
def test_v3_preserves_complete_report_validation(tmp_path, case):
    path, payload, *_ = _fixture(tmp_path)
    report = Path(payload["reports"][0]["path"])
    raw = report.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    if case == "failed_summary":
        rows[-1]["failed"] = 1
    elif case == "bad_order":
        rows[1]["row"] = 2
    elif case == "unknown_field":
        rows[1]["unexpected"] = True
    elif case == "nonfinite":
        rows[1]["article_count"] = float("nan")
    elif case == "incomplete":
        rows.pop()
    raw = b"".join((json.dumps(row) + "\n").encode() for row in rows)
    if case == "duplicate_key":
        raw = raw.replace(b'"row": 1', b'"row": 1, "row": 1')
    elif case == "invalid_utf8":
        raw += b"\xff"
    elif case == "wrong_bytes":
        raw += b" "
    report.write_bytes(raw)
    with pytest.raises(reader.ReleaseSetManifestError):
        reader.read_release_set_manifest(path)


@pytest.mark.parametrize("field,value", [("total", True), ("total", 20_001),
                                         ("replacements", None), ("numbering_reviews", None)])
def test_v3_top_level_types_and_limits(tmp_path, field, value):
    path, payload, *_ = _fixture(tmp_path)
    payload[field] = value
    _write(path, payload)
    with pytest.raises(reader.ReleaseSetManifestError):
        reader.read_release_set_manifest(path)


def test_v3_manifest_and_single_report_limits(tmp_path, monkeypatch):
    from lawyer_agent.infrastructure.documents import release_manifest

    path, *_ = _fixture(tmp_path)
    size = path.stat().st_size
    monkeypatch.setattr(reader, "_MAX_V3_SET_BYTES", size)
    reader.read_release_set_manifest(path)
    monkeypatch.setattr(reader, "_MAX_V3_SET_BYTES", size - 1)
    with pytest.raises(reader.ReleaseSetManifestError, match="size_limit"):
        reader.read_release_set_manifest(path)
    monkeypatch.setattr(reader, "_MAX_V3_SET_BYTES", size)
    monkeypatch.setattr(release_manifest, "_MAX_REPORT_BYTES", 1)
    with pytest.raises(reader.ReleaseSetManifestError, match="invalid_member"):
        reader.read_release_set_manifest(path)


def test_v3_reads_each_report_only_once_and_budgets_actual_snapshot(tmp_path, monkeypatch):
    from lawyer_agent.infrastructure.documents import release_set_v3

    path, payload, *_ = _fixture(tmp_path)
    expected = {Path(item["path"]): 0 for item in payload["reports"]}
    actual_open = Path.open

    def open_count(self, *args, **kwargs):
        if self in expected:
            expected[self] += 1
        return actual_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_count)
    # Deliberately fake metadata: the bound must account for the bytes actually read.
    monkeypatch.setattr(release_set_v3, "_MAX_TOTAL_REPORT_BYTES", 1)
    with pytest.raises(reader.ReleaseSetManifestError, match="total_report_size_limit"):
        reader.read_release_set_manifest(path)
    assert sum(expected.values()) == 1
    expected.update(dict.fromkeys(expected, 0))
    monkeypatch.setattr(release_set_v3, "_MAX_TOTAL_REPORT_BYTES", 512 * 1024 * 1024)
    reader.read_release_set_manifest(path)
    assert all(count == 1 for count in expected.values())
