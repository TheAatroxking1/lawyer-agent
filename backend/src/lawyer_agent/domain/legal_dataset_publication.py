"""Durable publication intent; ambiguous alias requests remain occupied."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState
from lawyer_agent.domain.legal_dataset_quality import validate_release_report_members
from lawyer_agent.domain.legal_navigation import navigation_index_name
from lawyer_agent.domain.legal_release_provenance import MIXED_PARSER_VERSION

_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,254}")


class PublicationState(StrEnum):
    READY = "ready"
    SWITCHING = "switching"
    ACKNOWLEDGED = "acknowledged"
    COMPLETED = "completed"


class PublicationError(ValueError):
    """Only a stable code, never candidate or provider payloads."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_candidate(candidate: DatasetSnapshot) -> None:
    """Check the supported manifest before any external mutation."""
    try:
        _validate_candidate(candidate)
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError):
        raise PublicationError("publication_invalid_candidate") from None


def _validate_candidate(candidate: DatasetSnapshot) -> None:
    if not isinstance(candidate, DatasetSnapshot):
        raise ValueError
    require_uuid7(candidate.id)
    if candidate.state != DatasetState.PENDING or candidate.released_at is not None:
        raise ValueError
    if not candidate.parser_version.strip() or len(candidate.parser_version) > 64:
        raise ValueError
    manifest = candidate.manifest
    alias, index = manifest["alias"], manifest["index_name"]
    if not isinstance(alias, str) or not _NAME.fullmatch(alias) or len(alias) > 64:
        raise ValueError
    if candidate.dataset_name != alias:
        raise ValueError
    if not isinstance(index, str) or not _NAME.fullmatch(index) or index == alias:
        raise ValueError
    if manifest.get("navigation_index") != navigation_index_name(index):
        raise ValueError
    if type(manifest.get("navigation_schema_version")) is not int:
        raise ValueError
    if manifest["navigation_schema_version"] != 1:
        raise ValueError
    for key in ("dimension", "indexed_documents", "navigation_documents"):
        if type(manifest.get(key)) is not int or manifest[key] <= 0:
            raise ValueError
    if not isinstance(manifest.get("model_ref"), str) or not manifest["model_ref"].strip():
        raise ValueError
    versions = manifest.get("version_ids", [manifest.get("version_id")])
    if not isinstance(versions, list) or not versions:
        raise ValueError
    for version in versions:
        if not isinstance(version, str):
            raise ValueError
        require_uuid7(UUID(version))
    if len({UUID(version) for version in versions}) != len(versions):
        raise ValueError
    if "version_id" in manifest and versions != [manifest["version_id"]]:
        raise ValueError
    if "version_count" in manifest and (
        type(manifest["version_count"]) is not int or manifest["version_count"] != len(versions)
    ):
        raise ValueError
    json.dumps(manifest, allow_nan=False)
    json.dumps(candidate.quality_metrics, allow_nan=False)
    if (
        candidate.parser_version == MIXED_PARSER_VERSION
        or any(
            key in manifest
            for key in (
                "quality_sha256",
                "review_ref",
                "selection_sha256",
                "release_selection_provenance",
            )
        )
        or ("release_quality" in candidate.quality_metrics)
    ):
        _validate_release_review(candidate, versions)


def validate_reviewed_candidate(candidate: DatasetSnapshot) -> None:
    """New production mutations require review; old journal records remain readable."""
    validate_candidate(candidate)
    if "quality_sha256" not in candidate.manifest:
        raise PublicationError("publication_quality_review_required")


def _validate_release_review(candidate: DatasetSnapshot, versions: list[str]) -> None:
    manifest = candidate.manifest
    for field in ("quality_sha256", "selection_sha256"):
        value = manifest.get(field)
        if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
            raise ValueError
    reference = manifest.get("review_ref")
    if not isinstance(reference, str) or not reference.strip() or len(reference) > 512:
        raise ValueError
    if manifest.get("normalization") != "l2":
        raise ValueError
    report = candidate.quality_metrics.get("release_quality")
    if (
        not isinstance(report, dict)
        or report.get("passed") is not True
        or report.get("quality_sha256") != manifest["quality_sha256"]
        or report.get("source_text_coverage") != "requires_source_review"
    ):
        raise ValueError
    if report.get("schema_version") == "release-quality-v2":
        from lawyer_agent.domain.legal_mixed_release_review import validate_mixed_release_review

        validate_mixed_release_review(candidate, versions, report)
        return
    if (
        report.get("schema_version") != "release-quality-v1"
        or candidate.parser_version == MIXED_PARSER_VERSION
        or "release_selection_provenance" in manifest
    ):
        raise ValueError
    configuration = report.get("configuration")
    expected = {
        "alias": candidate.dataset_name,
        "model_ref": manifest["model_ref"],
        "dimension": manifest["dimension"],
        "parser_version": candidate.parser_version,
        "selection_sha256": manifest["selection_sha256"],
        "normalization": "l2",
    }
    if not isinstance(configuration, dict):
        raise ValueError
    if "provenance" in configuration:
        raise ValueError
    if "report_members" in configuration:
        report_members = configuration["report_members"]
        if report_members == []:
            if "release_reports" in manifest:
                raise ValueError
        else:
            validate_release_report_members(report_members, selection_count=len(versions))
            if manifest.get("release_reports") != report_members:
                raise ValueError
    elif "release_reports" in manifest:
        raise ValueError
    if any(
        type(configuration.get(key)) is not type(value) or configuration.get(key) != value
        for key, value in expected.items()
    ):
        raise ValueError
    checked = report.get("versions")
    if not isinstance(checked, (list, tuple)) or len(checked) != len(versions):
        raise ValueError
    checked_ids = []
    for item in checked:
        if not isinstance(item, dict) or item.get("blockers") not in ([], ()):
            raise ValueError
        checked_ids.append(UUID(item["version_id"]))
    if len(set(checked_ids)) != len(checked_ids) or set(checked_ids) != {
        UUID(value) for value in versions
    }:
        raise ValueError


@dataclass(frozen=True, slots=True, init=False)
class DatasetPublication:
    id: UUID
    _candidate: DatasetSnapshot
    previous_target: str | None
    state: PublicationState

    def __init__(
        self,
        id: UUID,
        candidate: DatasetSnapshot,
        previous_target: str | None,
        state: PublicationState,
    ) -> None:
        try:
            require_uuid7(id)
            if not isinstance(state, PublicationState):
                raise ValueError
            validate_candidate(candidate)
            if previous_target is not None and (
                not isinstance(previous_target, str)
                or not _NAME.fullmatch(previous_target)
                or previous_target
                in (candidate.manifest["index_name"], candidate.manifest["alias"])
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise PublicationError("publication_invalid_candidate") from None
        object.__setattr__(self, "id", id)
        object.__setattr__(self, "_candidate", deepcopy(candidate))
        object.__setattr__(self, "previous_target", previous_target)
        object.__setattr__(self, "state", state)

    @property
    def candidate(self) -> DatasetSnapshot:
        """No caller can mutate this record's nested manifest or metrics."""
        return deepcopy(self._candidate)
