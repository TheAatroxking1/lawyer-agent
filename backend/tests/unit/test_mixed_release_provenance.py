import importlib
import json
from dataclasses import replace

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError, ReleaseSelection


def api():
    return importlib.import_module("lawyer_agent.domain.legal_release_provenance")


def fixture():
    m = api()
    reports = (
        m.ReleaseReportIdentity(
            "C:/reports/old.json", "a" * 64, 1, "ds", "v3", "v3/hierarchical-v2"
        ),
        m.ReleaseReportIdentity(
            "C:/reports/new.json", "b" * 64, 2, "ds", "v4", "v4/hierarchical-v2"
        ),
    )
    selection = ReleaseSelection(
        new_uuid7(),
        "file:///F:/laws/a.docx",
        "c" * 64,
        "d" * 64,
        "e" * 64,
        "review",
        1,
        1,
        new_uuid7(),
    )
    old = m.ReleaseEntry("a" * 64, 1, selection)
    new = m.ReleaseEntry("b" * 64, 1, replace(selection, version_id=new_uuid7()))
    extra = m.ReleaseEntry(
        "b" * 64,
        2,
        replace(
            selection,
            source_ref="file:///F:/laws/b.docx",
            version_id=new_uuid7(),
            expected_instrument_id=new_uuid7(),
        ),
    )
    return reports, (new, extra), (m.ReleaseReplacement(old, new),)


def proof():
    m = api()
    reports, entries, replacements = fixture()
    return m.ReleaseSetProvenance(
        "f" * 64, m.selection_digest(reports, entries, replacements), reports, entries, replacements
    )


def test_complete_reports_replacement_and_roundtrip():
    p = proof()
    assert api().ReleaseSetProvenance.from_dict(json.loads(json.dumps(p.to_dict()))) == p


@pytest.mark.parametrize(
    "mutation", ["drop", "row", "duplicate", "digest", "dataset", "old_review"]
)
def test_invalid_proof_is_rejected(mutation):
    m = api()
    p = proof()
    if mutation == "drop":
        fields = dict(entries=p.entries[:1])
    elif mutation == "row":
        fields = dict(entries=(p.entries[0], replace(p.entries[1], row=3)))
    elif mutation == "duplicate":
        fields = dict(entries=p.entries + p.entries[:1])
    elif mutation == "digest":
        fields = dict(selection_sha256="0" * 64)
    elif mutation == "dataset":
        fields = dict(reports=(replace(p.reports[0], dataset_version="other"), p.reports[1]))
    else:
        from lawyer_agent.domain.legal_dataset_quality import ArticleNumberingReview

        review = ArticleNumberingReview(2, 2, "review", "https://example.com/a", "a" * 64)
        fields = dict(
            replacements=(
                m.ReleaseReplacement(
                    replace(
                        p.replacements[0].old,
                        selection=replace(p.replacements[0].old.selection, numbering_review=review),
                    ),
                    p.entries[0],
                ),
            )
        )
    with pytest.raises(DatasetQualityError):
        replace(p, **fields)


@pytest.mark.parametrize("mutation", ["unknown", "bool", "uuid", "nan"])
def test_strict_deserialization(mutation):
    p = proof().to_dict()
    if mutation == "unknown":
        p["unknown"] = 1
    elif mutation == "bool":
        p["entries"][0]["row"] = True
    elif mutation == "uuid":
        p["entries"][0]["selection"]["version_id"] = "bad"
    else:
        p["entries"][0]["selection"]["expected_chunk_count"] = float("nan")
    with pytest.raises(DatasetQualityError):
        api().ReleaseSetProvenance.from_dict(p)


def test_mixed_configuration_roundtrip_and_legacy_shape():
    from lawyer_agent.domain.legal_dataset_quality import (
        ReleaseConfiguration,
        release_configuration_dict,
    )

    m = api()
    config = m.MixedReleaseConfiguration(
        "alias", "model", 1792, m.MIXED_PARSER_VERSION, "f" * 64, provenance=proof()
    )
    assert m.MixedReleaseConfiguration.from_dict(release_configuration_dict(config)) == config
    legacy = ReleaseConfiguration("alias", "model", 1792, "v3", "f" * 64)
    assert "provenance" not in release_configuration_dict(legacy)
    assert "report_members" not in release_configuration_dict(legacy)
    with pytest.raises(DatasetQualityError):
        replace(config, parser_version="v3")


def test_mixed_quality_digest_binds_summaries_and_facts():
    from lawyer_agent.domain.legal_dataset_quality import (
        ReleaseSourceSummary,
        VersionQualitySummary,
    )

    m = api()
    p = proof()
    config = m.MixedReleaseConfiguration(
        "alias", "model", 1792, m.MIXED_PARSER_VERSION, "f" * 64, provenance=p
    )
    summaries = tuple(
        VersionQualitySummary(
            str(e.selection.version_id),
            (),
            (),
            (),
            1,
            1,
            0,
            None,
            ReleaseSourceSummary(
                e.selection.source_ref, "c" * 64, "d" * 64, "e" * 64, "review", 1, 1
            ),
        )
        for e in sorted(p.entries, key=lambda e: str(e.selection.version_id))
    )
    facts = tuple({"version_id": v.version_id, "facts_sha256": "a" * 64} for v in summaries)
    digest = m.mixed_quality_digest(config, facts, summaries)
    report = m.MixedReleaseQualityReport(True, digest, summaries, config, version_digests=facts)
    payload = report.to_dict()
    assert payload["schema_version"] == "release-quality-v2"
    assert m.mixed_quality_payload_digest(json.loads(json.dumps(payload))) == digest
    payload["versions"][0]["chunk_count"] = 2
    assert m.mixed_quality_payload_digest(payload) != digest


@pytest.mark.parametrize(
    "field", ["source_ref", "source_sha256", "expected_instrument_id", "version_id"]
)
def test_replacement_cannot_change_source_identity(field):
    m = api()
    _, entries, replacements = fixture()
    new = entries[0]
    value = {
        "source_ref": "file:///F:/other.docx",
        "source_sha256": "0" * 64,
        "expected_instrument_id": new_uuid7(),
        "version_id": replacements[0].old.selection.version_id,
    }[field]
    with pytest.raises(DatasetQualityError):
        m.ReleaseReplacement(
            replacements[0].old, replace(new, selection=replace(new.selection, **{field: value}))
        )


def test_numbering_review_roundtrip():
    from lawyer_agent.domain.legal_dataset_quality import ArticleNumberingReview

    m = api()
    reports, entries, replacements = fixture()
    review = ArticleNumberingReview(2, 2, "review", "https://example.com/a", "a" * 64)
    new = replace(entries[0], selection=replace(entries[0].selection, numbering_review=review))
    entries = (new, entries[1])
    replacements = (m.ReleaseReplacement(replacements[0].old, new),)
    p = m.ReleaseSetProvenance(
        "f" * 64, m.selection_digest(reports, entries, replacements), reports, entries, replacements
    )
    assert m.ReleaseSetProvenance.from_dict(p.to_dict()) == p


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_old",
        "new_value",
        "unknown_report",
        "chain",
        "same_source",
        "same_version",
        "duplicate_report",
        "duplicate_path",
        "zero_reports",
    ],
)
def test_membership_and_coverage_rejections(mutation):
    m = api()
    reports, entries, replacements = fixture()
    if mutation == "missing_old":
        replacements = ()
    elif mutation == "new_value":
        entries = (
            replace(
                entries[0], selection=replace(entries[0].selection, metadata_review_ref="different")
            ),
            entries[1],
        )
    elif mutation == "unknown_report":
        entries = (replace(entries[0], report_sha256="0" * 64), entries[1])
    elif mutation == "chain":
        replacements = replacements + replacements
    elif mutation == "same_source":
        entries = (
            entries[0],
            replace(
                entries[1],
                selection=replace(entries[1].selection, source_ref="file:///f:/LAWS/%61.docx"),
            ),
        )
    elif mutation == "same_version":
        entries = (
            entries[0],
            replace(
                entries[1],
                selection=replace(entries[1].selection, version_id=entries[0].selection.version_id),
            ),
        )
    elif mutation == "duplicate_report":
        reports = reports + reports[:1]
    elif mutation == "duplicate_path":
        reports = (reports[0], replace(reports[1], report_path="c:/REPORTS/old.json"))
    else:
        reports = ()
    with pytest.raises(DatasetQualityError):
        m.ReleaseSetProvenance(
            "f" * 64,
            m.selection_digest(reports, entries, replacements),
            reports,
            entries,
            replacements,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("selection_count", True),
        ("selection_count", 0),
        ("selection_count", 40001),
        ("report_path", "relative.json"),
        ("dataset_version", "x" * 129),
        ("parser_version", "x" * 65),
        ("chunk_parser_version", "v4/hierarchical-v3"),
    ],
)
def test_report_identity_limits(field, value):
    reports, _, _ = fixture()
    with pytest.raises(DatasetQualityError):
        replace(reports[0], **{field: value})


@pytest.mark.parametrize(
    "field,limit", [("reports", 256), ("entries", 20000), ("replacements", 256)]
)
def test_collection_upper_bounds(field, limit):
    p = proof().to_dict()
    p[field] = p[field][:1] * (limit + 1)
    with pytest.raises(DatasetQualityError):
        api().ReleaseSetProvenance.from_dict(p)


def test_canonical_provenance_size_limit(monkeypatch):
    m = api()
    p = proof()
    size = len(
        json.dumps(p.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    )
    monkeypatch.setattr(m, "MAX_PROVENANCE_BYTES", size)
    assert m.ReleaseSetProvenance.from_dict(p.to_dict()) == p
    monkeypatch.setattr(m, "MAX_PROVENANCE_BYTES", size - 1)
    with pytest.raises(DatasetQualityError):
        m.ReleaseSetProvenance.from_dict(p.to_dict())
    with pytest.raises(DatasetQualityError):
        replace(p)


def test_twenty_thousand_sources_roundtrip():
    m = api()
    _, entries, _ = fixture()
    report = m.ReleaseReportIdentity(
        "C:/reports/all.json", "b" * 64, 20000, "ds", "v4", "v4/hierarchical-v2"
    )
    selected = tuple(
        m.ReleaseEntry(
            "b" * 64,
            i + 1,
            replace(
                entries[0].selection, version_id=new_uuid7(), source_ref=f"file:///F:/laws/{i}.docx"
            ),
        )
        for i in range(20000)
    )
    p = m.ReleaseSetProvenance(
        "f" * 64, m.selection_digest((report,), selected, ()), (report,), selected, ()
    )
    assert m.ReleaseSetProvenance.from_dict(p.to_dict()) == p


def test_selected_order_is_report_order_then_row_even_with_recomputed_digest():
    m = api()
    reports, entries, replacements = fixture()
    entries = tuple(reversed(entries))
    with pytest.raises(DatasetQualityError):
        m.ReleaseSetProvenance(
            "f" * 64,
            m.selection_digest(reports, entries, replacements),
            reports,
            entries,
            replacements,
        )
