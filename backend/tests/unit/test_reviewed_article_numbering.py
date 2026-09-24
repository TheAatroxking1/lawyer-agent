import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path

import pytest

from lawyer_agent.application.legal_corpus_publish import LegalCorpusQualityGate
from lawyer_agent.domain import legal_dataset_quality as quality_domain
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError, ReleaseSelection
from lawyer_agent.infrastructure.documents import release_set_manifest as release_set_reader
from lawyer_agent.infrastructure.documents.release_set_manifest import (
    ReleaseSetManifestError,
    read_release_set_manifest,
)


def _review(**changes: object):
    values = {
        "first_article": 3,
        "last_article": 4,
        "review_ref": "review:numbering",
        "evidence_ref": "https://official.example/evidence",
        "evidence_sha256": "a" * 64,
    }
    values.update(changes)
    return quality_domain.ArticleNumberingReview(**values)


@pytest.mark.parametrize(
    "changes",
    [
        {"first_article": True},
        {"first_article": "3"},
        {"first_article": 1},
        {"last_article": 10_000},
        {"first_article": 5, "last_article": 4},
        {"review_ref": " "},
        {"evidence_ref": "http://official.example/evidence"},
        {"evidence_ref": "https://user:secret@official.example/evidence"},
        {"evidence_ref": "https://official.example/evidence#fragment"},
        {"evidence_ref": "https://official.example/evidence#"},
        {"evidence_ref": "https://exa mple.invalid/"},
        {"evidence_ref": "https://official.example/evi\ndence"},
        {"evidence_ref": "https://example.invalid:bad/"},
        {"evidence_ref": "https://example.invalid:65536/"},
        {"evidence_sha256": "A" * 64},
    ],
)
def test_numbering_review_is_strictly_validated(changes):
    with pytest.raises(DatasetQualityError, match="quality_invalid_numbering_review"):
        _review(**changes)


@pytest.mark.parametrize(
    "evidence_ref",
    [
        "https://official.example:8443/evidence/path?source=review",
        "https://official.example/%E6%B3%95%E8%A7%84?q=%E8%AF%81%E6%8D%AE",
    ],
)
def test_numbering_review_accepts_usable_https_evidence_refs(evidence_ref):
    assert _review(evidence_ref=evidence_ref).evidence_ref == evidence_ref


def test_reviewed_sequence_requires_exact_plain_interval():
    gate = LegalCorpusQualityGate()
    review = _review()
    assert gate._sequence_break(("第三条", "第四条")) == "第三条"
    assert gate._sequence_break(("第三条", "第四条"), numbering_review=review) is None
    for numbers in (
        (),
        ("第三条", "第五条"),
        ("第三条", "第三条"),
        ("第四条", "第三条"),
        ("第三条",),
        ("第四条",),
        ("第三条之一", "第四条"),
        ("第三條", "第四条"),
    ):
        assert gate._sequence_break(numbers, numbering_review=review) is not None


@pytest.mark.parametrize(
    "numbers",
    [
        ("第03条", "第四条"),
        ("第０３条", "第四条"),
        ("03", "４"),
        ("０３", "４"),
    ],
)
def test_reviewed_sequence_rejects_leading_zero_arabic_forms(numbers):
    assert LegalCorpusQualityGate()._sequence_break(
        numbers, numbering_review=_review()
    ) is not None


@pytest.mark.parametrize(
    "numbers",
    [
        ("第3条", "第4条"),
        ("第３条", "第４条"),
        ("3", "4"),
        ("３", "４"),
        ("第三条", "第四条"),
    ],
)
def test_reviewed_sequence_accepts_canonical_supported_forms(numbers):
    assert (
        LegalCorpusQualityGate()._sequence_break(numbers, numbering_review=_review())
        is None
    )


def _write_report(path: Path, source: Path) -> tuple[bytes, dict[str, object]]:
    version_id, instrument_id = new_uuid7(), new_uuid7()
    selection = {
        "source_sha256": "2" * 64,
        "input_sha256": "3" * 64,
        "structure_sha256": "4" * 64,
        "version_id": str(version_id),
    }
    rows = [
        {
            "event": "run",
            "schema_version": "corpus-import-batch-v1",
            "run_id": "a" * 32,
            "manifest_sha256": "1" * 64,
            "conversion_manifest_sha256": None,
            "total": 1,
            "mode": "import",
            "dataset_version": "dataset_v1",
            "parser_version": "docx-v1",
            "chunk_parser_version": "docx-v1/hierarchical-v1",
        },
        {
            "event": "file",
            "row": 1,
            "source_path": str(source),
            **selection,
            "source_hash_verified": True,
            "metadata_review_ref": "review",
            "date_review_ref": None,
            "content_mode": "articles",
            "provision_count": 2,
            "static_review_sha256": None,
            "quality_flags": [],
            "status": "imported",
            "code": "import_completed",
            "instrument_id": str(instrument_id),
            "article_count": 2,
            "chunk_count": 2,
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
    return raw, selection


def _write_v2(tmp_path: Path, **review_changes: object) -> tuple[Path, Path]:
    report_path = tmp_path / "report.jsonl"
    report_raw, selection = _write_report(report_path, tmp_path / "source.docx")
    evidence_path = tmp_path / "evidence.html"
    evidence_raw = b"official evidence"
    evidence_path.write_bytes(evidence_raw)
    review = {
        **selection,
        "first_article": 3,
        "last_article": 4,
        "review_ref": "review:numbering",
        "evidence_ref": "https://official.example/evidence",
        "evidence_path": str(evidence_path.resolve()),
        "evidence_sha256": sha256(evidence_raw).hexdigest(),
    }
    review.update(review_changes)
    manifest_path = tmp_path / "set.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "legal-corpus-release-set-v2",
                "total": 1,
                "reports": [
                    {
                        "path": str(report_path.resolve()),
                        "sha256": sha256(report_raw).hexdigest(),
                        "selection_count": 1,
                    }
                ],
                "numbering_reviews": [review],
            }
        ),
        encoding="utf-8",
    )
    return manifest_path, evidence_path


def test_v2_binds_locally_hashed_numbering_review_after_report_validation(tmp_path):
    manifest_path, _ = _write_v2(tmp_path)
    loaded = read_release_set_manifest(manifest_path)
    review = loaded.selections[0].numbering_review
    assert review == quality_domain.ArticleNumberingReview(
        3,
        4,
        "review:numbering",
        "https://official.example/evidence",
        sha256(b"official evidence").hexdigest(),
    )


def test_actual_cli_release_input_preserves_bound_numbering_review(tmp_path):
    from types import SimpleNamespace

    from lawyer_agent.cli.corpus_publish_set import load_release_input

    manifest_path, _ = _write_v2(tmp_path)
    loaded = load_release_input(SimpleNamespace(import_report=None, release_set=manifest_path))
    assert loaded.selections[0].numbering_review == _review(
        evidence_sha256=sha256(b"official evidence").hexdigest()
    )


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"source_sha256": "9" * 64}, "release_set_numbering_binding_mismatch"),
        ({"first_article": 3, "last_article": 5}, "release_set_numbering_count_mismatch"),
        ({"evidence_sha256": "9" * 64}, "release_set_evidence_hash_mismatch"),
    ],
)
def test_v2_rejects_numbering_review_mismatch(tmp_path, changes, code):
    manifest_path, _ = _write_v2(tmp_path, **changes)
    with pytest.raises(ReleaseSetManifestError, match=code):
        read_release_set_manifest(manifest_path)


@pytest.mark.parametrize(
    "mutation,code",
    [
        (
            lambda payload: payload["numbering_reviews"].append(
                dict(payload["numbering_reviews"][0])
            ),
            "release_set_duplicate_numbering_review",
        ),
        (
            lambda payload: payload["numbering_reviews"][0].__setitem__(
                "version_id", str(new_uuid7())
            ),
            "release_set_unknown_numbering_version",
        ),
    ],
)
def test_v2_rejects_duplicate_or_unknown_reviewed_version(
    tmp_path, mutation, code
):
    manifest_path, _ = _write_v2(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(payload)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseSetManifestError, match=code):
        read_release_set_manifest(manifest_path)


def test_v2_rejects_numbering_review_for_non_article_selection(tmp_path):
    manifest_path, _ = _write_v2(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_path = Path(payload["reports"][0]["path"])
    rows = [json.loads(line) for line in report_path.read_text(encoding="utf-8").splitlines()]
    rows[1]["content_mode"] = "non_article_document"
    rows[1]["provision_count"] = 1
    rows[1]["article_count"] = 0
    rows[1]["quality_flags"] = ["non_article_document"]
    report_raw = b"".join(
        (json.dumps(row, ensure_ascii=False) + "\n").encode() for row in rows
    )
    report_path.write_bytes(report_raw)
    payload["reports"][0]["sha256"] = sha256(report_raw).hexdigest()
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseSetManifestError, match="release_set_numbering_mode_mismatch"):
        read_release_set_manifest(manifest_path)


def test_v2_rejects_unsafe_evidence_path(tmp_path):
    manifest_path, _ = _write_v2(tmp_path, evidence_path="relative/evidence.html")
    with pytest.raises(ReleaseSetManifestError, match="release_set_unsafe_path"):
        read_release_set_manifest(manifest_path)


@pytest.mark.parametrize(
    "limit_name,limit,code",
    [
        ("_MAX_EVIDENCE_BYTES", 4, "release_set_evidence_size_limit"),
        ("_MAX_TOTAL_EVIDENCE_BYTES", 4, "release_set_total_evidence_size_limit"),
    ],
)
def test_v2_enforces_evidence_byte_caps(tmp_path, monkeypatch, limit_name, limit, code):
    manifest_path, _ = _write_v2(tmp_path)
    monkeypatch.setattr(release_set_reader, limit_name, limit)
    with pytest.raises(ReleaseSetManifestError, match=code):
        read_release_set_manifest(manifest_path)


def test_v2_reads_shared_evidence_file_once(tmp_path, monkeypatch):
    evidence_path = tmp_path / "evidence.html"
    evidence_raw = b"official evidence"
    evidence_path.write_bytes(evidence_raw)
    reports = []
    reviews = []
    for index in range(2):
        report_path = tmp_path / f"report-{index}.jsonl"
        report_raw, selection = _write_report(
            report_path, tmp_path / f"source-{index}.docx"
        )
        reports.append(
            {
                "path": str(report_path.resolve()),
                "sha256": sha256(report_raw).hexdigest(),
                "selection_count": 1,
            }
        )
        reviews.append(
            {
                **selection,
                "first_article": 3,
                "last_article": 4,
                "review_ref": f"review:numbering:{index}",
                "evidence_ref": "https://official.example/evidence",
                "evidence_path": str(evidence_path.resolve()),
                "evidence_sha256": sha256(evidence_raw).hexdigest(),
            }
        )
    manifest_path = tmp_path / "set.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "legal-corpus-release-set-v2",
                "total": 2,
                "reports": reports,
                "numbering_reviews": reviews,
            }
        ),
        encoding="utf-8",
    )
    original_open = Path.open
    evidence_reads = 0

    def counting_open(path, *args, **kwargs):
        nonlocal evidence_reads
        if path == evidence_path:
            evidence_reads += 1
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    loaded = read_release_set_manifest(manifest_path)
    assert len(loaded.selections) == 2
    assert evidence_reads == 1


def test_v1_refuses_numbering_reviews_and_v2_requires_nonempty_list(tmp_path):
    manifest_path, _ = _write_v2(tmp_path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "legal-corpus-release-set-v1"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseSetManifestError, match="release_set_invalid"):
        read_release_set_manifest(manifest_path)
    payload["schema_version"] = "legal-corpus-release-set-v2"
    payload["numbering_reviews"] = []
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseSetManifestError, match="release_set_invalid"):
        read_release_set_manifest(manifest_path)


def test_selection_rejects_review_mode_and_count_mismatch():
    selection = ReleaseSelection(
        new_uuid7(),
        "file:///C:/synthetic.docx",
        "a" * 64,
        "b" * 64,
        "c" * 64,
        "review:metadata",
        2,
        2,
        new_uuid7(),
    )
    with pytest.raises(DatasetQualityError, match="quality_invalid_selection"):
        replace(selection, numbering_review=_review(), expected_article_count=3)
    with pytest.raises(DatasetQualityError, match="quality_invalid_selection"):
        replace(
            selection,
            numbering_review=_review(),
            content_mode="non_article_document",
            expected_article_count=0,
            expected_provision_count=1,
        )
