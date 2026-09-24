"""Structural parser for legal instruments.

Classifies paragraphs as part/chapter/section/article headings and article
bodies, building provision-level structure without cross-article joining.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from lawyer_agent.domain.legal_article_heading import (
    ARTICLE_HEADING,
    ARTICLE_SUPPLEMENT_CANDIDATE,
)
from lawyer_agent.domain.legal_parser_profiles import EXACT_TEXT_PARSER_VERSIONS
from lawyer_agent.infrastructure.documents.embedded_article import (
    EmbeddedArticleSpan,
    detect_embedded_article_spans,
)
from lawyer_agent.infrastructure.documents.leading_title import (
    LeadingTitleSpan,
    detect_leading_title_spans,
)
from lawyer_agent.infrastructure.documents.loader import ParsedDocument
from lawyer_agent.infrastructure.documents.prefixed_article import (
    PrefixedArticleSpan,
    detect_prefixed_article_spans,
)
from lawyer_agent.infrastructure.documents.spaced_article import (
    SpacedArticleSpan,
    detect_spaced_article_spans,
)

_SECTION_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+节\s*")
_CHAPTER_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+章\s*")
_PART_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+编\s*")
_PREAMBLE = "序言"
_SUBARTICLE_REFERENCE = re.compile(
    r"^第(?:(?:[（(][一二三四五六七八九十百千零〇0-9０-９]+[）)]、)*"
    r"第[（(][一二三四五六七八九十百千零〇0-9０-９]+[）)]项|"
    r"[（(][一二三四五六七八九十百千零〇0-9０-９]+[）)][款项]|"
    r"[一二三四五六七八九十百千零〇0-9０-９]+[款项])"
)
_REFERENCE_NUMBER = r"[一二三四五六七八九十百千零〇0-9０-９]+"
_PARENTHESIZED_ITEM = rf"(?:（{_REFERENCE_NUMBER}）|\({_REFERENCE_NUMBER}\))"
_REPEATED_ITEM_REFERENCE = re.compile(
    rf"^第{_PARENTHESIZED_ITEM}(?:、第{_PARENTHESIZED_ITEM})+[款项]"
)
_REPEATED_ITEM_CANDIDATE = re.compile(rf"^第[（(]{_REFERENCE_NUMBER}[）)]、第")


@dataclass(frozen=True, slots=True)
class ParsedHeading:
    level: str  # part | chapter | section | article | preamble | other
    heading: str


@dataclass(frozen=True, slots=True)
class ParsedArticle:
    provision_no: str
    structure_path: tuple[str, ...]
    text: str
    char_start: int
    char_end: int
    paragraphs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParsedInstrument:
    paragraphs: tuple[ParsedHeading, ...]
    articles: tuple[ParsedArticle, ...]
    leading_title_spans: tuple[LeadingTitleSpan, ...] = ()
    prefixed_article_spans: tuple[PrefixedArticleSpan, ...] = ()
    embedded_article_spans: tuple[EmbeddedArticleSpan, ...] = ()
    spaced_article_spans: tuple[SpacedArticleSpan, ...] = ()


@dataclass(frozen=True, slots=True)
class _ArticleSegment:
    text: str
    is_title: bool
    classification_start: int | None = None
    spaced_marker: SpacedArticleSpan | None = None


class LegalStructureParser:
    """Turns a parsed document into headings plus complete articles.

    Every article keeps the *paragraphs* it was built from (body pieces in
    order) so downstream hierarchical chunking can split 款/项/目 at real
    paragraph boundaries instead of re-inferring them from joined text.
    """

    def __init__(self, *, profile: str | None = None) -> None:
        self._profile = profile

    def parse(self, document: ParsedDocument) -> ParsedInstrument:
        part: str | None = None
        chapter: str | None = None
        section: str | None = None
        headings: list[ParsedHeading] = []
        articles: list[ParsedArticle] = []
        current_no: str | None = None
        current_path: tuple[str, ...] = ()
        current_start = 0
        current_pieces: list[str] = []
        cursor = 0

        def flush() -> None:
            nonlocal current_no, current_path, current_start, current_pieces
            if current_no is None:
                return
            text = "".join(current_pieces)
            articles.append(
                ParsedArticle(
                    provision_no=current_no,
                    structure_path=current_path,
                    text=text,
                    char_start=current_start,
                    char_end=current_start + len(text),
                    paragraphs=tuple(current_pieces),
                )
            )
            current_no = None
            current_path = ()
            current_start = 0
            current_pieces = []

        title_spans = (
            detect_leading_title_spans(document)
            if self._profile in EXACT_TEXT_PARSER_VERSIONS
            else ()
        )
        title_paragraphs = {span.paragraph_index for span in title_spans}
        prefixed_spans = (
            detect_prefixed_article_spans(
                document,
                excluded_paragraphs=title_paragraphs,
            )
            if self._profile in EXACT_TEXT_PARSER_VERSIONS
            else ()
        )
        prefixed_markers = {
            span.paragraph_index: span.marker_start for span in prefixed_spans
        }
        embedded_spans = (
            detect_embedded_article_spans(
                document,
                excluded_paragraphs=title_paragraphs,
                prefixed_spans=prefixed_spans,
            )
            if self._profile in EXACT_TEXT_PARSER_VERSIONS
            else ()
        )
        spaced_spans = (
            detect_spaced_article_spans(document, excluded_paragraphs=title_paragraphs)
            if self._profile in EXACT_TEXT_PARSER_VERSIONS
            else ()
        )
        for segment in _article_segments(
            document, title_paragraphs, prefixed_markers, embedded_spans, spaced_spans,
            repeated_item_references=self._profile == "corpus-docx-v5",
        ):
            text = segment.text
            if not text:
                continue
            if segment.is_title:
                cursor += len(text)
                continue
            classification_text = (
                text[segment.classification_start:]
                if segment.classification_start is not None
                else text
            )
            if segment.spaced_marker is not None:
                span = segment.spaced_marker
                classification_text = span.normalized_marker + text[len(span.original_marker):]
            heading_match = _classify_heading(
                classification_text, repeated_item_references=self._profile == "corpus-docx-v5",
            )
            if heading_match is not None:
                level, heading_text = heading_match
                if level == "article":
                    flush()
                    provision_no = _article_number(classification_text)
                    current_path = tuple(
                        value for value in (part, chapter, section) if value
                    )
                    current_no = provision_no
                    current_start = cursor
                    current_pieces = [text]
                else:
                    flush()
                    if level == "part":
                        part = heading_text
                        chapter = None
                        section = None
                    elif level == "chapter":
                        chapter = heading_text
                        section = None
                    elif level == "section":
                        section = heading_text
                    headings.append(ParsedHeading(level, heading_text))
                cursor += len(text)
                continue
            # Continuation of an article body or preamble content.
            if ARTICLE_SUPPLEMENT_CANDIDATE.match(text):
                # Do not guess whether this is a compact supplement heading
                # or a reference, and never merge a possible article boundary.
                raise ValueError("ambiguous supplement article heading")
            if current_no is not None:
                current_pieces.append(text)
            cursor += len(text)

        flush()
        return ParsedInstrument(
            paragraphs=tuple(headings),
            articles=tuple(articles),
            leading_title_spans=title_spans,
            prefixed_article_spans=prefixed_spans,
            embedded_article_spans=embedded_spans,
            spaced_article_spans=spaced_spans,
        )


def _article_segments(
    document: ParsedDocument,
    title_paragraphs: set[int] | None = None,
    prefixed_markers: dict[int, int] | None = None,
    embedded_spans: tuple[EmbeddedArticleSpan, ...] = (),
    spaced_spans: tuple[SpacedArticleSpan, ...] = (),
    *,
    repeated_item_references: bool = False,
) -> Iterator[_ArticleSegment]:
    """Split only explicit article heads following a source paragraph's line break.

    Other internal newlines remain byte-for-byte unchanged. A compact marker at
    such a boundary can also be a reference, so reject it rather than guessing.
    """
    title_paragraphs = title_paragraphs or set()
    prefixed_markers = prefixed_markers or {}
    spaced_markers = {span.paragraph_index: span for span in spaced_spans}
    embedded_by_paragraph: dict[int, list[EmbeddedArticleSpan]] = {}
    for span in embedded_spans:
        embedded_by_paragraph.setdefault(span.paragraph_index, []).append(span)
    for paragraph_index, paragraph in enumerate(document.paragraphs):
        if paragraph_index in title_paragraphs:
            yield _ArticleSegment(paragraph.text.strip(), True)
            continue
        start = 0
        embedded = embedded_by_paragraph.get(paragraph_index, [])
        for boundary in re.finditer(r"[\r\n\v\u2028\u2029]+", paragraph.text):
            tail = paragraph.text[boundary.end():]
            if repeated_item_references and _repeated_item_reference(tail) is True:
                continue
            match = ARTICLE_HEADING.match(tail)
            if match is None:
                if ARTICLE_SUPPLEMENT_CANDIDATE.match(tail):
                    raise ValueError("ambiguous supplement article heading")
                continue
            after_marker = tail[match.end("marker"):]
            if after_marker and not (after_marker[0].isspace() or after_marker[0] == "【"):
                raise ValueError("ambiguous internal article heading")
            yield from _split_normalized_segment(
                paragraph.text,
                start,
                boundary.start(),
                prefixed_markers.get(paragraph_index) if start == 0 else None,
                embedded,
                spaced_markers.get(paragraph_index) if start == 0 else None,
            )
            start = boundary.end()
        yield from _split_normalized_segment(
            paragraph.text,
            start,
            len(paragraph.text),
            prefixed_markers.get(paragraph_index) if start == 0 else None,
            embedded,
            spaced_markers.get(paragraph_index) if start == 0 else None,
        )


def _split_normalized_segment(
    paragraph_text: str,
    raw_start: int,
    raw_end: int,
    classification_start: int | None,
    embedded_spans: list[EmbeddedArticleSpan],
    spaced_marker: SpacedArticleSpan | None = None,
) -> Iterator[_ArticleSegment]:
    raw = paragraph_text[raw_start:raw_end]
    leading = len(raw) - len(raw.lstrip())
    normalized = raw.strip()
    if not normalized:
        return
    normalized_start = raw_start + leading
    cuts = [
        span.marker_start - normalized_start
        for span in embedded_spans
        if normalized_start < span.marker_start < normalized_start + len(normalized)
    ]
    starts = [0, *cuts]
    ends = [*cuts, len(normalized)]
    adjusted_classification = (
        classification_start - normalized_start
        if classification_start is not None
        and normalized_start <= classification_start < normalized_start + len(normalized)
        else None
    )
    for index, (piece_start, piece_end) in enumerate(zip(starts, ends, strict=True)):
        marker = adjusted_classification if index == 0 else None
        yield _ArticleSegment(
            text=normalized[piece_start:piece_end],
            is_title=False,
            classification_start=(marker - piece_start if marker is not None else None),
            spaced_marker=spaced_marker if index == 0 else None,
        )


def _repeated_item_reference(text: str) -> bool | None:
    article = ARTICLE_HEADING.match(text)
    if article is None:
        return None
    tail = text[article.end("marker"):]
    if _REPEATED_ITEM_CANDIDATE.match(tail) is None:
        return None
    return _REPEATED_ITEM_REFERENCE.match(tail) is not None


def _classify_heading(
    text: str, *, repeated_item_references: bool = False,
) -> tuple[str, str] | None:
    if text.startswith(_PREAMBLE):
        return "preamble", text
    article = ARTICLE_HEADING.match(text)
    if repeated_item_references:
        reference = _repeated_item_reference(text)
        if reference is True:
            return None
        if reference is False:
            # A malformed repeated locator cannot use the old permissive
            # two-item grammar to hide a possible duplicate article boundary.
            return "article", text
    if article and not _SUBARTICLE_REFERENCE.match(text[article.end("marker"):]):
        return "article", text
    if _SECTION_HEADING.match(text):
        return "section", text
    if _CHAPTER_HEADING.match(text):
        return "chapter", text
    if _PART_HEADING.match(text):
        return "part", text
    return None


def _article_number(text: str) -> str:
    match = ARTICLE_HEADING.match(text)
    if match is None:
        return text[:8]
    raw = re.sub(r"\s+", "", match.group("marker"))
    if match.group("supplement") is not None:
        return raw
    # Preserve the existing numeric-only identity for ordinary Arabic/fullwidth
    # article numbers; supplements retain the full marker to avoid collisions.
    return re.sub(r"[^\d0-9０-９]+", "", raw) or raw
