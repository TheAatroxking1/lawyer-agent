"""Read-only verification of delivered chunk files against their current sources."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

from lawyer_agent.application.conversion_provenance import (
    CONVERSION_QUALITY_FLAGS,
    ConversionProvenance,
)
from lawyer_agent.application.legal_chunk_structure import derive_hierarchical_chunks_with_locations
from lawyer_agent.application.legal_corpus_export import EXPORT_VERSION, PARSER_VERSION
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader, ExportDocument
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser
from lawyer_agent.infrastructure.documents.source_format import is_legacy_word_source


@dataclass(frozen=True)
class VerificationIssue:
    source_path: str
    code: str


@dataclass(frozen=True)
class VerificationReport:
    complete: bool
    scope_complete: bool
    manifest_sources: int
    verified_documents: int
    verified_chunks: int
    errors: tuple[VerificationIssue, ...]


class _Invalid(ValueError):
    pass


@dataclass(frozen=True)
class _VerificationProvision:
    id: UUID
    full_text: str
    char_start: int


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise _Invalid(code)


def _hash(path: Path) -> str:
    with path.open("rb") as stream:
        import hashlib

        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe(path: Path, root: Path, code: str) -> Path:
    resolved = path.resolve(strict=True)
    _require(resolved.is_relative_to(root), code)
    current = path.absolute()
    while current != root and current != current.parent:
        _require(not current.is_symlink() and not current.is_junction(), code)
        current = current.parent
    return resolved


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), "invalid_metadata")
    return dict(value)


def _rows(path: Path) -> list[dict[str, Any]]:
    result = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            value = json.loads(line)
            _require(isinstance(value, dict), "invalid_jsonl_record")
            result.append(dict(value))
    return result


def _sources(root: Path) -> set[str]:
    result: set[str] = set()

    def failed(error: OSError) -> None:
        raise error

    for directory, directories, files in os.walk(root, onerror=failed, followlinks=False):
        directories[:] = [
            name for name in directories
            if not (Path(directory) / name).is_symlink()
            and not (Path(directory) / name).is_junction()
        ]
        for name in files:
            candidate = Path(directory) / name
            if candidate.suffix.lower() not in {".doc", ".docx", ".docm"} or name.startswith("~$"):
                continue
            if candidate.is_symlink() or candidate.is_junction():
                continue
            result.add(str(_safe(candidate, root, "source_path_outside_root")))
    return result


def _validate_chunks(
    paragraphs: list[dict[str, Any]], chunks: list[dict[str, Any]], metadata: dict[str, Any]
) -> None:
    _require(bool(paragraphs) and bool(chunks), "empty_export")
    by_ordinal = {row["ordinal"]: row for row in paragraphs}
    _require(len(by_ordinal) == len(paragraphs), "duplicate_paragraph")
    _require(set(by_ordinal) == set(range(len(paragraphs))), "invalid_paragraph_ordinals")
    by_id = {row["chunk_id"]: row for row in chunks}
    _require(len(by_id) == len(chunks), "duplicate_chunk")
    covered: set[int] = set()
    for row in chunks:
        text = row["text"]
        _require(isinstance(text, str) and bool(text), "empty_chunk")
        _require(sha256(text.encode()).hexdigest() == row["content_sha256"], "chunk_hash_mismatch")
        parent_id = row["parent_chunk_id"]
        if parent_id is not None:
            _require(parent_id in by_id, "missing_parent_chunk")
            parent = by_id[parent_id]
            _require(parent["article_no"] == row["article_no"], "cross_article_parent")
            _require(text in parent["text"], "chunk_source_content_mismatch")
            span = row["parent_relative_char_span"]
            _require(
                isinstance(span, list) and len(span) == 2
                and all(type(n) is int for n in span)
                and 0 <= span[0] < span[1] <= len(parent["text"])
                and parent["text"][span[0]:span[1]] == text,
                "child_source_location_mismatch",
            )
            expected_ordinals = []
            offset = 0
            for ordinal in parent["source_paragraph_ordinals"]:
                end = offset + len(by_ordinal[ordinal]["text"].strip())
                if offset < span[1] and end > span[0]:
                    expected_ordinals.append(ordinal)
                offset = end
            parent_start = parent["source_char_range"][0]
            _require(
                row["source_location_status"] == "exact_parent_text"
                and row["source_paragraph_ordinals"] == expected_ordinals
                and row["parent_source_paragraph_ordinals"] == parent["source_paragraph_ordinals"]
                and row["parent_source_char_range"] == parent["source_char_range"]
                and row["source_char_range"] == [parent_start + span[0], parent_start + span[1]]
                and row["structure_path"] == parent["structure_path"],
                "child_source_location_mismatch",
            )
            visited = {row["chunk_id"]}
            ancestor = parent_id
            while ancestor is not None:
                _require(ancestor in by_id and ancestor not in visited, "invalid_parent_graph")
                visited.add(ancestor)
                ancestor = by_id[ancestor]["parent_chunk_id"]
            continue
        ordinals = row["source_paragraph_ordinals"]
        _require(
            isinstance(ordinals, list) and bool(ordinals)
            and all(isinstance(n, int) and n in by_ordinal for n in ordinals)
            and len(ordinals) == len(set(ordinals)),
            "invalid_source_ordinals",
        )
        covered.update(ordinals)
        if row["chunk_type"] == "source_paragraph":
            expected = "".join(by_ordinal[n]["text"] for n in ordinals)
        else:
            expected = "".join(by_ordinal[n]["text"].strip() for n in ordinals)
        _require(text == expected, "chunk_source_content_mismatch")
    _require(covered == set(by_ordinal), "paragraph_coverage_mismatch")
    parents = sum(row["parent_chunk_id"] is None for row in chunks)
    _require(
        metadata["paragraph_count"] == len(paragraphs)
        and metadata["covered_paragraph_count"] == len(paragraphs)
        and metadata["chunk_count"] == len(chunks)
        and metadata["parent_count"] == parents
        and metadata["child_count"] == len(chunks) - parents,
        "metadata_count_mismatch",
    )


def _validate_structure(
    read: ExportDocument, chunks: list[dict[str, Any]], metadata: dict[str, Any],
) -> None:
    body = tuple(p for p in read.paragraphs if p.part == "word/document.xml")
    try:
        articles = LegalStructureParser().parse(
            ParsedDocument(
                paragraphs=tuple(ParsedParagraph(p.text, ordinal=p.ordinal) for p in body),
                source_ref="offline-verification",
            )
        ).articles
        if len({a.provision_no for a in articles}) != len(articles):
            articles = ()
    except ValueError:
        articles = ()
    expected = {(a.char_start, a.char_end): a for a in articles}
    actual_ranges: set[tuple[int, int]] = set()
    for row in chunks:
        if row["parent_chunk_id"] is not None:
            continue
        if row["chunk_type"] == "source_paragraph":
            _require(
                row["article_no"] is None and row["structure_path"] == []
                and row["source_char_range"] is None,
                "article_structure_mismatch",
            )
            continue
        span = row["source_char_range"]
        _require(
            isinstance(span, list) and len(span) == 2
            and all(type(n) is int for n in span),
            "article_structure_mismatch",
        )
        key = (span[0], span[1])
        _require(key in expected and key not in actual_ranges, "article_structure_mismatch")
        article = expected[key]
        _require(
            row["chunk_type"] == "provision" and row["article_no"] == article.provision_no
            and row["structure_path"] == list(article.structure_path)
            and row["text"] == article.text,
            "article_structure_mismatch",
        )
        actual_ranges.add(key)
    _require(actual_ranges == set(expected), "article_structure_mismatch")
    structural = [row for row in chunks if row["chunk_type"] != "source_paragraph"]
    if not articles:
        _require(not structural, "chunk_derivation_mismatch")
        return
    provisions = tuple(
        _VerificationProvision(new_uuid7(), article.text, article.char_start)
        for article in articles
    )
    config = metadata["configuration"]
    located = derive_hierarchical_chunks_with_locations(
        version_id=new_uuid7(), provisions=provisions, articles=articles,
        parser_version=PARSER_VERSION,
        max_leaf_chars=config["max_leaf_chars"], window_chars=config["window_chars"],
        overlap_chars=config["overlap_chars"],
    )
    _require(len(located) == len(structural), "chunk_derivation_mismatch")
    provision_indices = {provision.id: index for index, provision in enumerate(provisions)}
    sequences: dict[UUID, int] = {}
    parents: dict[UUID, str] = {}
    for expected_chunk, row in zip(located, structural, strict=True):
        chunk = expected_chunk.chunk
        index = provision_indices[chunk.provision_id]
        sequence = sequences.get(chunk.provision_id, 0)
        sequences[chunk.provision_id] = sequence + 1
        chunk_id = sha256("\0".join((
            "offline-export-v1", "chunk", metadata["source_relative_path"],
            metadata["source_sha256"], str(index), str(sequence), chunk.content,
        )).encode()).hexdigest()
        parent_id = None
        if chunk.parent_chunk_id is None:
            parents[chunk.provision_id] = chunk_id
        else:
            parent_id = parents[chunk.provision_id]
        _require(
            row["chunk_id"] == chunk_id and row["parent_chunk_id"] == parent_id
            and row["chunk_type"] == chunk.chunk_type.value and row["text"] == chunk.content
            and row["parent_relative_char_span"] == list(expected_chunk.parent_relative_char_span),
            "chunk_derivation_mismatch",
        )


def _verify_document(
    row: dict[str, Any], *, source_root: Path, output_root: Path,
    conversions: dict[str, dict[str, Any]], converted_root: Path | None,
) -> int:
    source = _safe(Path(row["source_path"]), source_root, "source_path_outside_root")
    _require(_hash(source) == row["source_sha256"], "source_hash_mismatch")
    relative_dir = Path(row["output_directory"])
    _require(
        not relative_dir.is_absolute() and len(relative_dir.parts) == 1,
        "output_path_outside_root",
    )
    folder = _safe(output_root / relative_dir, output_root, "output_path_outside_root")
    metadata = _object(_safe(folder / "document.json", output_root, "output_path_outside_root"))
    relative = source.relative_to(source_root).as_posix()
    document_id = sha256(
        "\0".join(("offline-export-v1", "document", relative, row["source_sha256"])).encode()
    ).hexdigest()
    _require(
        metadata["id_namespace"] == "offline-export-v1"
        and metadata["schema_version"] == metadata["export_version"] == EXPORT_VERSION
        and metadata["parser_version"] == PARSER_VERSION
        and metadata["source_path"] == str(source)
        and metadata["source_relative_path"] == relative
        and metadata["source_sha256"] == row["source_sha256"]
        and metadata["document_id"] == row["document_id"] == document_id,
        "metadata_source_mismatch",
    )
    files = {
        name: _safe(folder / name, output_root, "output_path_outside_root")
        for name in ("paragraphs.jsonl", "chunks.jsonl")
    }
    for name, path in files.items():
        _require(_hash(path) == metadata["output_hashes"][name], "output_hash_mismatch")
    paragraphs, chunks = _rows(files["paragraphs.jsonl"]), _rows(files["chunks.jsonl"])
    _validate_chunks(paragraphs, chunks, metadata)
    read_path = source
    provenance: ConversionProvenance | None = None
    if is_legacy_word_source(source):
        _require(str(source) in conversions and converted_root is not None, "conversion_missing")
        conversion = conversions[str(source)]
        provenance = ConversionProvenance.from_record(conversion)
        assert converted_root is not None
        read_path = _safe(
            Path(conversion["converted_path"]), converted_root, "conversion_path_outside_root",
        )
        _require(
            conversion["source_sha256"] == row["source_sha256"]
            and _hash(read_path) == conversion["converted_sha256"]
            == metadata["input_sha256"] == metadata["converted_sha256"],
            "conversion_hash_mismatch",
        )
    else:
        _require(metadata["input_sha256"] == row["source_sha256"], "input_hash_mismatch")
    _require(
        metadata.get("conversion_provenance") == (
            asdict(provenance) if provenance is not None else None
        ), "conversion_provenance_mismatch",
    )
    flags = metadata.get("quality_flags")
    _require(isinstance(flags, list) and all(isinstance(flag, str) for flag in flags),
             "invalid_quality_flags")
    assert isinstance(flags, list)
    expected_flags = provenance.quality_flags if provenance is not None else frozenset()
    _require(set(flags) & CONVERSION_QUALITY_FLAGS == expected_flags,
             "conversion_quality_flags_mismatch")
    _require(row.get("quality_flags") == flags, "manifest_quality_flags_mismatch")
    read = DocxExportReader().read(read_path)
    _require(metadata["loader_version"] == read.loader_version, "loader_version_mismatch")
    _validate_structure(read, chunks, metadata)
    _require(len(read.paragraphs) == len(paragraphs), "source_paragraphs_mismatch")
    for expected, actual in zip(read.paragraphs, paragraphs, strict=True):
        _require(
            expected.text == actual["text"] and expected.ordinal == actual["ordinal"]
            and expected.part == actual["part"] and expected.location == actual["location"],
            "source_paragraphs_mismatch",
        )
        expected_position = list(expected.table_position) if expected.table_position else None
        _require(actual["table_position"] == expected_position, "source_table_position_mismatch")
    _require(_hash(source) == row["source_sha256"], "source_changed_during_verification")
    _require(_hash(read_path) == metadata["input_sha256"], "input_changed_during_verification")
    return len(chunks)


def verify_export(
    *, source_root: Path, output_root: Path, require_all_sources: bool = True,
    conversion_manifest: Path | None = None, converted_root: Path | None = None,
) -> VerificationReport:
    source_root, output_root = source_root.resolve(strict=True), output_root.resolve(strict=True)
    issues: list[VerificationIssue] = []
    verified = chunk_count = 0
    manifest: list[dict[str, Any]] = []
    scope_complete = False
    try:
        _require(
            not source_root.is_relative_to(output_root)
            and not output_root.is_relative_to(source_root), "source_output_overlap",
        )
        manifest = _rows(
            _safe(output_root / "manifest.jsonl", output_root, "output_path_outside_root")
        )
        declared = [row["source_path"] for row in manifest]
        _require(len(declared) == len(set(declared)), "duplicate_manifest_source")
        if require_all_sources:
            actual_sources = _sources(source_root)
            for missing in sorted(actual_sources - set(declared)):
                issues.append(VerificationIssue(missing, "source_missing_from_manifest"))
            for extra in sorted(set(declared) - actual_sources):
                issues.append(VerificationIssue(extra, "manifest_source_not_in_scope"))
            scope_complete = actual_sources == set(declared) and bool(actual_sources)
        conversions: dict[str, dict[str, Any]] = {}
        if conversion_manifest is not None:
            converted_root = (converted_root or conversion_manifest.parent).resolve(strict=True)
            _require(
                not converted_root.is_relative_to(source_root)
                and not source_root.is_relative_to(converted_root),
                "source_conversion_overlap",
            )
            for conversion in _rows(conversion_manifest):
                if conversion.get("status") == "converted":
                    _require(
                        conversion["schema_version"] == "word-conversion-v1", "conversion_schema",
                    )
                    key = conversion["source_path"]
                    original = Path(key)
                    converted = Path(conversion["converted_path"])
                    _require(
                        original.is_absolute() and converted.is_absolute()
                        and converted.suffix.lower() == ".docx",
                        "invalid_conversion_paths",
                    )
                    _safe(original, source_root, "source_path_outside_root")
                    _safe(converted, converted_root, "conversion_path_outside_root")
                    _require(is_legacy_word_source(original), "invalid_conversion_paths")
                    _require(key not in conversions, "duplicate_conversion_source")
                    conversions[key] = conversion
        for row in manifest:
            source = str(row.get("source_path", ""))
            try:
                _require(row.get("status") == "completed", "source_not_completed")
                chunk_count += _verify_document(
                    row, source_root=source_root, output_root=output_root,
                    conversions=conversions, converted_root=converted_root,
                )
                verified += 1
            except _Invalid as exc:
                issues.append(VerificationIssue(source, str(exc)))
            except (OSError, ValueError, KeyError, TypeError):
                issues.append(VerificationIssue(source, "invalid_or_missing_export"))
    except _Invalid as exc:
        issues.append(VerificationIssue("", str(exc)))
    except (OSError, ValueError, KeyError, TypeError):
        issues.append(VerificationIssue("", "manifest_or_inventory_invalid"))
    return VerificationReport(
        complete=scope_complete and not issues, scope_complete=scope_complete,
        manifest_sources=len(manifest), verified_documents=verified,
        verified_chunks=chunk_count, errors=tuple(issues),
    )
