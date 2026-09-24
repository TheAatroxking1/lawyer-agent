"""Structured hierarchical legal chunking (non-model, retrieval feed).

Design (2026-09-10, user-confirmed): every law document gets a document-level
record; structure is recognised as 编-章-节-条; every article is an independent
PARENT block (never joined across articles, short articles are never merged);
over-long articles are split into 款/项/目 CHILD blocks that inherit the full
article text through the DB provision (and the parent chunk id); an optional
sliding-window pass covers leaf units that are still too long — windows never
cross 款/项/条 boundaries.

This module produces the chunk *rows* for one version. Indexing (which rows are
searchable: parents without children plus child leaves) and retrieval belong to
later slices; the rows here keep the full parent chain so those slices can
filter and restore article context from MySQL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_article_heading import ARTICLE_HEADING
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    content_sha256,
)
from lawyer_agent.domain.legal_parser_profiles import EXACT_TEXT_CHUNK_PARSER_VERSIONS

_ITEM_PAREN = re.compile(r"^[（(]\s*[一二三四五六七八九十百]+[)）]")
_ITEM_CN = re.compile(r"^[一二三四五六七八九十百]+、")
_SUB_NUMBER = re.compile(r"^[0-9０-９]+[\.．、]")
_SENTENCE_END = "。！？；;"

_DEFAULT_MAX_LEAF_CHARS = 600
_DEFAULT_WINDOW_CHARS = 400
_DEFAULT_OVERLAP_CHARS = 60


class _ProvisionSource(Protocol):
    @property
    def id(self) -> UUID: ...

    @property
    def full_text(self) -> str: ...

    @property
    def char_start(self) -> int: ...


class _ArticleSource(Protocol):
    @property
    def provision_no(self) -> str: ...

    @property
    def structure_path(self) -> tuple[str, ...]: ...

    @property
    def paragraphs(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class _Unit:
    kind: ChunkType
    text: str
    parent_start: int


@dataclass(frozen=True, slots=True)
class LocatedLegalChunk:
    """A derived chunk plus its exact half-open span in the parent article."""

    chunk: LegalChunk
    parent_relative_char_span: tuple[int, int]


class LegalChunkStructureError(ValueError):
    """Stable error for structured hierarchical chunk derivation."""


def derive_hierarchical_chunks(
    *,
    version_id: UUID,
    provisions: tuple[_ProvisionSource, ...],
    articles: tuple[_ArticleSource, ...],
    parser_version: str,
    max_leaf_chars: int = _DEFAULT_MAX_LEAF_CHARS,
    window_chars: int = _DEFAULT_WINDOW_CHARS,
    overlap_chars: int = _DEFAULT_OVERLAP_CHARS,
) -> tuple[LegalChunk, ...]:
    """Builds one PARENT chunk per article plus 款/项/目 CHILD chunks.

    The parent keeps the full article text; every child references the parent
    through ``parent_chunk_id`` so downstream indexing can treat the children as
    searchable leaves and restore the whole article from MySQL.
    """
    return tuple(
        located.chunk
        for located in derive_hierarchical_chunks_with_locations(
            version_id=version_id,
            provisions=provisions,
            articles=articles,
            parser_version=parser_version,
            max_leaf_chars=max_leaf_chars,
            window_chars=window_chars,
            overlap_chars=overlap_chars,
        )
    )


def derive_hierarchical_chunks_with_locations(
    *,
    version_id: UUID,
    provisions: tuple[_ProvisionSource, ...],
    articles: tuple[_ArticleSource, ...],
    parser_version: str,
    max_leaf_chars: int = _DEFAULT_MAX_LEAF_CHARS,
    window_chars: int = _DEFAULT_WINDOW_CHARS,
    overlap_chars: int = _DEFAULT_OVERLAP_CHARS,
) -> tuple[LocatedLegalChunk, ...]:
    """Build chunks and retain exact offsets relative to each parent article."""
    _validate_settings(
        max_leaf_chars=max_leaf_chars,
        window_chars=window_chars,
        overlap_chars=overlap_chars,
    )
    if not isinstance(provisions, tuple) or not provisions:
        raise LegalChunkStructureError(
            "provisions must be a non-empty tuple of typed provisions"
        )
    if not isinstance(articles, tuple) or not articles:
        raise LegalChunkStructureError(
            "articles must be a non-empty tuple of parsed articles"
        )
    if len(provisions) != len(articles):
        raise LegalChunkStructureError(
            "provisions and articles must correspond one to one"
        )
    if not isinstance(parser_version, str) or not parser_version.strip():
        raise LegalChunkStructureError("parser version must be non-empty text")

    paired = sorted(
        zip(provisions, articles, strict=True),
        key=lambda pair: (pair[0].char_start, pair[0].full_text),
    )
    result: list[LocatedLegalChunk] = []
    exact_positions = parser_version.endswith("/hierarchical-v2")
    exact_text = parser_version in EXACT_TEXT_CHUNK_PARSER_VERSIONS
    for provision, article in paired:
        parent = LegalChunk(
            id=new_uuid7(),
            version_id=version_id,
            provision_id=provision.id,
            chunk_type=ChunkType.PROVISION,
            quality=ChunkQuality.OK,
            content=provision.full_text,
            content_hash=content_sha256(provision.full_text),
            parent_chunk_id=None,
            parser_version=parser_version.strip(),
            parent_relative_char_start=0 if exact_positions else None,
            parent_relative_char_end=len(provision.full_text) if exact_positions else None,
        )
        result.append(LocatedLegalChunk(parent, (0, len(provision.full_text))))
        units = _units_for_article(
            provision,
            article,
            max_leaf_chars=max_leaf_chars,
            preserve_heading=exact_positions,
            exact_text=exact_text,
        )
        if exact_text and len(provision.full_text) > max_leaf_chars and not units:
            raise LegalChunkStructureError(
                "exact paragraph units must concatenate to the full provision text"
            )
        for unit in units:
            for piece, unit_start, unit_end in _window_pieces_with_offsets(
                unit.text,
                max_leaf_chars=max_leaf_chars,
                window_chars=window_chars,
                overlap_chars=overlap_chars,
            ):
                result.append(
                    LocatedLegalChunk(
                        chunk=LegalChunk(
                        id=new_uuid7(),
                        version_id=version_id,
                        provision_id=provision.id,
                        chunk_type=unit.kind,
                        quality=ChunkQuality.OK,
                        content=piece,
                        content_hash=content_sha256(piece),
                        parent_chunk_id=parent.id,
                        parser_version=parser_version.strip(),
                        parent_relative_char_start=(
                            unit.parent_start + unit_start if exact_positions else None
                        ),
                        parent_relative_char_end=(
                            unit.parent_start + unit_end if exact_positions else None
                        ),
                    ),
                        parent_relative_char_span=(
                            unit.parent_start + unit_start,
                            unit.parent_start + unit_end,
                        ),
                    )
                )
    return tuple(result)


def _validate_settings(
    *,
    max_leaf_chars: int,
    window_chars: int,
    overlap_chars: int,
) -> None:
    for value, name in (
        (max_leaf_chars, "max_leaf_chars"),
        (window_chars, "window_chars"),
        (overlap_chars, "overlap_chars"),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise LegalChunkStructureError(f"{name} must be a positive integer")
    if overlap_chars >= window_chars:
        raise LegalChunkStructureError(
            "overlap_chars must be smaller than window_chars"
        )
    if max_leaf_chars < window_chars:
        raise LegalChunkStructureError(
            "max_leaf_chars must not be smaller than window_chars"
        )


def _units_for_article(
    provision: _ProvisionSource,
    article: _ArticleSource,
    *,
    max_leaf_chars: int,
    preserve_heading: bool = False,
    exact_text: bool = False,
) -> tuple[_Unit, ...]:
    if len(provision.full_text) <= max_leaf_chars:
        return ()
    paragraphs = article.paragraphs or ()
    if exact_text:
        if not paragraphs or "".join(paragraphs) != provision.full_text:
            return ()
        exact_units: list[_Unit] = []
        parent_cursor = 0
        for paragraph_text in paragraphs:
            if not paragraph_text:
                continue
            classification_text = paragraph_text.lstrip()
            if _SUB_NUMBER.match(classification_text):
                kind = ChunkType.SUB_ITEM
            elif _ITEM_PAREN.match(classification_text) or _ITEM_CN.match(
                classification_text
            ):
                kind = ChunkType.ITEM
            else:
                kind = ChunkType.PARAGRAPH
            exact_units.append(
                _Unit(
                    kind=kind,
                    text=paragraph_text,
                    parent_start=parent_cursor,
                )
            )
            parent_cursor += len(paragraph_text)
        return tuple(exact_units)
    stripped_paragraphs = tuple(paragraph.strip() for paragraph in paragraphs)
    if (
        not stripped_paragraphs
        or any(not paragraph for paragraph in stripped_paragraphs)
        or "".join(stripped_paragraphs) != provision.full_text.strip()
    ):
        return ()
    units: list[_Unit] = []
    parent_cursor = 0
    for index, paragraph_text in enumerate(stripped_paragraphs):
        paragraph_start = provision.full_text.find(paragraph_text, parent_cursor)
        if paragraph_start < 0:
            return ()
        parent_cursor = paragraph_start + len(paragraph_text)
        text = paragraph_text
        unit_start = paragraph_start
        if index == 0 and not preserve_heading:
            heading = ARTICLE_HEADING.match(paragraph_text)
            if heading is not None:
                remainder = paragraph_text[heading.end():]
                leading_space = len(remainder) - len(remainder.lstrip())
                unit_start = paragraph_start + heading.end() + leading_space
                text = remainder.lstrip()
        if not text:
            continue
        if _SUB_NUMBER.match(text):
            kind = ChunkType.SUB_ITEM
        elif _ITEM_PAREN.match(text) or _ITEM_CN.match(text):
            kind = ChunkType.ITEM
        else:
            kind = ChunkType.PARAGRAPH
        units.append(_Unit(kind=kind, text=text, parent_start=unit_start))
    return tuple(units)


def _window_pieces(
    text: str,
    *,
    max_leaf_chars: int,
    window_chars: int,
    overlap_chars: int,
) -> tuple[str, ...]:
    """Splits a leaf unit that is longer than ``max_leaf_chars`` into windows.

    Windows never cross units by construction (they are applied per unit);
    boundaries prefer the last sentence end inside the overlap region.
    """
    return tuple(
        piece
        for piece, _, _ in _window_pieces_with_offsets(
            text,
            max_leaf_chars=max_leaf_chars,
            window_chars=window_chars,
            overlap_chars=overlap_chars,
        )
    )


def _window_pieces_with_offsets(
    text: str,
    *,
    max_leaf_chars: int,
    window_chars: int,
    overlap_chars: int,
) -> tuple[tuple[str, int, int], ...]:
    if len(text) <= max_leaf_chars:
        return ((text, 0, len(text)),)
    pieces: list[tuple[str, int, int]] = []
    start = 0
    length = len(text)
    step = window_chars - overlap_chars
    while start < length:
        end = min(start + window_chars, length)
        if end < length:
            low = max(start + 1, end - overlap_chars)
            boundary = max(
                (text.rfind(marker, low, end) for marker in _SENTENCE_END),
                default=-1,
            )
            if boundary >= low:
                end = boundary + 1
        pieces.append((text[start:end], start, end))
        if end >= length:
            break
        start = max(start + step, end - overlap_chars)
    return tuple(pieces)
