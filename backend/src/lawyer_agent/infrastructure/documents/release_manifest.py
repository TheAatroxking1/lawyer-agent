"""Validate one bounded batch-result snapshot without touching its listed sources."""

from __future__ import annotations

import json
import ntpath
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from lawyer_agent.domain.common import require_uuid7
from lawyer_agent.domain.legal_dataset_quality import DatasetQualityError, ReleaseSelection
from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path

_MAX_REPORT_BYTES = 64 * 1024 * 1024
_MAX_REPORT_LINE = 1024 * 1024
_MAX_REPORT_ROWS = 20_000
_Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$", min_length=64, max_length=64)]
_Count = Annotated[int, Field(ge=0)]


class ReleaseManifestError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class LoadedReleaseManifest:
    path: Path
    sha256: str
    selections: tuple[ReleaseSelection, ...]
    parser_version: str
    dataset_version: str
    chunk_parser_version: str | None = None


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)

    @field_validator("*")
    @classmethod
    def nonblank(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("blank")
        return value


class _Run(_Record):
    event: Literal["run"]
    schema_version: Literal["corpus-import-batch-v1"]
    run_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$", min_length=32, max_length=32)]
    manifest_sha256: _Hash
    conversion_manifest_sha256: _Hash | None
    total: Annotated[int, Field(gt=0)]
    mode: Literal["import"]
    dataset_version: Annotated[str, Field(min_length=1, max_length=64)]
    parser_version: Annotated[str, Field(min_length=1, max_length=64)]
    chunk_parser_version: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class _File(_Record):
    event: Literal["file"]
    row: Annotated[int, Field(gt=0)]
    source_path: Annotated[str, Field(min_length=1, max_length=4096)]
    source_sha256: _Hash
    input_sha256: _Hash
    structure_sha256: _Hash
    source_hash_verified: bool
    metadata_review_ref: Annotated[str, Field(min_length=1, max_length=512)]
    date_review_ref: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    content_mode: Literal["articles", "non_article_document"] = "articles"
    provision_count: Annotated[int, Field(gt=0)] | None = None
    static_review_sha256: _Hash | None = None
    quality_flags: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=512)]], Field(max_length=64),
    ]
    status: Literal["imported", "replayed"]
    code: Literal["import_completed"]
    version_id: UUID
    instrument_id: UUID
    article_count: Annotated[int, Field(ge=0)]
    chunk_count: Annotated[int, Field(gt=0)]

    @field_validator("version_id", "instrument_id", mode="before")
    @classmethod
    def uuid7(cls, value: object) -> UUID:
        if not isinstance(value, str):
            raise ValueError("invalid_uuid")
        parsed = UUID(value)
        require_uuid7(parsed)
        return parsed

    @field_validator("source_hash_verified")
    @classmethod
    def verified(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("unverified")
        return value

    @field_validator("quality_flags")
    @classmethod
    def flags_nonblank(cls, value: list[str]) -> list[str]:
        if any(not flag.strip() for flag in value):
            raise ValueError("blank_flag")
        return value


class _Summary(_Record):
    event: Literal["summary"]
    complete: bool
    total: _Count
    processed: _Count
    imported: _Count
    replayed: _Count
    preflighted: _Count
    failed: _Count
    report: Annotated[str, Field(min_length=1, max_length=4096)]


def _lexical_path(value: str) -> Path:
    """Pure syntax check; never resolve/stat a listed source or old report path."""
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts
            or any(ord(char) < 32 for char in value)
            or value.startswith(("\\\\?\\", "\\\\.\\"))):
        raise ReleaseManifestError("release_manifest_unsafe_path")
    devices = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    devices.update(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10))
    for part in path.parts:
        if part == path.anchor:
            continue
        if (":" in part or part.endswith((" ", "."))
                or part.split(".", 1)[0].upper() in devices):
            raise ReleaseManifestError("release_manifest_unsafe_path")
    return path


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseManifestError("release_manifest_duplicate_key")
        result[key] = value
    return result


def _record(line: bytes) -> dict[str, Any]:
    if len(line) > _MAX_REPORT_LINE:
        raise ReleaseManifestError("release_manifest_line_limit")
    try:
        value = json.loads(line, object_pairs_hook=_unique_object)
        if not isinstance(value, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise ReleaseManifestError("release_manifest_invalid_json") from None
    return value


def read_release_manifest(path: Path) -> LoadedReleaseManifest:
    try:
        manifest = unredirected_path(_lexical_path(str(path)))
        with manifest.open("rb") as stream:
            snapshot = stream.read(_MAX_REPORT_BYTES + 1)
    except ReleaseManifestError:
        raise
    except (OSError, ValueError):
        raise ReleaseManifestError("release_manifest_read_failed") from None
    return read_release_manifest_snapshot(manifest, snapshot)


def read_release_manifest_snapshot(manifest: Path, snapshot: bytes) -> LoadedReleaseManifest:
    """Validate already bounded immutable report bytes using the ordinary reader contract."""
    if len(snapshot) > _MAX_REPORT_BYTES:
        raise ReleaseManifestError("release_manifest_size_limit")
    try:
        snapshot.decode("utf-8")
    except UnicodeDecodeError:
        raise ReleaseManifestError("release_manifest_invalid_utf8") from None
    # Bound list allocation too: a small report full of newlines must not create millions of rows.
    lines = snapshot.split(b"\n", _MAX_REPORT_ROWS + 2)
    if lines and lines[-1] == b"":
        lines.pop()
    if len(lines) < 3:
        raise ReleaseManifestError("release_manifest_incomplete")
    if len(lines) > _MAX_REPORT_ROWS + 2:
        raise ReleaseManifestError("release_manifest_row_limit")
    try:
        return _load(manifest, snapshot, lines)
    except ReleaseManifestError:
        raise
    except (ValidationError, ValueError, TypeError, KeyError, UnicodeError):
        raise ReleaseManifestError("release_manifest_invalid") from None


def _load(manifest: Path, snapshot: bytes, lines: list[bytes]) -> LoadedReleaseManifest:
    header = _Run.model_validate(_record(lines[0]))
    summary = _Summary.model_validate(_record(lines[-1]))
    _lexical_path(summary.report)
    if len(f"{header.parser_version}/hierarchical-v1") > 64:
        raise ReleaseManifestError("release_manifest_invalid_parser")
    chunk_parser_version = header.chunk_parser_version or f"{header.parser_version}/hierarchical-v1"
    if chunk_parser_version not in (
        f"{header.parser_version}/hierarchical-v1", f"{header.parser_version}/hierarchical-v2",
    ):
        raise ReleaseManifestError("release_manifest_invalid_parser")
    if (header.total != len(lines) - 2 or header.total > _MAX_REPORT_ROWS
            or not summary.complete or summary.total != header.total
            or summary.processed != header.total or summary.preflighted != 0
            or summary.failed != 0):
        raise ReleaseManifestError("release_manifest_incomplete")
    selections: list[ReleaseSelection] = []
    sources: set[str] = set()
    versions: set[UUID] = set()
    imported = replayed = 0
    for number, line in enumerate(lines[1:-1], 1):
        payload = _record(line)
        if "metadata_review_ref" not in payload:
            raise ReleaseManifestError("release_metadata_review_missing")
        entry = _File.model_validate(payload)
        if entry.row != number:
            raise ReleaseManifestError("release_manifest_row_order")
        source = _lexical_path(entry.source_path)
        if (source.name.startswith("~$")
                or source.suffix.lower() not in {".doc", ".docx", ".docm"}):
            raise ReleaseManifestError("release_manifest_unsafe_path")
        source_key = ntpath.normcase(ntpath.normpath(str(source)))
        if entry.version_id in versions or source_key in sources:
            raise ReleaseManifestError("release_manifest_duplicate_selection")
        versions.add(entry.version_id)
        sources.add(source_key)
        imported += entry.status == "imported"
        replayed += entry.status == "replayed"
        if ((entry.content_mode == "non_article_document")
                != ("non_article_document" in entry.quality_flags)):
            raise ReleaseManifestError("release_manifest_invalid_record")
        try:
            selection = _selection(entry, source)
        except DatasetQualityError:
            raise ReleaseManifestError("release_manifest_invalid_record") from None
        selections.append(selection)
    if imported != summary.imported or replayed != summary.replayed:
        raise ReleaseManifestError("release_manifest_count_mismatch")
    return LoadedReleaseManifest(
        manifest, sha256(snapshot).hexdigest(), tuple(selections),
        header.parser_version, header.dataset_version,
        chunk_parser_version,
    )


def _selection(entry: _File, source: Path) -> ReleaseSelection:
    return ReleaseSelection(
            version_id=entry.version_id, source_ref=source.as_uri(),
            source_sha256=entry.source_sha256, input_sha256=entry.input_sha256,
            structure_sha256=entry.structure_sha256, metadata_review_ref=entry.metadata_review_ref,
            date_review_ref=entry.date_review_ref,
            expected_article_count=entry.article_count, expected_chunk_count=entry.chunk_count,
            expected_instrument_id=entry.instrument_id,
            content_mode=entry.content_mode, expected_provision_count=entry.provision_count,
            static_review_sha256=entry.static_review_sha256,
    )
