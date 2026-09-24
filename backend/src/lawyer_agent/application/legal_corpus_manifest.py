"""Local metadata candidates from an export manifest; never confirmed legal metadata."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from lawyer_agent.domain.legal_corpus import LegalCategory
from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path

_DATE_SUFFIX = re.compile(r"_(\d{8})$", re.ASCII)
_MAX_ROW_CHARS = 1024 * 1024
_CATEGORY_BY_DIRECTORY = {
    "宪法": LegalCategory.CONSTITUTION,
    "法律": LegalCategory.LAW,
    "行政法规": LegalCategory.ADMINISTRATIVE_REGULATION,
    "司法解释": LegalCategory.JUDICIAL_INTERPRETATION,
    "地方法规": LegalCategory.LOCAL_REGULATION,
    "监察法规": LegalCategory.SUPERVISORY_REGULATION,
}
ExportStatus = Literal["completed", "pending_conversion", "failed"]
Sha256Text = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CorpusManifestError(ValueError):
    """Stable failure preparing a candidate manifest."""


class _ExportRow(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    schema_version: Literal["legal-corpus-export-v2"]
    id_namespace: Literal["offline-export-v1"]
    document_id: str
    source_path: str
    source_relative_path: str
    source_sha256: Sha256Text | None
    status: ExportStatus
    code: str
    output_directory: str | None = None
    quality_flags: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_hash_for_read_sources(self) -> _ExportRow:
        if self.status != "failed" and self.source_sha256 is None:
            raise ValueError("source_hash_missing")
        return self


class CorpusMetadataCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["legal-corpus-metadata-candidate-v1"] = (
        "legal-corpus-metadata-candidate-v1"
    )
    source_path: str
    source_relative_path: str
    source_filename: str
    source_sha256: Sha256Text | None
    source_manifest_path: str
    export_status: ExportStatus
    export_code: str
    export_quality_flags: tuple[str, ...]
    title_candidate: str
    category_candidate: LegalCategory
    issuing_authority_candidate: str | None
    filename_date_candidate: date | None
    published_on: None = None
    effective_on: None = None
    region_code: None = None
    status: Literal["status_unknown"] = "status_unknown"
    review_status: Literal["pending"] = "pending"
    hash_verification: Literal["pending"] = "pending"
    fields_to_review: tuple[str, ...] = (
        "source_sha256", "title", "category", "issuing_authority",
        "published_on", "effective_on", "status", "region_code",
    )
    issues: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorpusManifestSummary:
    manifest_sources: int
    selected_sources: int
    incomplete_sources: int
    output_path: str


def _source_path(row: _ExportRow, root: Path) -> Path:
    raw = Path(row.source_path)
    if not raw.is_absolute():
        raise CorpusManifestError("source_path_must_be_absolute")
    source = unredirected_path(raw)
    if not source.is_relative_to(root):
        raise CorpusManifestError("source_path_outside_root")
    if not source.is_file() or source.name.startswith("~$"):
        raise CorpusManifestError("source_file_missing_or_unsupported")
    if source.suffix.lower() not in {".doc", ".docx", ".docm"}:
        raise CorpusManifestError("source_file_missing_or_unsupported")
    if source.relative_to(root).as_posix() != row.source_relative_path:
        raise CorpusManifestError("source_relative_path_mismatch")
    return source


def _read_rows(manifest: Path, root: Path) -> list[_ExportRow]:
    rows: list[_ExportRow] = []
    seen: set[str] = set()
    with manifest.open(encoding="utf-8") as stream:
        while line := stream.readline(_MAX_ROW_CHARS + 1):
            if len(line) > _MAX_ROW_CHARS:
                raise CorpusManifestError("manifest_row_size_limit")
            if not line.strip():
                continue
            try:
                row = _ExportRow.model_validate_json(line)
            except ValidationError as exc:
                raise CorpusManifestError("invalid_manifest_row") from exc
            source = _source_path(row, root)
            key = os.path.normcase(str(source))
            if key in seen:
                raise CorpusManifestError("duplicate_source")
            seen.add(key)
            rows.append(row)
    if not rows:
        raise CorpusManifestError("manifest_empty")
    return sorted(rows, key=lambda row: row.source_relative_path)


def _candidate(row: _ExportRow, manifest: Path) -> CorpusMetadataCandidate:
    source = Path(row.source_path)
    title = source.stem
    issues: list[str] = []
    filename_date: date | None = None
    match = _DATE_SUFFIX.search(title)
    if match:
        raw_date = match.group(1)
        title = title[:match.start()]
        try:
            filename_date = date(int(raw_date[:4]), int(raw_date[4:6]), int(raw_date[6:]))
        except ValueError:
            issues.append("invalid_filename_date")
    else:
        issues.append("filename_date_missing")
    title = title.replace("+", "、").strip()
    if not title:
        issues.append("title_candidate_empty")
    category = _CATEGORY_BY_DIRECTORY.get(
        row.source_relative_path.split("/", 1)[0], LegalCategory.UNKNOWN,
    )
    if category is LegalCategory.UNKNOWN:
        issues.append("category_unrecognized")
    authorities = [name for name in ("最高人民法院", "最高人民检察院") if name in title]
    authority = "、".join(authorities) or None
    if authority is None:
        issues.append("issuing_authority_candidate_missing")
    if row.status != "completed":
        issues.append("source_not_completed")
    if row.source_sha256 is None:
        issues.append("source_hash_missing")
    issues.extend("export_quality:" + flag for flag in row.quality_flags)
    return CorpusMetadataCandidate(
        source_path=str(source), source_relative_path=row.source_relative_path,
        source_filename=source.name, source_sha256=row.source_sha256,
        source_manifest_path=str(manifest), export_status=row.status, export_code=row.code,
        export_quality_flags=row.quality_flags, title_candidate=title,
        category_candidate=category, issuing_authority_candidate=authority,
        filename_date_candidate=filename_date, issues=tuple(issues),
    )


def _check_output(output: Path, root: Path, manifest: Path) -> None:
    unredirected_path(root)
    unredirected_path(manifest)
    unredirected_path(output)
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise CorpusManifestError("source_output_overlap")
    if output == manifest or (output.exists() and output.samefile(manifest)):
        raise CorpusManifestError("output_overlaps_input_manifest")
    if output.exists() and not output.is_file():
        raise CorpusManifestError("output_must_be_file")


def prepare_corpus_manifest(
    *, source_root: Path, manifest_path: Path, output_path: Path, limit: int | None = None,
) -> CorpusManifestSummary:
    """Validate all input rows before selecting a limit; read no source bodies."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise CorpusManifestError("limit_must_be_positive")
    root = unredirected_path(source_root)
    manifest = unredirected_path(manifest_path)
    output = unredirected_path(output_path)
    if not root.is_dir() or not manifest.is_file():
        raise CorpusManifestError("source_root_or_manifest_missing")
    _check_output(output, root, manifest)
    rows = _read_rows(manifest, root)
    if output.exists() and any(output.samefile(Path(row.source_path)) for row in rows):
        raise CorpusManifestError("output_overlaps_source_file")
    candidates = tuple(_candidate(row, manifest) for row in rows[:limit])
    output.parent.mkdir(parents=True, exist_ok=True)
    _check_output(output, root, manifest)
    descriptor, name = tempfile.mkstemp(
        prefix=".corpus-candidates-", suffix=".tmp", dir=output.parent,
    )
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            for candidate in candidates:
                stream.write(candidate.model_dump_json() + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        _check_output(output, root, manifest)
        unredirected_path(temporary)
        os.replace(temporary, output)
    finally:
        unredirected_path(temporary).unlink(missing_ok=True)
    return CorpusManifestSummary(
        manifest_sources=len(rows), selected_sources=len(candidates),
        incomplete_sources=sum(row.export_status != "completed" for row in candidates),
        output_path=str(output),
    )
