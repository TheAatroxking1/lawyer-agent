"""Verified, resumable conversion artifacts; Word execution is an injected boundary."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
import zipfile
from pathlib import Path
from typing import Literal, Protocol
from xml.parsers import expat

from pydantic import BaseModel, ConfigDict

from lawyer_agent.infrastructure.documents.conversion_paths import ConversionPaths
from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader
from lawyer_agent.infrastructure.documents.source_format import is_legacy_word_source


class WordConverter(Protocol):
    @property
    def fingerprint(self) -> str: ...

    def __call__(self, source: Path, target: Path) -> str: ...


class WordConversionError(ValueError):
    def __init__(self, code: str) -> None:
        if code not in {
            "word_conversion_failed",
            "word_ownership_unverified",
            "word_settings_restore_failed",
            "word_busy",
        }:
            raise ValueError("unknown conversion error code")
        super().__init__(code)
        self.code = code


class ConversionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["word-conversion-v1"] = "word-conversion-v1"
    source_path: str
    source_sha256: str
    status: Literal["converted", "failed"]
    converted_path: str | None = None
    converted_sha256: str | None = None
    converter_version: str | None = None
    converter_fingerprint: str | None = None
    error_code: str | None = None
    recovery_reason: Literal["office_validation_failed"] | None = None


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _validate_roots(source: Path, source_root: Path, output_root: Path) -> None:
    if source_root.is_relative_to(output_root) or output_root.is_relative_to(source_root):
        raise ValueError("source and output roots overlap")
    if (
        not source.is_relative_to(source_root)
        or source.name.startswith(("~", "."))
        or not source.is_file()
        or not is_legacy_word_source(source)
    ):
        raise ValueError("source must be a DOC file within the source root")


def _validate_docx(path: Path) -> None:
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("converted payload too large")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if len(infos) > 10_000 or sum(info.file_size for info in infos) > 64 * 1024 * 1024:
            raise ValueError("converted archive expands beyond the limit")
        if archive.getinfo("word/document.xml").file_size > 32 * 1024 * 1024:
            raise ValueError("converted document XML too large")
        _validate_word_body(archive.read("word/document.xml"))
    document = DocxExportReader().read(path)
    if not any(p.part == "word/document.xml" and p.text.strip() for p in document.paragraphs):
        raise ValueError("converted document has no readable body")


def _validate_word_body(payload: bytes) -> None:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    stack: list[str] = []
    bodies = 0
    readable = False

    def reject(*args: object) -> None:
        raise ValueError("converted XML declarations forbidden")

    def start(name: str, attributes: dict[str, str]) -> None:
        nonlocal bodies
        if not stack and name != namespace + "document":
            raise ValueError("converted Word document root invalid")
        if name == namespace + "body":
            if stack != [namespace + "document"]:
                raise ValueError("converted Word body invalid")
            bodies += 1
        stack.append(name)

    def end(name: str) -> None:
        stack.pop()

    def text(value: str) -> None:
        nonlocal readable
        if (
            value.strip()
            and len(stack) >= 4
            and stack[1] == namespace + "body"
            and stack[-1] == namespace + "t"
            and namespace + "p" in stack[2:-1]
        ):
            readable = True

    parser = expat.ParserCreate(namespace_separator="}")
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.ExternalEntityRefHandler = lambda *args: 0
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = text
    try:
        parser.Parse(payload, True)
    except expat.ExpatError as exc:
        raise ValueError("converted XML invalid") from exc
    if bodies != 1 or not readable:
        raise ValueError("converted document has no readable body")


def _write_record(path: Path, record: ConversionRecord) -> None:
    temporary = path.with_name(f".{uuid.uuid4().hex}.json")
    try:
        temporary.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _cached_record(
    path: Path, source: Path, digest: str, target: Path, fingerprint: str,
    recovery_reason: Literal["office_validation_failed"] | None,
) -> ConversionRecord | None:
    try:
        record = ConversionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if (
            record.status == "converted"
            and record.source_path == str(source)
            and record.source_sha256 == digest
            and record.converted_path == str(target)
            and target.is_file()
            and record.converted_sha256 == file_sha256(target)
            and record.converter_version
            and record.converter_fingerprint == fingerprint
            and record.recovery_reason == recovery_reason
            and (
                recovery_reason is None or record.converter_version == "binary-word-static-text-v1"
            )
            and record.error_code is None
        ):
            _validate_docx(target)
            return record
    except (OSError, ValueError, KeyError, zipfile.BadZipFile):
        return None
    return None


def convert_document(
    source: Path,
    *,
    source_root: Path,
    output_root: Path,
    converter: WordConverter,
    recovery_reason: Literal["office_validation_failed"] | None = None,
) -> ConversionRecord:
    """Convert one source without overwriting it, publishing metadata last.

    The driver receives a private temporary DOCX target and returns its version.
    It must open the input read-only and enforce macro/link/timeout controls.
    """
    if recovery_reason not in {None, "office_validation_failed"}:
        raise ValueError("invalid_recovery_reason")
    paths = ConversionPaths(source_root, output_root)
    source, source_root, output_root = paths.source(source), paths.source_root, paths.output_root
    _validate_roots(source, source_root, output_root)
    fingerprint = getattr(converter, "fingerprint", None)
    if not isinstance(fingerprint, str) or not fingerprint.strip():
        raise ValueError("converter fingerprint required")
    digest = file_sha256(source)
    identity = source.relative_to(source_root).as_posix() + "\0" + digest
    source_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    folder = paths.output(output_root / source_id)
    if not folder.is_relative_to(output_root):
        raise ValueError("converted target escapes output root")
    target, record_path = (
        paths.output(folder / "document.docx"),
        paths.output(folder / "record.json"),
    )
    if target.resolve() != target or record_path.resolve() != record_path:
        raise ValueError("converted output must not be redirected")
    cached = _cached_record(record_path, source, digest, target, fingerprint, recovery_reason)
    if cached is not None:
        return cached
    paths.output(folder).mkdir(parents=True, exist_ok=True)
    temporary = paths.output(folder / f".{uuid.uuid4().hex}.partial.docx")
    error_code = "conversion_failed"
    try:
        version = converter(source, temporary)
        if converter.fingerprint != fingerprint:
            raise ValueError("converter fingerprint changed during conversion")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("missing converter version")
        if recovery_reason is not None and version != "binary-word-static-text-v1":
            raise ValueError("recovery reason requires static text converter")
        if file_sha256(source) != digest:
            error_code = "source_changed"
            raise ValueError("source changed during conversion")
        error_code = "converted_document_invalid"
        if temporary.resolve() != temporary:
            raise ValueError("converted temporary file was redirected")
        _validate_docx(temporary)
        converted_digest = file_sha256(temporary)
        os.replace(paths.output(temporary), paths.output(target))
        result = ConversionRecord(
            source_path=str(source),
            source_sha256=digest,
            status="converted",
            converted_path=str(target),
            converted_sha256=converted_digest,
            converter_version=version,
            converter_fingerprint=fingerprint,
            recovery_reason=recovery_reason,
        )
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        if isinstance(exc, TimeoutError):
            error_code = "conversion_timeout"
        elif isinstance(exc, WordConversionError):
            error_code = exc.code
        result = ConversionRecord(
            source_path=str(source),
            source_sha256=digest,
            status="failed",
            error_code=error_code,
            recovery_reason=recovery_reason,
        )
    finally:
        paths.output(temporary).unlink(missing_ok=True)
    _write_record(paths.output(record_path), result)
    return result


def write_manifest(path: Path, records: tuple[ConversionRecord, ...]) -> None:
    """Replace a batch manifest atomically; records remain separate per source."""
    temporary = path.with_name(f".{uuid.uuid4().hex}.jsonl")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record.model_dump(), ensure_ascii=False) + "\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
