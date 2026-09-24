"""Strict, self-contained provenance for an explicitly selected mixed release."""

from __future__ import annotations

import json
import ntpath
from dataclasses import asdict, dataclass, fields
from hashlib import sha256
from typing import Any, Self, cast
from urllib.parse import unquote, urlsplit
from uuid import UUID

from lawyer_agent.domain.legal_dataset_quality import (
    ArticleNumberingReview,
    DatasetQualityError,
    ReleaseConfiguration,
    ReleaseQualityReport,
    ReleaseSelection,
    VersionQualitySummary,
    _absolute_report_path,
    _sha,
    _text,
)

MIXED_PARSER_VERSION = "release-set-v3/hybrid-v1"
MAX_PROVENANCE_BYTES = 64 * 1024 * 1024


def _invalid() -> DatasetQualityError:
    return DatasetQualityError("quality_invalid_release_provenance")


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _invalid() from None


def _object(value: object, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise _invalid()
    return value


def _uuid(value: object) -> UUID:
    try:
        if not isinstance(value, str):
            raise ValueError
        result = UUID(value)
        if str(result) != value:
            raise ValueError
        return result
    except (TypeError, ValueError):
        raise _invalid() from None


def source_key(selection: ReleaseSelection) -> str:
    parsed = urlsplit(selection.source_ref)
    value = f"//{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path.lstrip("/")
    return ntpath.normcase(ntpath.normpath(unquote(value)))


def _selection_dict(selection: ReleaseSelection) -> dict[str, object]:
    result = asdict(selection)
    result["version_id"] = str(selection.version_id)
    result["expected_instrument_id"] = str(selection.expected_instrument_id)
    return result


def _selection_from_dict(value: object) -> ReleaseSelection:
    data = dict(_object(value, {field.name for field in fields(ReleaseSelection)}))
    data["version_id"] = _uuid(data["version_id"])
    data["expected_instrument_id"] = _uuid(data["expected_instrument_id"])
    if data["numbering_review"] is not None:
        review = _object(
            data["numbering_review"], {field.name for field in fields(ArticleNumberingReview)}
        )
        data["numbering_review"] = ArticleNumberingReview(**review)
    return ReleaseSelection(**data)


@dataclass(frozen=True, slots=True)
class ReleaseReportIdentity:
    report_path: str
    report_sha256: str
    selection_count: int
    dataset_version: str
    parser_version: str
    chunk_parser_version: str

    def __post_init__(self) -> None:
        if (
            not _absolute_report_path(self.report_path)
            or not _sha(self.report_sha256)
            or type(self.selection_count) is not int
            or not 1 <= self.selection_count <= 40000
            or not _text(self.dataset_version, 128)
            or not _text(self.parser_version, 64)
            or self.chunk_parser_version
            not in (
                f"{self.parser_version}/hierarchical-v1",
                f"{self.parser_version}/hierarchical-v2",
            )
        ):
            raise _invalid()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls(**_object(value, {field.name for field in fields(cls)}))


@dataclass(frozen=True, slots=True)
class ReleaseEntry:
    report_sha256: str
    row: int
    selection: ReleaseSelection

    def __post_init__(self) -> None:
        if (
            not _sha(self.report_sha256)
            or type(self.row) is not int
            or not 1 <= self.row <= 40000
            or type(self.selection) is not ReleaseSelection
        ):
            raise _invalid()

    def to_dict(self) -> dict[str, object]:
        return {
            "report_sha256": self.report_sha256,
            "row": self.row,
            "selection": _selection_dict(self.selection),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, {"report_sha256", "row", "selection"})
        return cls(data["report_sha256"], data["row"], _selection_from_dict(data["selection"]))


@dataclass(frozen=True, slots=True)
class ReleaseReplacement:
    old: ReleaseEntry
    new: ReleaseEntry

    def __post_init__(self) -> None:
        if type(self.old) is not ReleaseEntry or type(self.new) is not ReleaseEntry:
            raise _invalid()
        old, new = self.old.selection, self.new.selection
        if (
            source_key(old) != source_key(new)
            or old.source_sha256 != new.source_sha256
            or old.expected_instrument_id != new.expected_instrument_id
            or old.version_id == new.version_id
            or self.old.report_sha256 == self.new.report_sha256
        ):
            raise _invalid()

    def to_dict(self) -> dict[str, object]:
        return {"old": self.old.to_dict(), "new": self.new.to_dict()}

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(value, {"old", "new"})
        return cls(ReleaseEntry.from_dict(data["old"]), ReleaseEntry.from_dict(data["new"]))


def selection_digest(
    reports: tuple[ReleaseReportIdentity, ...],
    entries: tuple[ReleaseEntry, ...],
    replacements: tuple[ReleaseReplacement, ...],
) -> str:
    return sha256(
        _canonical(
            {
                "schema_version": "release-selection-v3",
                "reports": [item.to_dict() for item in reports],
                "entries": [item.to_dict() for item in entries],
                "replacements": [item.to_dict() for item in replacements],
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class ReleaseSetProvenance:
    manifest_sha256: str
    selection_sha256: str
    reports: tuple[ReleaseReportIdentity, ...]
    entries: tuple[ReleaseEntry, ...]
    replacements: tuple[ReleaseReplacement, ...]

    def __post_init__(self) -> None:
        if not _sha(self.manifest_sha256) or not _sha(self.selection_sha256):
            raise _invalid()
        for values, cls, lower, upper in (
            (self.reports, ReleaseReportIdentity, 1, 256),
            (self.entries, ReleaseEntry, 1, 20000),
            (self.replacements, ReleaseReplacement, 0, 256),
        ):
            if (
                type(values) is not tuple
                or not lower <= len(values) <= upper
                or any(type(item) is not cls for item in values)
            ):
                raise _invalid()
        reports = {item.report_sha256: item for item in self.reports}
        paths = {ntpath.normcase(ntpath.normpath(item.report_path)) for item in self.reports}
        if (
            len(reports) != len(self.reports)
            or len(paths) != len(self.reports)
            or len({item.dataset_version for item in self.reports}) != 1
            or sum(item.selection_count for item in self.reports) > 40000
        ):
            raise _invalid()
        selected = {(item.report_sha256, item.row): item for item in self.entries}
        report_order = {item.report_sha256: index for index, item in enumerate(self.reports)}
        last_position = (-1, 0)
        for item in self.entries:
            position = (report_order.get(item.report_sha256, -1), item.row)
            if position[0] < 0 or position <= last_position:
                raise _invalid()
            last_position = position
        if (
            len(selected) != len(self.entries)
            or len({source_key(item.selection) for item in self.entries}) != len(self.entries)
            or len({item.selection.version_id for item in self.entries}) != len(self.entries)
        ):
            raise _invalid()
        all_entries = list(self.entries)
        replaced_sources: set[str] = set()
        for replacement in self.replacements:
            old, new = replacement.old, replacement.new
            key = source_key(old.selection)
            if (
                key in replaced_sources
                or old.selection.numbering_review is not None
                or selected.get((new.report_sha256, new.row)) != new
            ):
                raise _invalid()
            replaced_sources.add(key)
            all_entries.append(old)
        seen: set[tuple[str, int]] = set()
        versions: set[UUID] = set()
        for item in all_entries:
            key_row = (item.report_sha256, item.row)
            report = reports.get(item.report_sha256)
            if (
                report is None
                or item.row > report.selection_count
                or key_row in seen
                or item.selection.version_id in versions
            ):
                raise _invalid()
            seen.add(key_row)
            versions.add(item.selection.version_id)
        if len(seen) != sum(item.selection_count for item in self.reports):
            raise _invalid()
        if self.selection_sha256 != selection_digest(self.reports, self.entries, self.replacements):
            raise _invalid()
        if len(_canonical(self.to_dict())) > MAX_PROVENANCE_BYTES:
            raise _invalid()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "release-provenance-v1",
            "manifest_sha256": self.manifest_sha256,
            "selection_sha256": self.selection_sha256,
            "reports": [item.to_dict() for item in self.reports],
            "entries": [item.to_dict() for item in self.entries],
            "replacements": [item.to_dict() for item in self.replacements],
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = _object(
            value,
            {
                "schema_version",
                "manifest_sha256",
                "selection_sha256",
                "reports",
                "entries",
                "replacements",
            },
        )
        if data["schema_version"] != "release-provenance-v1":
            raise _invalid()
        for name, low, high in (
            ("reports", 1, 256),
            ("entries", 1, 20000),
            ("replacements", 0, 256),
        ):
            if type(data[name]) is not list or not low <= len(data[name]) <= high:
                raise _invalid()
        if len(_canonical(data)) > MAX_PROVENANCE_BYTES:
            raise _invalid()
        return cls(
            data["manifest_sha256"],
            data["selection_sha256"],
            tuple(ReleaseReportIdentity.from_dict(item) for item in data["reports"]),
            tuple(ReleaseEntry.from_dict(item) for item in data["entries"]),
            tuple(ReleaseReplacement.from_dict(item) for item in data["replacements"]),
        )


def provenance_digest(provenance: ReleaseSetProvenance) -> str:
    return sha256(_canonical(provenance.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class MixedReleaseConfiguration(ReleaseConfiguration):
    provenance: ReleaseSetProvenance

    def __post_init__(self) -> None:
        ReleaseConfiguration.__post_init__(self)
        if (
            type(self.provenance) is not ReleaseSetProvenance
            or self.parser_version != MIXED_PARSER_VERSION
            or self.report_members
            or self.selection_sha256 != self.provenance.manifest_sha256
        ):
            raise DatasetQualityError("quality_invalid_configuration")

    def to_dict(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "model_ref": self.model_ref,
            "dimension": self.dimension,
            "parser_version": self.parser_version,
            "selection_sha256": self.selection_sha256,
            "normalization": self.normalization,
            "provenance": self.provenance.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: object) -> Self:
        data = dict(
            _object(
                value,
                {
                    "alias",
                    "model_ref",
                    "dimension",
                    "parser_version",
                    "selection_sha256",
                    "normalization",
                    "provenance",
                },
            )
        )
        data["provenance"] = ReleaseSetProvenance.from_dict(data["provenance"])
        return cls(**data)


def _versions_dict(versions: tuple[VersionQualitySummary, ...]) -> list[dict[str, object]]:
    result = [asdict(version) for version in versions]
    for version in result:
        if version["source"].get("numbering_review") is None:
            version["source"].pop("numbering_review")
    return result


def mixed_quality_digest(
    configuration: MixedReleaseConfiguration,
    version_digests: tuple[dict[str, str], ...],
    versions: tuple[VersionQualitySummary, ...],
) -> str:
    return sha256(
        _canonical(
            {
                "schema_version": "release-quality-v2",
                "configuration": configuration.to_dict(),
                "version_digests": list(version_digests),
                "versions": _versions_dict(versions),
            }
        )
    ).hexdigest()


def mixed_quality_payload_digest(value: object) -> str:
    """Recompute the approval binding; callers separately validate candidate semantics."""
    data = _object(
        value,
        {
            "schema_version",
            "passed",
            "quality_sha256",
            "source_text_coverage",
            "configuration",
            "versions",
            "version_digests",
        },
    )
    if data["schema_version"] != "release-quality-v2":
        raise _invalid()
    return sha256(
        _canonical(
            {
                key: data[key]
                for key in ("schema_version", "configuration", "version_digests", "versions")
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class MixedReleaseQualityReport(ReleaseQualityReport):
    version_digests: tuple[dict[str, str], ...]

    def __post_init__(self) -> None:
        if (
            type(self.configuration) is not MixedReleaseConfiguration
            or type(self.version_digests) is not tuple
            or type(self.versions) is not tuple
            or type(self.passed) is not bool
            or not _sha(self.digest)
        ):
            raise _invalid()
        expected = sorted(
            str(item.selection.version_id) for item in self.configuration.provenance.entries
        )
        actual = []
        for item in self.version_digests:
            data = _object(item, {"version_id", "facts_sha256"})
            _uuid(data["version_id"])
            if not _sha(data["facts_sha256"]):
                raise _invalid()
            actual.append(data["version_id"])
        if (
            actual != expected
            or [item.version_id for item in self.versions] != expected
            or self.passed != (not any(item.blockers for item in self.versions))
            or self.digest
            != mixed_quality_digest(self.configuration, self.version_digests, self.versions)
        ):
            raise _invalid()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "release-quality-v2",
            "passed": self.passed,
            "quality_sha256": self.digest,
            "source_text_coverage": "requires_source_review",
            "configuration": cast(MixedReleaseConfiguration, self.configuration).to_dict(),
            "version_digests": [dict(item) for item in self.version_digests],
            "versions": _versions_dict(self.versions),
        }
