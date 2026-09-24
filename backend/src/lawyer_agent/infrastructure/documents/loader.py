"""Unified read-only document loading for legal corpus import.

Loaders never write to the source file; they return normalized paragraphs with
basic metadata. ZIP files and macros are not executed. Only mainland-China
legal corpus is in scope for this slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from lawyer_agent.infrastructure.documents.word_article_numbering import NumberingProvenance


@dataclass(frozen=True, slots=True)
class ParsedParagraph:
    text: str
    style: str | None = None
    ordinal: int = 0
    numbering_provenance: NumberingProvenance | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    paragraphs: tuple[ParsedParagraph, ...]
    source_ref: str
    source_sha256: bytes | None = None
    loader_version: str = "docx-zip-v1"


class DocumentLoader(Protocol):
    loader_version: str

    def load(self, source_ref: str, payload: bytes) -> ParsedDocument: ...
