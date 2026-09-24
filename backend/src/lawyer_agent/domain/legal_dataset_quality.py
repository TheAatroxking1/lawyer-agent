"""Typed release selection, configuration and body-free quality results."""

from __future__ import annotations

import ntpath
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7


class DatasetQualityError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _text(value: object, limit: int) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= limit


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _absolute_report_path(value: object) -> bool:
    if not _text(value, 4096) or not isinstance(value, str):
        return False
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        not ntpath.isabs(value)
        or any(ord(character) < 32 for character in value)
        or value.startswith(("\\\\?\\", "\\\\.\\"))
    ):
        return False
    drive, tail = ntpath.splitdrive(value)
    if not drive or not tail.startswith(("\\", "/")):
        return False
    return all(
        part not in ("", ".", "..")
        and ":" not in part
        and not part.endswith((" ", "."))
        and part.split(".", 1)[0].upper() not in devices
        for part in re.split(r"[\\/]", tail.lstrip("\\/"))
    )


@dataclass(frozen=True, slots=True)
class ArticleNumberingReview:
    first_article: int
    last_article: int
    review_ref: str
    evidence_ref: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        try:
            if (
                type(self.first_article) is not int
                or type(self.last_article) is not int
                or not 2 <= self.first_article <= self.last_article <= 9999
                or not _text(self.review_ref, 512)
                or not _text(self.evidence_ref, 4096)
                or re.search(r"[\s\x00-\x1f\x7f-\x9f]", self.evidence_ref) is not None
                or "#" in self.evidence_ref
                or not _sha(self.evidence_sha256)
            ):
                raise ValueError
            parsed = urlsplit(self.evidence_ref)
            _ = parsed.port
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError
        except (TypeError, ValueError):
            raise DatasetQualityError("quality_invalid_numbering_review") from None


@dataclass(frozen=True, slots=True)
class ReleaseSelection:
    version_id: UUID
    source_ref: str
    source_sha256: str
    input_sha256: str
    structure_sha256: str
    metadata_review_ref: str
    expected_article_count: int
    expected_chunk_count: int
    expected_instrument_id: UUID
    date_review_ref: str | None = None
    content_mode: str = "articles"
    expected_provision_count: int | None = None
    static_review_sha256: str | None = None
    numbering_review: ArticleNumberingReview | None = None

    def __post_init__(self) -> None:
        try:
            require_uuid7(self.version_id)
            require_uuid7(self.expected_instrument_id)
            if self.static_review_sha256 is not None and not _sha(self.static_review_sha256):
                raise ValueError
            if self.numbering_review is not None and not isinstance(
                self.numbering_review, ArticleNumberingReview
            ):
                raise ValueError
            if self.date_review_ref is not None and not _text(self.date_review_ref, 512):
                raise ValueError
            if not _text(self.source_ref, 4096):
                raise ValueError
            parsed = urlsplit(self.source_ref)
            if parsed.scheme != "file" or not parsed.path.startswith("/"):
                raise ValueError
            if parsed.query or parsed.fragment:
                raise ValueError
            if not all(
                _sha(value)
                for value in (self.source_sha256, self.input_sha256, self.structure_sha256)
            ) or not _text(self.metadata_review_ref, 512):
                raise ValueError
            if any(
                type(count) is not int or count <= 0
                for count in (self.expected_chunk_count,)
            ):
                raise ValueError
            if self.content_mode not in ("articles", "non_article_document"):
                raise ValueError
            if type(self.expected_article_count) is not int:
                raise ValueError
            if self.expected_provision_count is not None and (
                type(self.expected_provision_count) is not int or self.expected_provision_count <= 0
            ):
                raise ValueError
            if self.content_mode == "articles":
                if self.expected_article_count <= 0 or self.expected_provision_count not in (
                    None, self.expected_article_count,
                ):
                    raise ValueError
                if self.numbering_review is not None and self.expected_article_count != (
                    self.numbering_review.last_article - self.numbering_review.first_article + 1
                ):
                    raise ValueError
            elif self.expected_article_count != 0 or self.expected_provision_count != 1:
                raise ValueError
            elif self.numbering_review is not None:
                raise ValueError
        except (TypeError, ValueError):
            raise DatasetQualityError("quality_invalid_selection") from None


@dataclass(frozen=True, slots=True)
class ReleaseReportMember:
    report_path: str
    report_sha256: str
    selection_start: int
    selection_count: int

    def __post_init__(self) -> None:
        if (not _absolute_report_path(self.report_path) or not _sha(self.report_sha256)
                or type(self.selection_start) is not int or self.selection_start < 0
                or type(self.selection_count) is not int or self.selection_count <= 0):
            raise DatasetQualityError("quality_invalid_report_member")


def validate_release_report_members(
    value: object, *, selection_count: int
) -> tuple[ReleaseReportMember, ...]:
    """Parse serialized provenance and bind its ranges to the accepted selection."""
    if not isinstance(value, list) or not value or len(value) > 256:
        raise DatasetQualityError("quality_invalid_report_members")
    expected_keys = {"report_path", "report_sha256", "selection_start", "selection_count"}
    members: list[ReleaseReportMember] = []
    paths: set[str] = set()
    next_start = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != expected_keys:
            raise DatasetQualityError("quality_invalid_report_members")
        member = ReleaseReportMember(**item)
        path_key = ntpath.normcase(ntpath.normpath(member.report_path))
        if member.selection_start != next_start or path_key in paths:
            raise DatasetQualityError("quality_invalid_report_members")
        paths.add(path_key)
        members.append(member)
        next_start += member.selection_count
    if next_start != selection_count:
        raise DatasetQualityError("quality_invalid_report_members")
    return tuple(members)


@dataclass(frozen=True, slots=True)
class ReleaseConfiguration:
    alias: str
    model_ref: str
    dimension: int
    parser_version: str
    selection_sha256: str
    normalization: str = "l2"
    report_members: tuple[ReleaseReportMember, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.alias, str)
            or re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", self.alias) is None
            or not _text(self.model_ref, 4096)
            or type(self.dimension) is not int
            or self.dimension <= 0
            or not _text(self.parser_version, 64)
            or not _sha(self.selection_sha256)
            or self.normalization != "l2"
            or not isinstance(self.report_members, tuple)
            or any(not isinstance(member, ReleaseReportMember) for member in self.report_members)
            or any(member.selection_start != sum(
                previous.selection_count for previous in self.report_members[:index]
            ) for index, member in enumerate(self.report_members))
        ):
            raise DatasetQualityError("quality_invalid_configuration")


def release_configuration_dict(configuration: ReleaseConfiguration) -> dict[str, object]:
    """Serialize provenance while retaining the legacy single-report shape."""
    from lawyer_agent.domain.legal_release_provenance import MixedReleaseConfiguration

    if isinstance(configuration, MixedReleaseConfiguration):
        return configuration.to_dict()
    result = asdict(configuration)
    if not configuration.report_members:
        result.pop("report_members")
    else:
        result["report_members"] = [asdict(item) for item in configuration.report_members]
    return result


@dataclass(frozen=True, slots=True)
class ReleaseMetadataSummary:
    title: str
    issuing_authority: str
    category: str
    jurisdiction: str
    region_code: str | None
    version_label: str
    status: str
    published_on: str | None
    effective_on: str | None
    repealed_on: str | None


@dataclass(frozen=True, slots=True)
class ReleaseSourceSummary:
    source_ref: str
    source_sha256: str
    input_sha256: str
    structure_sha256: str
    metadata_review_ref: str
    expected_article_count: int
    expected_chunk_count: int
    date_review_ref: str | None = None
    content_mode: str = "articles"
    expected_provision_count: int | None = None
    static_review_sha256: str | None = None
    numbering_review: ArticleNumberingReview | None = None


@dataclass(frozen=True, slots=True)
class VersionQualitySummary:
    version_id: str
    blockers: tuple[str, ...]
    manual_review: tuple[str, ...]
    source_quality_flags: tuple[str, ...]
    provision_count: int
    chunk_count: int
    degraded_chunks: int
    metadata: ReleaseMetadataSummary | None
    source: ReleaseSourceSummary


@dataclass(frozen=True, slots=True)
class ReleaseQualityReport:
    passed: bool
    digest: str
    versions: tuple[VersionQualitySummary, ...]
    configuration: ReleaseConfiguration

    def to_dict(self) -> dict[str, object]:
        versions = [asdict(version) for version in self.versions]
        for version in versions:
            source = version.get("source")
            if isinstance(source, dict) and source.get("numbering_review") is None:
                source.pop("numbering_review")
        return {
            "schema_version": "release-quality-v1",
            "passed": self.passed,
            "quality_sha256": self.digest,
            "source_text_coverage": "requires_source_review",
            "configuration": release_configuration_dict(self.configuration),
            "versions": versions,
        }
