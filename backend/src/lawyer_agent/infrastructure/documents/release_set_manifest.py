"""Read a bounded ordered set of immutable complete import reports."""

from __future__ import annotations

import json
import ntpath
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lawyer_agent.domain.legal_dataset_quality import (
    ArticleNumberingReview,
    DatasetQualityError,
    ReleaseReportMember,
    ReleaseSelection,
)
from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path
from lawyer_agent.infrastructure.documents.release_manifest import (
    ReleaseManifestError,
    read_release_manifest,
)

if TYPE_CHECKING:
    from lawyer_agent.infrastructure.documents.release_set_v3 import LoadedMixedReleaseSet

_MAX_SET_BYTES = 1024 * 1024
_MAX_V3_SET_BYTES = 64 * 1024 * 1024
_MAX_MEMBERS = 256
_MAX_SELECTIONS = 20_000
_MAX_NUMBERING_REVIEWS = 256
_MAX_EVIDENCE_BYTES = 4 * 1024 * 1024
_MAX_TOTAL_EVIDENCE_BYTES = 16 * 1024 * 1024
_Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]


class ReleaseSetManifestError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _Member(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    sha256: _Hash
    selection_count: Annotated[int, Field(gt=0)]


class _ManifestV1(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["legal-corpus-release-set-v1"]
    total: Annotated[int, Field(gt=0, le=_MAX_SELECTIONS)]
    reports: Annotated[list[_Member], Field(min_length=1, max_length=_MAX_MEMBERS)]


class _NumberingReview(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    version_id: Annotated[str, Field(min_length=1, max_length=64)]
    source_sha256: _Hash
    input_sha256: _Hash
    structure_sha256: _Hash
    first_article: Annotated[int, Field(ge=2, le=9999)]
    last_article: Annotated[int, Field(ge=2, le=9999)]
    review_ref: Annotated[str, Field(min_length=1, max_length=512)]
    evidence_ref: Annotated[str, Field(min_length=1, max_length=4096)]
    evidence_path: Annotated[str, Field(min_length=1, max_length=4096)]
    evidence_sha256: _Hash


class _ManifestV2(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["legal-corpus-release-set-v2"]
    total: Annotated[int, Field(gt=0, le=_MAX_SELECTIONS)]
    reports: Annotated[list[_Member], Field(min_length=1, max_length=_MAX_MEMBERS)]
    numbering_reviews: Annotated[
        list[_NumberingReview], Field(min_length=1, max_length=_MAX_NUMBERING_REVIEWS)
    ]


@dataclass(frozen=True, slots=True)
class LoadedReleaseSetManifest:
    path: Path
    sha256: str
    selections: tuple[ReleaseSelection, ...]
    members: tuple[ReleaseReportMember, ...]
    parser_version: str
    dataset_version: str
    chunk_parser_version: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseSetManifestError("release_set_duplicate_key")
        result[key] = value
    return result


def _safe_path(value: str) -> Path:
    path = Path(value)
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
        f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
    }
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(ord(c) < 32 for c in value)
        or value.startswith(("\\\\?\\", "\\\\.\\"))
    ):
        raise ReleaseSetManifestError("release_set_unsafe_path")
    for part in path.parts:
        if part != path.anchor and (
            ":" in part or part.endswith((" ", ".")) or part.split(".", 1)[0].upper() in devices
        ):
            raise ReleaseSetManifestError("release_set_unsafe_path")
    try:
        return unredirected_path(path)
    except (OSError, ValueError):
        raise ReleaseSetManifestError("release_set_unsafe_path") from None


def _source_key(selection: ReleaseSelection) -> str:
    parsed = urlsplit(selection.source_ref)
    value = f"//{parsed.netloc}{parsed.path}" if parsed.netloc else parsed.path.lstrip("/")
    return ntpath.normcase(ntpath.normpath(unquote(value)))


def read_release_set_manifest(path: Path) -> LoadedReleaseSetManifest | LoadedMixedReleaseSet:
    manifest_path = _safe_path(str(path))
    try:
        with manifest_path.open("rb") as stream:
            raw = stream.read(_MAX_V3_SET_BYTES + 1)
    except OSError:
        raise ReleaseSetManifestError("release_set_read_failed") from None
    if len(raw) > _MAX_V3_SET_BYTES:
        raise ReleaseSetManifestError("release_set_size_limit")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        if isinstance(payload, dict) and payload.get("schema_version") == (
            "legal-corpus-release-set-v3"
        ):
            from lawyer_agent.infrastructure.documents.release_set_v3 import load_release_set_v3

            return load_release_set_v3(manifest_path, raw, payload)
        if len(raw) > _MAX_SET_BYTES:
            raise ReleaseSetManifestError("release_set_size_limit")
        if isinstance(payload, dict) and payload.get("schema_version") == (
            "legal-corpus-release-set-v1"
        ):
            manifest: _ManifestV1 | _ManifestV2 = _ManifestV1.model_validate(payload)
        else:
            manifest = _ManifestV2.model_validate(payload)
    except ReleaseSetManifestError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        TypeError,
        ValueError,
        RecursionError,
    ):
        if len(raw) > _MAX_SET_BYTES:
            raise ReleaseSetManifestError("release_set_size_limit") from None
        raise ReleaseSetManifestError("release_set_invalid") from None
    if len(manifest.reports) > _MAX_MEMBERS:
        raise ReleaseSetManifestError("release_set_member_limit")
    if manifest.total > _MAX_SELECTIONS:
        raise ReleaseSetManifestError("release_set_selection_limit")

    selections: list[ReleaseSelection] = []
    members: list[ReleaseReportMember] = []
    report_paths: set[str] = set()
    sources: set[str] = set()
    versions: set[object] = set()
    identity: tuple[str, str, str] | None = None
    for declared in manifest.reports:
        report_path = _safe_path(declared.path)
        report_key = ntpath.normcase(ntpath.normpath(str(report_path)))
        if report_key in report_paths:
            raise ReleaseSetManifestError("release_set_duplicate_report")
        report_paths.add(report_key)
        try:
            loaded = read_release_manifest(report_path)
        except ReleaseManifestError:
            raise ReleaseSetManifestError("release_set_invalid_member") from None
        if loaded.sha256 != declared.sha256:
            raise ReleaseSetManifestError("release_set_member_hash_mismatch")
        if len(loaded.selections) != declared.selection_count:
            raise ReleaseSetManifestError("release_set_member_count_mismatch")
        member_identity = (
            loaded.dataset_version,
            loaded.parser_version,
            loaded.chunk_parser_version or f"{loaded.parser_version}/hierarchical-v1",
        )
        if identity is None:
            identity = member_identity
        elif identity != member_identity:
            raise ReleaseSetManifestError("release_set_version_mismatch")
        start = len(selections)
        for selection in loaded.selections:
            source_key = _source_key(selection)
            if source_key in sources or selection.version_id in versions:
                raise ReleaseSetManifestError("release_set_duplicate_selection")
            sources.add(source_key)
            versions.add(selection.version_id)
            selections.append(selection)
            if len(selections) > _MAX_SELECTIONS:
                raise ReleaseSetManifestError("release_set_selection_limit")
        members.append(
            ReleaseReportMember(str(report_path), loaded.sha256, start, len(loaded.selections))
        )
    if identity is None or manifest.total != len(selections):
        raise ReleaseSetManifestError("release_set_total_mismatch")
    if isinstance(manifest, _ManifestV2):
        selections = _bind_numbering_reviews(selections, manifest.numbering_reviews)
    return LoadedReleaseSetManifest(
        manifest_path,
        sha256(raw).hexdigest(),
        tuple(selections),
        tuple(members),
        identity[1],
        identity[0],
        identity[2],
    )


def _bind_numbering_reviews(
    selections: list[ReleaseSelection], reviews: list[_NumberingReview]
) -> list[ReleaseSelection]:
    by_version = {
        str(selection.version_id): (index, selection)
        for index, selection in enumerate(selections)
    }
    seen: set[str] = set()
    evidence_cache: dict[str, tuple[int, str]] = {}
    total_evidence_bytes = 0
    for declared in reviews:
        if declared.version_id in seen:
            raise ReleaseSetManifestError("release_set_duplicate_numbering_review")
        seen.add(declared.version_id)
        matched = by_version.get(declared.version_id)
        if matched is None:
            raise ReleaseSetManifestError("release_set_unknown_numbering_version")
        index, selection = matched
        if (
            selection.source_sha256 != declared.source_sha256
            or selection.input_sha256 != declared.input_sha256
            or selection.structure_sha256 != declared.structure_sha256
        ):
            raise ReleaseSetManifestError("release_set_numbering_binding_mismatch")
        if selection.content_mode != "articles":
            raise ReleaseSetManifestError("release_set_numbering_mode_mismatch")
        if selection.expected_article_count != declared.last_article - declared.first_article + 1:
            raise ReleaseSetManifestError("release_set_numbering_count_mismatch")
        evidence_path = _safe_path(declared.evidence_path)
        evidence_key = ntpath.normcase(ntpath.normpath(str(evidence_path)))
        cached = evidence_cache.get(evidence_key)
        if cached is None:
            try:
                if not evidence_path.is_file():
                    raise OSError
                with evidence_path.open("rb") as stream:
                    evidence_raw = stream.read(_MAX_EVIDENCE_BYTES + 1)
            except OSError:
                raise ReleaseSetManifestError("release_set_evidence_read_failed") from None
            if len(evidence_raw) > _MAX_EVIDENCE_BYTES:
                raise ReleaseSetManifestError("release_set_evidence_size_limit")
            cached = (len(evidence_raw), sha256(evidence_raw).hexdigest())
            evidence_cache[evidence_key] = cached
            total_evidence_bytes += cached[0]
            if total_evidence_bytes > _MAX_TOTAL_EVIDENCE_BYTES:
                raise ReleaseSetManifestError("release_set_total_evidence_size_limit")
        if cached[1] != declared.evidence_sha256:
            raise ReleaseSetManifestError("release_set_evidence_hash_mismatch")
        try:
            review = ArticleNumberingReview(
                declared.first_article,
                declared.last_article,
                declared.review_ref,
                declared.evidence_ref,
                declared.evidence_sha256,
            )
            selections[index] = replace(selection, numbering_review=review)
        except DatasetQualityError:
            raise ReleaseSetManifestError("release_set_invalid_numbering_review") from None
    return selections
