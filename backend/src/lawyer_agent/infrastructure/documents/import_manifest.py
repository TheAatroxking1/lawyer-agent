"""Read a bounded, explicitly reviewed import snapshot without reading sources."""

from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from lawyer_agent.domain.legal_corpus import LegalCategory, LegalVersionStatus
from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path

_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_MANIFEST_LINE = 1024 * 1024
_MAX_MANIFEST_ROWS = 20_000


class ImportManifestError(ValueError):
    """Stable public code, never including the untrusted row or validation payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CorpusImportRow(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", hide_input_in_errors=True)

    schema_version: Literal["legal-corpus-import-v1"]
    review_status: Literal["reviewed"]
    metadata_review_ref: Annotated[str, Field(min_length=1, max_length=512)]
    source_path: str
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    title: Annotated[str, Field(min_length=1, max_length=512)]
    issuing_authority: Annotated[str, Field(min_length=1, max_length=256)]
    jurisdiction: Annotated[str, Field(min_length=1, max_length=64)]
    category: LegalCategory = LegalCategory.UNKNOWN
    region_code: Annotated[str, Field(min_length=1, max_length=16)] | None = None
    version_label: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    status: LegalVersionStatus = LegalVersionStatus.STATUS_UNKNOWN
    published_on: date | None = None
    effective_on: date | None = None
    repealed_on: date | None = None
    law_number: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    source_ref: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    date_review_ref: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    content_mode: Literal["articles", "non_article_document"] = "articles"
    static_review_path: str | None = None
    static_review_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None

    @field_validator("*")
    @classmethod
    def nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("blank_string")
        return value

    @field_validator("published_on", "effective_on", "repealed_on", mode="before")
    @classmethod
    def iso_date(cls, value: object) -> object:
        if isinstance(value, str):
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
                raise ValueError("invalid_iso_date")
            return date.fromisoformat(value)
        return value

    @field_validator("source_path")
    @classmethod
    def absolute_source(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts or "\x00" in value:
            raise ValueError("invalid_source_path")
        # Alternate data streams and trailing Windows aliases are not source files.
        if any(":" in part or part.endswith((" ", ".")) for part in path.parts[1:]):
            raise ValueError("invalid_source_path")
        if path.name.startswith("~$") or path.suffix.lower() not in {".doc", ".docx", ".docm"}:
            raise ValueError("unsupported_source_path")
        return str(path)

    @model_validator(mode="after")
    def current_has_date(self) -> Self:
        if (self.static_review_path is None) != (self.static_review_sha256 is None):
            raise ValueError("static_review_pair_required")
        if self.date_review_ref is not None and self.status is not LegalVersionStatus.CURRENT:
            raise ValueError("date_review_requires_current")
        if self.status is LegalVersionStatus.CURRENT and (
            self.published_on is None or self.effective_on is None
        ) and self.date_review_ref is None:
            raise ValueError("current_requires_date")
        return self


@dataclass(frozen=True, slots=True)
class LoadedCorpusImportManifest:
    path: Path
    sha256: str
    rows: tuple[CorpusImportRow, ...]


def _safe_path(path: Path) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ImportManifestError("invalid_manifest_path")
    try:
        return unredirected_path(path)
    except (OSError, ValueError):
        raise ImportManifestError("unsafe_reparse_path") from None


def read_import_manifest(path: Path, source_root: Path) -> LoadedCorpusImportManifest:
    manifest = _safe_path(path)
    root = _safe_path(source_root)
    try:
        root_is_directory = root.is_dir()
    except OSError:
        raise ImportManifestError("invalid_source_root") from None
    if not root_is_directory:
        raise ImportManifestError("invalid_source_root")
    try:
        with manifest.open("rb") as stream:
            snapshot = stream.read(_MAX_MANIFEST_BYTES + 1)
    except OSError:
        raise ImportManifestError("import_manifest_read_failed") from None
    if len(snapshot) > _MAX_MANIFEST_BYTES:
        raise ImportManifestError("import_manifest_size_limit")
    try:
        snapshot.decode("utf-8")
    except UnicodeDecodeError:
        raise ImportManifestError("invalid_import_manifest_utf8") from None
    rows: list[CorpusImportRow] = []
    seen: set[str] = set()
    for line in snapshot.split(b"\n"):
        if len(line) > _MAX_MANIFEST_LINE:
            raise ImportManifestError("import_manifest_line_limit")
        if not line.strip():
            continue
        if len(rows) >= _MAX_MANIFEST_ROWS:
            raise ImportManifestError("import_manifest_row_limit")
        try:
            row = CorpusImportRow.model_validate_json(line, strict=True)
        except ValidationError:
            raise ImportManifestError("invalid_import_manifest_row") from None
        source = _safe_path(Path(row.source_path))
        if source == root or not source.is_relative_to(root):
            raise ImportManifestError("source_outside_root")
        key = ntpath.normcase(ntpath.normpath(str(source)))
        if key in seen:
            raise ImportManifestError("duplicate_import_source")
        seen.add(key)
        rows.append(row)
    if not rows:
        raise ImportManifestError("empty_import_manifest")
    return LoadedCorpusImportManifest(manifest, sha256(snapshot).hexdigest(), tuple(rows))
