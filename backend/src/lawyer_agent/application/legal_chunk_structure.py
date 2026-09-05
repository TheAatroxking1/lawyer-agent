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
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    content_sha256,
)

_ARTICLE_PREFIX = re.compile(
    r"^\s*第[一二三四五六七八九十百千0-9０-９]+条[\s　]*"
)
_ITEM_PAREN = re.compile(r"^[（(]\s*[一二三四五六七八九十百]+[)）]")
_ITEM_CN = re.compile(r"^[一二三四五六七八九十百]+、")
_SUB_NUMBER = re.compile(r"^[0-9０-９]+[\.．、]")
_SENTENCE_END = "。！？；;"

_DEFAULT_MAX_LEAF_CHARS = 600
_DEFAULT_WINDOW_CHARS = 400
_DEFAULT_OVERLAP_CHARS = 60


class _ProvisionSource(Protocol):
    id: UUID
    full_text: str
    char_start: int


class _ArticleSource(Protocol):
    provision_no: str
    structure_path: tuple[str, ...]
    paragraphs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Unit:
    kind: ChunkType
    text: str


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
    result: list[LegalChunk] = []
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
        )
        result.append(parent)
        units = _units_for_article(provision, article)
        for unit in units:
            for piece in _window_pieces(
                unit.text,
                max_leaf_chars=max_leaf_chars,
                window_chars=window_chars,
                overlap_chars=overlap_chars,
            ):
                result.append(
                    LegalChunk(
                        id=new_uuid7(),
                        version_id=version_id,
                        provision_id=provision.id,
                        chunk_type=unit.kind,
                        quality=ChunkQuality.OK,
                        content=piece,
                        content_hash=content_sha256(piece),
                        parent_chunk_id=parent.id,
                        parser_version=parser_version.strip(),
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
    provision: _ProvisionSource, article: _ArticleSource
) -> tuple[_Unit, ...]:
    paragraphs = article.paragraphs or ()
    if not paragraphs:
        body = _strip_article_prefix(provision.full_text)
        return (_Unit(kind=ChunkType.PARAGRAPH, text=body),)
    units: list[_Unit] = []
    for index, paragraph in enumerate(paragraphs):
        text = paragraph.strip()
        if not text:
            continue
        if index == 0:
            text = _strip_article_prefix(text)
        if not text:
            continue
        if _SUB_NUMBER.match(text):
            kind = ChunkType.SUB_ITEM
        elif _ITEM_PAREN.match(text) or _ITEM_CN.match(text):
            kind = ChunkType.ITEM
        else:
            kind = ChunkType.PARAGRAPH
        units.append(_Unit(kind=kind, text=text))
    return tuple(units)


def _strip_article_prefix(text: str) -> str:
    return _ARTICLE_PREFIX.sub("", text, count=1).strip()


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
    if len(text) <= max_leaf_chars:
        return (text,)
    pieces: list[str] = []
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
        pieces.append(text[start:end])
        if end >= length:
            break
        start = max(start + step, end - overlap_chars)
    return tuple(pieces)
