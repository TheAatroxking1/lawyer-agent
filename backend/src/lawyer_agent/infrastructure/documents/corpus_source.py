"""Read-only preparation of original Word sources and verified derivatives."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import MappingProxyType

from pydantic import ValidationError

from lawyer_agent.application.conversion_provenance import ConversionProvenance
from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path
from lawyer_agent.infrastructure.documents.export_reader import (
    DocxExportReader,
    ExportParagraph,
    ExportReadError,
)
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.source_format import is_legacy_word_source
from lawyer_agent.infrastructure.documents.word_conversion import ConversionRecord, file_sha256

_MAX_MANIFEST_LINE = 1024 * 1024
_MAX_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_MANIFEST_ROWS = 20_000
_MAX_INPUT_BYTES = 64 * 1024 * 1024


class CorpusSourceError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CorpusConversionCatalog:
    manifest_path: Path
    manifest_sha256: str
    source_root: Path
    converted_root: Path
    records: Mapping[Path, tuple[ConversionRecord, Path | None, ConversionProvenance]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", MappingProxyType(dict(self.records)))


@dataclass(frozen=True, slots=True)
class CorpusSourceRequest:
    source: Path
    source_root: Path
    expected_source_sha256: str | None = None
    conversion_manifest: Path | None = None
    converted_root: Path | None = None
    conversion_catalog: CorpusConversionCatalog | None = None


@dataclass(frozen=True, slots=True)
class PreparedCorpusSource:
    source_path: Path
    input_path: Path
    source_sha256: str
    input_sha256: str
    document: ParsedDocument
    auxiliary_paragraphs: tuple[ExportParagraph, ...]
    quality_flags: tuple[str, ...]
    conversion_provenance: ConversionProvenance | None


def _path(path: Path) -> Path:
    try:
        return unredirected_path(path)
    except ValueError as exc:
        raise CorpusSourceError("unsafe_reparse_path") from exc


def _hash(value: str | None, code: str) -> str:
    if value is None or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise CorpusSourceError(code)
    return value.lower()


def _record_path(value: str, root: Path, code: str) -> Path:
    # Records use absolute paths. Relative paths would depend on the caller's cwd.
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise CorpusSourceError(code)
    candidate = _path(candidate)
    if not candidate.is_relative_to(root) or candidate == root:
        raise CorpusSourceError(code)
    return candidate


def load_conversion_catalog(
    manifest: Path, source_root: Path, converted_root: Path
) -> CorpusConversionCatalog:
    """Validate a bounded immutable manifest snapshot without reading document bodies."""
    try:
        return _load_conversion_catalog(manifest, source_root, converted_root)
    except OSError as exc:
        raise CorpusSourceError("corpus_source_io_error") from exc


def _validated_conversion_record(
    record: ConversionRecord, source_root: Path, converted_root: Path
) -> tuple[Path, ConversionRecord, Path | None, ConversionProvenance]:
    # Revalidate data rather than trusting a model instance constructed by callers.
    try:
        record = ConversionRecord.model_validate(record.model_dump(), strict=True)
        provenance = ConversionProvenance.from_record(record.model_dump())
    except (ValidationError, ValueError) as exc:
        raise CorpusSourceError("invalid_conversion_record") from exc
    record_source = _record_path(record.source_path, source_root, "conversion_source_outside_root")
    _hash(record.source_sha256, "invalid_conversion_source_sha256")
    target = None
    if record.converted_path is not None:
        target = _record_path(
            record.converted_path, converted_root, "conversion_input_outside_root"
        )
        if target.suffix.lower() != ".docx":
            raise CorpusSourceError("invalid_conversion_input_format")
    if record.status == "converted":
        if target is None or not record.converter_version or record.error_code is not None:
            raise CorpusSourceError("invalid_conversion_record")
        _hash(record.converted_sha256, "invalid_conversion_input_sha256")
        _hash(record.converter_fingerprint, "invalid_conversion_fingerprint")
    return record_source, record, target, provenance


def _load_conversion_catalog(
    manifest: Path, source_root: Path, converted_root: Path
) -> CorpusConversionCatalog:
    manifest, source_root, converted_root = (
        _path(manifest),
        _path(source_root),
        _path(converted_root),
    )
    if not source_root.is_dir():
        raise CorpusSourceError("invalid_source_root")
    if source_root.is_relative_to(converted_root) or converted_root.is_relative_to(source_root):
        raise CorpusSourceError("source_converted_roots_overlap")
    if not converted_root.is_dir():
        raise CorpusSourceError("invalid_converted_root")
    with manifest.open("rb") as file:
        snapshot = file.read(_MAX_MANIFEST_BYTES + 1)
    if len(snapshot) > _MAX_MANIFEST_BYTES:
        raise CorpusSourceError("conversion_manifest_size_limit")
    records: dict[Path, tuple[ConversionRecord, Path | None, ConversionProvenance]] = {}
    with BytesIO(snapshot) as stream:
        count = 0
        while line := stream.readline(_MAX_MANIFEST_LINE + 1):
            count += 1
            if count > _MAX_MANIFEST_ROWS:
                raise CorpusSourceError("conversion_manifest_row_limit")
            if len(line) > _MAX_MANIFEST_LINE:
                raise CorpusSourceError("conversion_manifest_line_limit")
            try:
                record = ConversionRecord.model_validate_json(line, strict=True)
            except (ValidationError, ValueError) as exc:
                raise CorpusSourceError("invalid_conversion_record") from exc
            record_source, record, target, provenance = _validated_conversion_record(
                record, source_root, converted_root
            )
            if record_source in records:
                raise CorpusSourceError("duplicate_conversion_source")
            records[record_source] = record, target, provenance
    return CorpusConversionCatalog(
        manifest, sha256(snapshot).hexdigest(), source_root, converted_root, records
    )


def _conversion(
    manifest: Path,
    source: Path,
    source_root: Path,
    converted_root: Path,
    catalog: CorpusConversionCatalog | None = None,
) -> tuple[ConversionRecord, Path, ConversionProvenance]:
    if catalog is None:
        catalog = load_conversion_catalog(manifest, source_root, converted_root)
    selected = catalog.records.get(source)
    if selected is None:
        raise CorpusSourceError("conversion_source_missing")
    record_source, record, target, provenance = _validated_conversion_record(
        selected[0], source_root, converted_root
    )
    if record_source != source:
        raise CorpusSourceError("conversion_source_mismatch")
    if record.status != "converted" or target is None:
        raise CorpusSourceError("conversion_source_failed")
    return record, target, provenance


def prepare_corpus_source(request: CorpusSourceRequest) -> PreparedCorpusSource:
    """Return text and provenance without conversion, metadata inference or writes."""
    try:
        return _prepare(request)
    except CorpusSourceError:
        raise
    except ExportReadError as exc:
        raise CorpusSourceError(str(exc)) from exc
    except OSError as exc:
        raise CorpusSourceError("corpus_source_io_error") from exc


def _prepare(request: CorpusSourceRequest) -> PreparedCorpusSource:
    source_root = _path(request.source_root)
    source = _path(request.source)
    if not source_root.is_dir():
        raise CorpusSourceError("invalid_source_root")
    if not source.is_relative_to(source_root) or source == source_root:
        raise CorpusSourceError("source_outside_root")
    if source.suffix.lower() not in {".doc", ".docx", ".docm"}:
        raise CorpusSourceError("unsupported_source_format")
    if not source.is_file():
        raise CorpusSourceError("source_not_file")
    expected = (
        _hash(request.expected_source_sha256, "invalid_source_sha256")
        if request.expected_source_sha256 is not None
        else None
    )
    if (request.conversion_manifest is None) != (request.converted_root is None):
        raise CorpusSourceError("incomplete_conversion_configuration")
    converted_root = _path(request.converted_root) if request.converted_root else None
    manifest = _path(request.conversion_manifest) if request.conversion_manifest else None
    catalog = request.conversion_catalog
    if catalog is not None and (
        catalog.manifest_path != manifest
        or catalog.source_root != source_root
        or catalog.converted_root != converted_root
    ):
        raise CorpusSourceError("conversion_catalog_configuration_mismatch")
    if converted_root is not None:
        if source_root.is_relative_to(converted_root) or converted_root.is_relative_to(source_root):
            raise CorpusSourceError("source_converted_roots_overlap")
        if not converted_root.is_dir():
            raise CorpusSourceError("invalid_converted_root")
    source_hash = file_sha256(source)
    if expected is not None and expected != source_hash:
        raise CorpusSourceError("source_sha256_mismatch")
    input_path = source
    provenance = None
    if is_legacy_word_source(source):
        if manifest is None or converted_root is None:
            raise CorpusSourceError("conversion_required")
        record, input_path, provenance = _conversion(
            manifest, source, source_root, converted_root, catalog
        )
        if record.source_sha256.lower() != source_hash:
            raise CorpusSourceError("conversion_source_sha256_mismatch")
        input_hash = file_sha256(_path(input_path))
        if input_hash != _hash(record.converted_sha256, "invalid_conversion_input_sha256"):
            raise CorpusSourceError("conversion_input_sha256_mismatch")
    else:
        input_hash = source_hash
    _path(source)
    with _path(input_path).open("rb") as stream:
        snapshot = stream.read(_MAX_INPUT_BYTES + 1)
    if len(snapshot) > _MAX_INPUT_BYTES:
        raise CorpusSourceError("corpus_input_size_limit")
    if sha256(snapshot).hexdigest() != input_hash:
        raise CorpusSourceError("input_changed_during_read")
    exported = DocxExportReader().read_bytes(snapshot, source_suffix=input_path.suffix)
    # Validate both identities again before returning any prepared text.
    if file_sha256(_path(source)) != source_hash:
        raise CorpusSourceError("source_changed_during_read")
    if file_sha256(_path(input_path)) != input_hash:
        raise CorpusSourceError("input_changed_during_read")
    paragraphs = tuple(
        ParsedParagraph(
            text=p.text,
            ordinal=p.ordinal,
            numbering_provenance=p.numbering_provenance,
        )
        for p in exported.paragraphs
        if p.location in {"body", "table"}
    )
    if not paragraphs:
        raise CorpusSourceError("no_readable_body")
    auxiliary = tuple(p for p in exported.paragraphs if p.location == "auxiliary")
    flags = set(exported.quality_flags)
    if provenance is not None:
        flags.update(provenance.quality_flags)
    return PreparedCorpusSource(
        source,
        input_path,
        source_hash,
        input_hash,
        ParsedDocument(
            paragraphs, source.as_uri(), bytes.fromhex(source_hash), exported.loader_version
        ),
        auxiliary,
        tuple(sorted(flags)),
        provenance,
    )
