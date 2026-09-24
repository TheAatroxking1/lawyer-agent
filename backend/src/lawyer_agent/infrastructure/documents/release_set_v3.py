"""Bind complete mixed-parser reports and explicit replacements to one ordered selection."""

from __future__ import annotations

import ntpath
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError, ReleaseSelection
from lawyer_agent.domain.legal_release_provenance import (
    MIXED_PARSER_VERSION,
    ReleaseEntry,
    ReleaseReplacement,
    ReleaseReportIdentity,
    ReleaseSetProvenance,
)
from lawyer_agent.infrastructure.documents import release_manifest
from lawyer_agent.infrastructure.documents.release_set_manifest import (
    ReleaseSetManifestError,
    _bind_numbering_reviews,
    _Hash,
    _Member,
    _NumberingReview,
    _safe_path,
)

_MAX_TOTAL_REPORT_BYTES = 512 * 1024 * 1024


class _MixedMember(_Member):
    dataset_version: Annotated[str, Field(min_length=1, max_length=64)]
    parser_version: Annotated[str, Field(min_length=1, max_length=64)]
    chunk_parser_version: Annotated[str, Field(min_length=1, max_length=64)]


class _ManifestV3(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal["legal-corpus-release-set-v3"]
    total: Annotated[int, Field(gt=0, le=20_000)]
    selection_sha256: _Hash
    reports: Annotated[list[_MixedMember], Field(min_length=1, max_length=256)]
    replacements: Annotated[list[dict[str, object]], Field(max_length=256)]
    numbering_reviews: Annotated[list[_NumberingReview], Field(max_length=256)]


@dataclass(frozen=True, slots=True)
class LoadedMixedReleaseSet:
    path: Path
    sha256: str
    selections: tuple[ReleaseSelection, ...]
    provenance: ReleaseSetProvenance
    dataset_version: str
    parser_version: str = MIXED_PARSER_VERSION
    chunk_parser_version: str = MIXED_PARSER_VERSION


def _identity(member: _MixedMember, path: Path) -> ReleaseReportIdentity:
    return ReleaseReportIdentity(
        str(path), member.sha256, member.selection_count, member.dataset_version,
        member.parser_version, member.chunk_parser_version,
    )


def load_release_set_v3(path: Path, raw: bytes, payload: object) -> LoadedMixedReleaseSet:
    """Read report bytes once, preserving their header and every actual row binding."""
    try:
        return _load(path, raw, _ManifestV3.model_validate(payload))
    except ReleaseSetManifestError:
        raise
    except (DatasetQualityError, ValidationError, TypeError, ValueError, RecursionError):
        raise ReleaseSetManifestError("release_set_invalid_provenance") from None


def _load(path: Path, raw: bytes, manifest: _ManifestV3) -> LoadedMixedReleaseSet:
    if sum(member.selection_count for member in manifest.reports) > 40_000:
        raise ReleaseSetManifestError("release_set_selection_limit")
    identities: list[ReleaseReportIdentity] = []
    all_entries: list[ReleaseEntry] = []
    report_paths: set[str] = set()
    report_hashes: set[str] = set()
    total_bytes = 0
    for member in manifest.reports:
        report_path = _safe_path(member.path)
        key = ntpath.normcase(ntpath.normpath(str(report_path)))
        if key in report_paths or member.sha256 in report_hashes:
            raise ReleaseSetManifestError("release_set_duplicate_report")
        report_paths.add(key)
        report_hashes.add(member.sha256)
        identity = _identity(member, report_path)
        if identities and identity.dataset_version != identities[0].dataset_version:
            raise ReleaseSetManifestError("release_set_version_mismatch")
        remaining = _MAX_TOTAL_REPORT_BYTES - total_bytes
        try:
            with report_path.open("rb") as stream:
                snapshot = stream.read(min(remaining, release_manifest._MAX_REPORT_BYTES) + 1)
        except OSError:
            raise ReleaseSetManifestError("release_set_invalid_member") from None
        total_bytes += len(snapshot)
        if total_bytes > _MAX_TOTAL_REPORT_BYTES:
            raise ReleaseSetManifestError("release_set_total_report_size_limit")
        try:
            loaded = release_manifest.read_release_manifest_snapshot(report_path, snapshot)
        except release_manifest.ReleaseManifestError:
            raise ReleaseSetManifestError("release_set_invalid_member") from None
        if loaded.sha256 != identity.report_sha256:
            raise ReleaseSetManifestError("release_set_member_hash_mismatch")
        if len(loaded.selections) != identity.selection_count:
            raise ReleaseSetManifestError("release_set_member_count_mismatch")
        if (loaded.dataset_version, loaded.parser_version, loaded.chunk_parser_version) != (
            identity.dataset_version, identity.parser_version, identity.chunk_parser_version
        ):
            raise ReleaseSetManifestError("release_set_version_mismatch")
        identities.append(identity)
        all_entries.extend(ReleaseEntry(loaded.sha256, row, item)
                           for row, item in enumerate(loaded.selections, 1))

    by_row = {(item.report_sha256, item.row): item for item in all_entries}
    replacements = tuple(ReleaseReplacement.from_dict(item) for item in manifest.replacements)
    excluded: set[tuple[str, int]] = set()
    for replacement in replacements:
        old, new = replacement.old, replacement.new
        old_key, new_key = (old.report_sha256, old.row), (new.report_sha256, new.row)
        # Inline new reviews are checked only after evidence has been bound below.
        unreviewed_new = replace(new, selection=replace(new.selection, numbering_review=None))
        if (old_key in excluded or by_row.get(old_key) != old
                or by_row.get(new_key) != unreviewed_new):
            raise ReleaseSetManifestError("release_set_replacement_binding_mismatch")
        excluded.add(old_key)
    entries = [item for item in all_entries if (item.report_sha256, item.row) not in excluded]
    if len(entries) != manifest.total:
        raise ReleaseSetManifestError("release_set_total_mismatch")
    selections = _bind_numbering_reviews(
        [item.selection for item in entries], manifest.numbering_reviews
    )
    reviewed_entries = tuple(replace(item, selection=selection)
                             for item, selection in zip(entries, selections, strict=True))
    # Provenance validates final new-entry equality, uniqueness, canonical order, complete
    # report coverage, replacement topology, and the independently declared selection digest.
    manifest_sha = sha256(raw).hexdigest()
    provenance = ReleaseSetProvenance(
        manifest_sha, manifest.selection_sha256, tuple(identities), reviewed_entries, replacements
    )
    return LoadedMixedReleaseSet(path, manifest_sha, tuple(selections), provenance,
                                 identities[0].dataset_version)
