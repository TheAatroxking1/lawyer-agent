"""Validate self-contained mixed publication evidence after JSON recovery."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, fields
from typing import Any

from lawyer_agent.domain.legal_corpus import DatasetSnapshot
from lawyer_agent.domain.legal_dataset_quality import (
    ReleaseMetadataSummary,
    ReleaseSelection,
    ReleaseSourceSummary,
    VersionQualitySummary,
)
from lawyer_agent.domain.legal_release_provenance import (
    MIXED_PARSER_VERSION,
    MixedReleaseConfiguration,
    ReleaseSetProvenance,
    mixed_quality_payload_digest,
    provenance_digest,
)


def provenance_reference(provenance: ReleaseSetProvenance) -> dict[str, str]:
    return {
        "schema_version": "release-provenance-ref-v1",
        "manifest_sha256": provenance.manifest_sha256,
        "selection_sha256": provenance.selection_sha256,
        "provenance_sha256": provenance_digest(provenance),
    }


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _validate_summary(summary: dict[str, Any], selection: ReleaseSelection) -> None:
    if set(summary) != {field.name for field in fields(VersionQualitySummary)}:
        raise ValueError
    source = asdict(
        ReleaseSourceSummary(
            **{field.name: getattr(selection, field.name) for field in fields(ReleaseSourceSummary)}
        )
    )
    if source["numbering_review"] is None:
        source.pop("numbering_review")
    if _canonical(summary["source"]) != _canonical(source):
        raise ValueError
    for key in ("blockers", "manual_review", "source_quality_flags"):
        values = summary[key]
        if (
            not isinstance(values, (tuple, list))
            or len(values) > 256
            or any(type(value) is not str or not value or len(value) > 512 for value in values)
        ):
            raise ValueError
    if summary["blockers"]:
        raise ValueError
    counts = ("provision_count", "chunk_count", "degraded_chunks")
    if any(type(summary[key]) is not int or summary[key] < 0 for key in counts):
        raise ValueError
    if (
        summary["chunk_count"] != selection.expected_chunk_count
        or summary["provision_count"]
        != (selection.expected_provision_count or selection.expected_article_count)
        or summary["degraded_chunks"] > summary["chunk_count"]
    ):
        raise ValueError
    metadata = summary["metadata"]
    if not isinstance(metadata, dict) or set(metadata) != {
        field.name for field in fields(ReleaseMetadataSummary)
    }:
        raise ValueError
    optional = {"region_code", "published_on", "effective_on", "repealed_on"}
    for key, value in metadata.items():
        if value is None and key in optional:
            continue
        if type(value) is not str or len(value) > 4096:
            raise ValueError


def validate_mixed_release_review(
    candidate: DatasetSnapshot, versions: list[str], report: dict[str, Any]
) -> None:
    manifest = candidate.manifest
    if candidate.parser_version != MIXED_PARSER_VERSION or "release_reports" in manifest:
        raise ValueError
    configuration = MixedReleaseConfiguration.from_dict(report.get("configuration"))
    expected = {
        "alias": candidate.dataset_name,
        "model_ref": manifest["model_ref"],
        "dimension": manifest["dimension"],
        "parser_version": candidate.parser_version,
        "selection_sha256": manifest["selection_sha256"],
        "normalization": "l2",
    }
    if any(
        type(getattr(configuration, key)) is not type(value) or getattr(configuration, key) != value
        for key, value in expected.items()
    ):
        raise ValueError
    provenance = configuration.provenance
    if manifest.get("release_selection_provenance") != provenance_reference(provenance):
        raise ValueError
    selected_ids = [str(entry.selection.version_id) for entry in provenance.entries]
    if versions != selected_ids:
        raise ValueError
    selected = {str(entry.selection.version_id): entry.selection for entry in provenance.entries}
    ordered_ids = sorted(selected)
    summaries, facts = report.get("versions"), report.get("version_digests")
    if (
        not isinstance(summaries, (list, tuple))
        or not isinstance(facts, list)
        or len(summaries) != len(selected)
        or len(facts) != len(selected)
    ):
        raise ValueError
    for identifier, summary, fact in zip(ordered_ids, summaries, facts, strict=True):
        if (
            not isinstance(summary, dict)
            or summary.get("version_id") != identifier
            or not isinstance(fact, dict)
            or set(fact) != {"version_id", "facts_sha256"}
            or fact["version_id"] != identifier
            or type(fact["facts_sha256"]) is not str
            or re.fullmatch(r"[a-f0-9]{64}", fact["facts_sha256"]) is None
        ):
            raise ValueError
        _validate_summary(summary, selected[identifier])
    if mixed_quality_payload_digest(report) != manifest["quality_sha256"]:
        raise ValueError
