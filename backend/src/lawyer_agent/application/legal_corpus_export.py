"""Verified, resumable offline export of legal-corpus Word documents."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from lawyer_agent.application.conversion_provenance import (
    CONVERSION_QUALITY_FLAGS,
    ConversionProvenance,
)
from lawyer_agent.application.legal_chunk_structure import (
    derive_hierarchical_chunks_with_locations,
)
from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser, ParsedArticle
from lawyer_agent.infrastructure.documents.source_format import is_legacy_word_source

ID_NAMESPACE = "offline-export-v1"
EXPORT_VERSION = "legal-corpus-export-v2"
PARSER_VERSION = "legal-structure-parser-v1"
LOADER_VERSION = "docx-export-reader-v2"
NUMBERING_LOADER_VERSION = f"{LOADER_VERSION}:numbering-v1"


class ExportError(ValueError):
    """Stable export configuration or conversion-manifest failure."""


@dataclass(frozen=True, slots=True)
class ExportConfig:
    source_root: Path
    output_root: Path
    limit: int | None = None
    conversion_manifest: Path | None = None
    converted_root: Path | None = None
    max_leaf_chars: int = 600
    window_chars: int = 400
    overlap_chars: int = 60
    docx_only: bool = False


@dataclass(frozen=True, slots=True)
class ExportSummary:
    total: int
    completed: int
    pending_conversion: int
    failed: int
    structure_review_required: int


@dataclass(frozen=True, slots=True)
class _Provision:
    id: UUID
    full_text: str
    char_start: int


def export_corpus(config: ExportConfig) -> ExportSummary:
    _reject_reparse_path(config.source_root, "unsafe_source_root")
    _reject_reparse_path(config.output_root, "unsafe_output_path")
    source_root = config.source_root.resolve(strict=True)
    output_root = config.output_root.resolve(strict=False)
    _reject_overlap(source_root, output_root)
    if config.limit is not None and (isinstance(config.limit, bool) or config.limit <= 0):
        raise ExportError("limit_must_be_positive")
    conversions = _load_conversions(config, source_root)
    sources = _enumerate_sources(source_root, docx_only=config.docx_only)
    if config.limit is not None:
        sources = sources[: config.limit]
    output_root.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    counts = {"completed": 0, "pending_conversion": 0, "failed": 0}
    review_count = 0
    for source in sources:
        relative = source.relative_to(source_root).as_posix()
        base: dict[str, Any] = {
            "schema_version": EXPORT_VERSION,
            "id_namespace": ID_NAMESPACE,
            "document_id": _stable_id("document-path", relative),
            "source_path": str(source),
            "source_relative_path": relative,
            "source_sha256": None,
        }
        try:
            source_hash = _file_hash(source)
            base["source_sha256"] = source_hash
            base["document_id"] = _stable_id("document", relative, source_hash)
            read_path = source
            converted_hash: str | None = None
            provenance: ConversionProvenance | None = None
            if is_legacy_word_source(source):
                conversion = conversions.get(str(source))
                if conversion is None:
                    base.update(status="pending_conversion", code="pending_conversion")
                    manifest.append(base)
                    counts["pending_conversion"] += 1
                    continue
                read_path, expected_source_hash, converted_hash, provenance = conversion
                if source_hash != expected_source_hash or _file_hash(read_path) != converted_hash:
                    raise ExportError("conversion_hash_mismatch")
            directory_name = sha256(f"{relative}\0{source_hash}".encode()).hexdigest()[:24]
            directory = output_root / directory_name
            _assert_safe_output_target(directory, output_root)
            document = _export_one(
                source=source,
                read_path=read_path,
                source_hash=source_hash,
                converted_hash=converted_hash,
                provenance=provenance,
                relative=relative,
                directory=directory,
                config=config,
            )
            if _file_hash(source) != source_hash:
                raise ExportError("source_changed_during_export")
            if converted_hash is not None and _file_hash(read_path) != converted_hash:
                raise ExportError("converted_input_changed_during_export")
        except Exception as exc:  # per-file isolation is an explicit batch contract
            base.update(status="failed", code=_stable_error_code(exc))
            manifest.append(base)
            counts["failed"] += 1
            continue
        base.update(
            status="completed",
            code="completed",
            output_directory=directory_name,
            quality_flags=document["quality_flags"],
        )
        manifest.append(base)
        counts["completed"] += 1
        review_count += int("structure_review_required" in document["quality_flags"])
    _assert_safe_output_target(output_root / "manifest.jsonl", output_root)
    _write_jsonl(output_root / "manifest.jsonl", manifest)
    summary = ExportSummary(
        total=len(sources),
        completed=counts["completed"],
        pending_conversion=counts["pending_conversion"],
        failed=counts["failed"],
        structure_review_required=review_count,
    )
    _assert_safe_output_target(output_root / "summary.json", output_root)
    _atomic_json(
        output_root / "summary.json",
        {**asdict(summary), "export_version": EXPORT_VERSION, "pilot_limit": config.limit},
    )
    _assert_safe_output_target(output_root / "README.md", output_root)
    _atomic_text(output_root / "README.md", _readme())
    return summary


def _export_one(
    *,
    source: Path,
    read_path: Path,
    source_hash: str,
    converted_hash: str | None,
    provenance: ConversionProvenance | None,
    relative: str,
    directory: Path,
    config: ExportConfig,
) -> dict[str, Any]:
    expected = directory / "document.json"
    output_root = config.output_root.resolve(strict=False)
    _assert_safe_output_target(expected, output_root)
    if expected.is_file():
        try:
            loaded = json.loads(expected.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("invalid completion metadata")
            old = cast(dict[str, Any], loaded)
            if _valid_existing(
                old, directory, output_root, source, relative, source_hash,
                converted_hash, config, provenance
            ):
                return old
        except (OSError, ValueError, TypeError):
            pass
    read = DocxExportReader().read(read_path)
    _verify_input_unchanged(source, read_path, source_hash, converted_hash)
    if not read.paragraphs:
        raise ExportError("no_readable_text")
    body = tuple(p for p in read.paragraphs if p.location in {"body", "table"})
    parsed_document = ParsedDocument(
        paragraphs=tuple(
            ParsedParagraph(
                p.text,
                ordinal=p.ordinal,
                numbering_provenance=p.numbering_provenance,
            )
            for p in body
        ),
        source_ref=str(source),
        source_sha256=bytes.fromhex(source_hash),
        loader_version=read.loader_version,
    )
    flags = set(read.quality_flags)
    if provenance is not None:
        flags.update(provenance.quality_flags)
    try:
        instrument = LegalStructureParser().parse(parsed_document)
        articles = instrument.articles
        article_numbers = [article.provision_no for article in articles]
        if len(article_numbers) != len(set(article_numbers)):
            articles = ()
            flags.add("duplicate_article_number")
            flags.add("structure_review_required")
    except ValueError:
        articles = ()
        flags.add("structure_review_required")
    if not articles:
        flags.add("structure_review_required")
    paragraphs = []
    for p in read.paragraphs:
        row = {
            "paragraph_id": _stable_id("paragraph", relative, source_hash, str(p.ordinal)),
            "ordinal": p.ordinal,
            "text": p.text,
            "part": p.part,
            "location": p.location,
            "table_position": list(p.table_position) if p.table_position else None,
        }
        if p.numbering_provenance is not None:
            row["numbering_provenance"] = asdict(p.numbering_provenance)
        paragraphs.append(row)
    chunks = _build_chunks(articles, body, relative, source_hash, config)
    if not articles:
        chunks = [_source_chunk(p, relative, source_hash) for p in read.paragraphs]
    else:
        used = {ordinal for row in chunks for ordinal in row["source_paragraph_ordinals"]}
        chunks.extend(
            _source_chunk(p, relative, source_hash)
            for p in read.paragraphs
            if p.ordinal not in used
        )
    covered = {ordinal for row in chunks for ordinal in row["source_paragraph_ordinals"]}
    expected_ordinals = {p.ordinal for p in read.paragraphs}
    if covered != expected_ordinals:
        raise ExportError("coverage_validation_failed")
    _assert_safe_output_target(directory, output_root)
    directory.mkdir(parents=True, exist_ok=True)
    _assert_safe_output_target(directory / "paragraphs.jsonl", output_root)
    paragraph_hash = _write_jsonl(directory / "paragraphs.jsonl", paragraphs)
    _assert_safe_output_target(directory / "chunks.jsonl", output_root)
    chunk_hash = _write_jsonl(directory / "chunks.jsonl", chunks)
    document: dict[str, Any] = {
        "schema_version": EXPORT_VERSION,
        "id_namespace": ID_NAMESPACE,
        "document_id": _stable_id("document", relative, source_hash),
        "source_path": str(source),
        "source_relative_path": relative,
        "source_sha256": source_hash,
        "input_sha256": converted_hash or source_hash,
        "converted_sha256": converted_hash,
        "conversion_provenance": asdict(provenance) if provenance is not None else None,
        "loader_version": read.loader_version,
        "parser_version": PARSER_VERSION,
        "export_version": EXPORT_VERSION,
        "configuration": {
            "max_leaf_chars": config.max_leaf_chars,
            "window_chars": config.window_chars,
            "overlap_chars": config.overlap_chars,
        },
        "legal_metadata": {
            "title": None,
            "version": None,
            "issuing_authority": None,
            "published_on": None,
            "effective_on": None,
            "status": "unknown",
        },
        "paragraph_count": len(paragraphs),
        "chunk_count": len(chunks),
        "parent_count": sum(row["parent_chunk_id"] is None for row in chunks),
        "child_count": sum(row["parent_chunk_id"] is not None for row in chunks),
        "covered_paragraph_count": len(covered),
        "quality_flags": sorted(flags),
        "output_hashes": {"paragraphs.jsonl": paragraph_hash, "chunks.jsonl": chunk_hash},
    }
    _verify_input_unchanged(source, read_path, source_hash, converted_hash)
    _assert_safe_output_target(expected, output_root)
    _atomic_json(expected, document)  # completion marker is written last
    return document


def _verify_input_unchanged(
    source: Path, read_path: Path, source_hash: str, converted_hash: str | None
) -> None:
    if _file_hash(source) != source_hash:
        raise ExportError("source_changed_during_export")
    if converted_hash is not None and _file_hash(read_path) != converted_hash:
        raise ExportError("converted_input_changed_during_export")


def _build_chunks(
    articles: tuple[ParsedArticle, ...],
    body: tuple[Any, ...],
    relative: str,
    source_hash: str,
    config: ExportConfig,
) -> list[dict[str, Any]]:
    if not articles:
        return []
    provision_rows: list[_Provision] = []
    article_ordinals: list[list[int]] = []
    cursor = 0
    for index, article in enumerate(articles):
        matched: list[int] = []
        for piece in article.paragraphs:
            while cursor < len(body) and body[cursor].text != piece:
                cursor += 1
            if cursor >= len(body):
                raise ExportError("article_paragraph_mapping_failed")
            matched.append(body[cursor].ordinal)
            cursor += 1
        article_ordinals.append(matched)
        provision_rows.append(
            _Provision(
                _uuid7(relative, source_hash, "provision", str(index)),
                article.text,
                article.char_start,
            )
        )
    derived = derive_hierarchical_chunks_with_locations(
        version_id=_uuid7(relative, source_hash, "version"),
        provisions=tuple(provision_rows),
        articles=articles,
        parser_version=PARSER_VERSION,
        max_leaf_chars=config.max_leaf_chars,
        window_chars=config.window_chars,
        overlap_chars=config.overlap_chars,
    )
    provision_index = {row.id: index for index, row in enumerate(provision_rows)}
    parent_ids: dict[UUID, str] = {}
    result: list[dict[str, Any]] = []
    per_provision_sequence: dict[UUID, int] = {}
    paragraph_spans = [
        _paragraph_spans(article, article_ordinals[index])
        for index, article in enumerate(articles)
    ]
    for located in derived:
        chunk = located.chunk
        index = provision_index[chunk.provision_id]
        sequence = per_provision_sequence.get(chunk.provision_id, 0)
        per_provision_sequence[chunk.provision_id] = sequence + 1
        chunk_id = _stable_id(
            "chunk", relative, source_hash, str(index), str(sequence), chunk.content
        )
        if chunk.parent_chunk_id is None:
            parent_ids[chunk.id] = chunk_id
            parent_id = None
        else:
            parent_id = parent_ids[chunk.parent_chunk_id]
        local_span = located.parent_relative_char_span
        exact_ordinals = [
            ordinal
            for ordinal, start, end in paragraph_spans[index]
            if start < local_span[1] and end > local_span[0]
        ]
        result.append(
            {
                "chunk_id": chunk_id,
                "parent_chunk_id": parent_id,
                "chunk_type": chunk.chunk_type.value,
                "text": chunk.content,
                "content_sha256": sha256(chunk.content.encode()).hexdigest(),
                "article_no": articles[index].provision_no,
                "structure_path": list(articles[index].structure_path),
                "source_paragraph_ordinals": exact_ordinals,
                "source_char_range": [
                    articles[index].char_start + local_span[0],
                    articles[index].char_start + local_span[1],
                ],
                "parent_source_paragraph_ordinals": article_ordinals[index],
                "parent_source_char_range": [
                    articles[index].char_start, articles[index].char_end
                ],
                "parent_relative_char_span": list(local_span),
                "source_location_status": "exact_parent_text",
            }
        )
    return result


def _source_chunk(paragraph: Any, relative: str, source_hash: str) -> dict[str, Any]:
    return {
        "chunk_id": _stable_id("source-paragraph", relative, source_hash, str(paragraph.ordinal)),
        "parent_chunk_id": None,
        "chunk_type": "source_paragraph",
        "text": paragraph.text,
        "content_sha256": sha256(paragraph.text.encode()).hexdigest(),
        "article_no": None,
        "structure_path": [],
        "source_paragraph_ordinals": [paragraph.ordinal],
        "source_char_range": None,
        "parent_source_paragraph_ordinals": [paragraph.ordinal],
        "parent_source_char_range": None,
        "parent_relative_char_span": [0, len(paragraph.text)],
        "source_location_status": "exact_source_paragraph",
    }


def _load_conversions(
    config: ExportConfig, source_root: Path
) -> dict[str, tuple[Path, str, str, ConversionProvenance]]:
    if config.conversion_manifest is None:
        return {}
    manifest = config.conversion_manifest.resolve(strict=True)
    converted_root = (config.converted_root or manifest.parent).resolve(strict=True)
    _reject_overlap(source_root, converted_root)
    result: dict[str, tuple[Path, str, str, ConversionProvenance]] = {}
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ExportError("invalid_conversion_manifest_json") from exc
        if not isinstance(row, dict):
            raise ExportError("invalid_conversion_manifest_record")
        if row.get("schema_version") != "word-conversion-v1" or row.get("status") != "converted":
            continue
        try:
            source_raw = Path(row["source_path"])
            converted_raw = Path(row["converted_path"])
            source_hash = str(row["source_sha256"])
            converted_hash = str(row["converted_sha256"])
        except (KeyError, TypeError) as exc:
            raise ExportError("invalid_conversion_manifest_record") from exc
        if not source_raw.is_absolute() or not converted_raw.is_absolute():
            raise ExportError("conversion_paths_must_be_absolute")
        if not _is_sha256(source_hash) or not _is_sha256(converted_hash):
            raise ExportError("invalid_conversion_hash")
        _reject_reparse_path(source_raw, "unsafe_source_root")
        _reject_reparse_path(converted_raw, "unsafe_output_path")
        source = source_raw.resolve(strict=False)
        converted = converted_raw.resolve(strict=False)
        if not _within(source, source_root) or not _within(converted, converted_root):
            raise ExportError("conversion_path_outside_root")
        if not is_legacy_word_source(source) or converted.suffix.lower() != ".docx":
            raise ExportError("invalid_conversion_extensions")
        key = str(source)
        if key in result:
            raise ExportError("duplicate_conversion_source")
        try:
            provenance = ConversionProvenance.from_record(row)
        except ValueError as exc:
            raise ExportError("invalid_conversion_provenance") from exc
        result[key] = (converted, source_hash, converted_hash, provenance)
    return result


def _enumerate_sources(root: Path, *, docx_only: bool) -> list[Path]:
    found: list[Path] = []
    errors: list[OSError] = []
    for current, directories, files in os.walk(
        root, followlinks=False, onerror=errors.append
    ):
        directories[:] = sorted(
            d for d in directories if not _is_reparse(Path(current) / d)
        )
        for name in sorted(files):
            path = Path(current) / name
            if _is_reparse(path) or name.startswith("~$"):
                continue
            suffixes = {".docx"} if docx_only else {".doc", ".docx", ".docm"}
            if path.suffix.lower() in suffixes:
                found.append(path.resolve(strict=True))
    if errors:
        raise ExportError("source_enumeration_failed") from errors[0]
    return sorted(found, key=lambda path: path.relative_to(root).as_posix().casefold())


def _valid_existing(
    document: dict[str, Any], directory: Path, output_root: Path,
    source: Path, relative: str,
    source_hash: str, converted_hash: str | None, config: ExportConfig,
    provenance: ConversionProvenance | None,
) -> bool:
    required = {
        "schema_version", "id_namespace", "document_id", "source_path",
        "source_relative_path", "source_sha256", "input_sha256", "loader_version",
        "parser_version", "export_version", "configuration", "legal_metadata",
        "paragraph_count", "chunk_count", "parent_count", "child_count",
        "covered_paragraph_count", "quality_flags", "output_hashes",
    }
    if not required.issubset(document):
        return False
    if (
        document.get("schema_version") != EXPORT_VERSION
        or document.get("id_namespace") != ID_NAMESPACE
    ):
        return False
    if (
        document.get("source_path") != str(source)
        or document.get("source_relative_path") != relative
    ):
        return False
    if document.get("document_id") != _stable_id("document", relative, source_hash):
        return False
    if (
        document.get("loader_version") not in {LOADER_VERSION, NUMBERING_LOADER_VERSION}
        or document.get("parser_version") != PARSER_VERSION
    ):
        return False
    if not isinstance(document.get("quality_flags"), list):
        return False
    flags = document["quality_flags"]
    if not all(isinstance(flag, str) for flag in flags):
        return False
    if document.get("conversion_provenance") != (
        asdict(provenance) if provenance is not None else None
    ):
        return False
    expected_flags = provenance.quality_flags if provenance is not None else frozenset()
    if set(flags) & CONVERSION_QUALITY_FLAGS != expected_flags:
        return False
    if (
        document.get("source_sha256") != source_hash
        or document.get("export_version") != EXPORT_VERSION
    ):
        return False
    if document.get("converted_sha256") != converted_hash:
        return False
    expected_config = {
        "max_leaf_chars": config.max_leaf_chars,
        "window_chars": config.window_chars,
        "overlap_chars": config.overlap_chars,
    }
    if document.get("configuration") != expected_config:
        return False
    hashes = document.get("output_hashes")
    if not isinstance(hashes, dict) or set(hashes) != {"paragraphs.jsonl", "chunks.jsonl"}:
        return False
    for name, digest in hashes.items():
        target = directory / name
        _assert_safe_output_target(target, output_root)
        if _file_hash(target) != digest:
            return False
    return _cached_rows_are_valid(document, directory, output_root)


def _cached_rows_are_valid(
    document: dict[str, Any], directory: Path, output_root: Path
) -> bool:
    try:
        _assert_safe_output_target(directory / "paragraphs.jsonl", output_root)
        paragraphs = _read_jsonl(directory / "paragraphs.jsonl")
        _assert_safe_output_target(directory / "chunks.jsonl", output_root)
        chunks = _read_jsonl(directory / "chunks.jsonl")
        if not paragraphs or not chunks:
            return False
        if len(paragraphs) != document["paragraph_count"]:
            return False
        if len(chunks) != document["chunk_count"]:
            return False
        parent_ids = {
            row["chunk_id"] for row in chunks if row.get("parent_chunk_id") is None
        }
        if len(parent_ids) != document["parent_count"]:
            return False
        children = [row for row in chunks if row.get("parent_chunk_id") is not None]
        if len(children) != document["child_count"]:
            return False
        if any(row.get("parent_chunk_id") not in parent_ids for row in children):
            return False
        required_chunk_fields = {
            "chunk_id", "parent_chunk_id", "chunk_type", "text",
            "content_sha256", "article_no", "structure_path",
            "source_paragraph_ordinals", "source_char_range",
            "parent_source_paragraph_ordinals", "parent_source_char_range",
            "parent_relative_char_span", "source_location_status",
        }
        if any(not required_chunk_fields.issubset(row) for row in chunks):
            return False
        paragraph_ordinals = {row["ordinal"] for row in paragraphs}
        covered = {
            ordinal
            for row in chunks
            for ordinal in row["source_paragraph_ordinals"]
        }
        return covered == paragraph_ordinals and len(covered) == document["covered_paragraph_count"]
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        loaded = json.loads(line)
        if not isinstance(loaded, dict):
            raise ValueError("invalid jsonl row")
        result.append(cast(dict[str, Any], loaded))
    return result


def _paragraph_spans(
    article: ParsedArticle, ordinals: list[int]
) -> list[tuple[int, int, int]]:
    cursor = 0
    result: list[tuple[int, int, int]] = []
    for ordinal, text in zip(ordinals, article.paragraphs, strict=True):
        result.append((ordinal, cursor, cursor + len(text)))
        cursor += len(text)
    return result


def _reject_overlap(first: Path, second: Path) -> None:
    if _within(first, second) or _within(second, first):
        raise ExportError("source_output_overlap")


def _is_reparse(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _reject_reparse_path(path: Path, code: str) -> None:
    absolute = path.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.exists() and _is_reparse(candidate):
            raise ExportError(code)


def _assert_safe_output_target(path: Path, output_root: Path) -> None:
    if not _within(path, output_root):
        raise ExportError("unsafe_output_path")
    current = output_root
    if _is_reparse(current):
        raise ExportError("unsafe_output_path")
    for part in path.relative_to(output_root).parts:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise ExportError("unsafe_output_path")


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _uuid7(*parts: str) -> UUID:
    raw = bytearray(sha256("\0".join((ID_NAMESPACE, *parts)).encode()).digest()[:16])
    raw[6] = (raw[6] & 0x0F) | 0x70
    raw[8] = (raw[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(raw))


def _stable_id(*parts: str) -> str:
    return sha256("\0".join((ID_NAMESPACE, *parts)).encode()).hexdigest()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _file_hash(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
    _atomic_text(path, text)
    return sha256(text.encode()).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _stable_error_code(exc: Exception) -> str:
    text = str(exc)
    allowed = {
        "zip_member_count_limit", "expanded_size_limit", "xml_entities_forbidden",
        "document_xml_missing", "invalid_docx_zip", "invalid_docx_xml",
        "coverage_validation_failed", "article_paragraph_mapping_failed",
        "child_source_location_failed", "no_readable_text",
        "source_changed_during_export", "converted_input_changed_during_export",
        "unsafe_output_path", "conversion_hash_mismatch",
    }
    if text in allowed:
        return text
    if isinstance(exc, (OSError, PermissionError)):
        return "source_read_failed"
    return "document_export_failed"


def _readme() -> str:
    return (
        "# 法规离线切块导出\n\n"
        "此目录是机械读取与切块产物，ID 属于 `offline-export-v1`，不是 MySQL ID。\n\n"
        "按原始文件查找时，打开另行生成的 `文件清单.csv`，查看切块文件绝对路径。"
        "每份来源的目录中，`chunks.jsonl` 保存切块，`paragraphs.jsonl` 保存段落，"
        "`document.json` 保存来源、版本、质量标记与输出哈希。JSON/JSONL 均为 UTF-8。\n\n"
        "`manifest.jsonl` 是逐源状态，`summary.json` 是导出汇总。重新导出后必须重新"
        "运行独立核验并生成 CSV；全范围 `verification.json` 的 `complete=true` 且"
        "`errors=[]` 才表示全部来源已通过核验。目录存在或部分 completed 不代表全集完成。\n\n"
        "字符范围为左闭右开区间，来源范围基于规范化段落文本，"
        "不是 Word 文件字节偏移；父块相对范围结合段落序号和 XML part 定位。"
        "父块保留整条，因此父子文本有意重叠，不能将两者视为不重复的文档。\n\n"
        "`completed` 仅表示文本覆盖校验通过；表格排版、自动编号、图片/文本框 OCR、"
        "法律元数据与效力均未据此确认。附属脚注、尾注、页眉和页脚按 XML part 单独保存，"
        "不混入正文条文。`structure_review_required` 必须人工复核。\n"
    )
