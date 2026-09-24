"""Bounded v4 recovery of horizontal whitespace inside ordinary article numerals."""

from __future__ import annotations

import re
from dataclasses import dataclass

from lawyer_agent.domain.legal_article_heading import ARTICLE_HEADING
from lawyer_agent.infrastructure.documents.loader import ParsedDocument
from lawyer_agent.infrastructure.documents.prefixed_article import (
    _STRUCTURAL_BARRIER,
    _is_supplementary_article_heading,
    _marker_number,
    _ordinary_article_number,
    _scan_boundaries,
)

_HORIZONTAL = r"[^\S\r\n\v\f\x1c-\x1f\u0085\u2028\u2029]"
_NUMBER = r"[一二三四五六七八九十百千零〇0-9０-９]+"
_BODY_AFTER_MARKER = re.compile(rf"{_HORIZONTAL}+\S")
_SPACED_MARKER = re.compile(
    rf"^{_HORIZONTAL}*(?P<marker>第{_NUMBER}(?:{_HORIZONTAL}+{_NUMBER})+"
    rf"{_HORIZONTAL}*条)(?={_HORIZONTAL}+\S)"
)
_SPACED_SUPPLEMENT = re.compile(
    rf"^{_HORIZONTAL}*第{_NUMBER}(?:{_HORIZONTAL}+{_NUMBER})+{_HORIZONTAL}*条之"
)


@dataclass(frozen=True, slots=True)
class SpacedArticleSpan:
    paragraph_index: int
    paragraph_ordinal: int
    marker_start: int
    marker_end: int
    original_marker: str
    normalized_marker: str


def detect_spaced_article_spans(
    document: ParsedDocument,
    *,
    excluded_paragraphs: set[int] | None = None,
) -> tuple[SpacedArticleSpan, ...]:
    """Accept only consecutive runs strictly enclosed by ordinary paragraph anchors."""
    excluded = excluded_paragraphs or set()
    stack: list[str] = []
    certain = True
    left: int | None = None
    pending: list[tuple[int, SpacedArticleSpan]] = []
    accepted: list[SpacedArticleSpan] = []
    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text
        clear = not stack and certain
        match = _SPACED_MARKER.match(text)
        number = _ordinary_article_number(text)
        heading = ARTICLE_HEADING.match(text)
        anchor_has_body_separator = (
            heading is not None
            and _BODY_AFTER_MARKER.match(text, heading.end("marker")) is not None
        )
        barrier = (
            index in excluded
            or _STRUCTURAL_BARRIER.match(text) is not None
            or _is_supplementary_article_heading(text)
            or _SPACED_SUPPLEMENT.match(text) is not None
            or (heading is not None and not anchor_has_body_separator)
        )
        if barrier or (not clear and (match is not None or number is not None)):
            left = None
            pending = []
        elif match is not None:
            marker = match.group("marker")
            normalized = re.sub(_HORIZONTAL, "", marker)
            candidate_number = _marker_number(normalized)
            if candidate_number is None:
                left = None
                pending = []
            elif left is not None:
                pending.append((candidate_number, SpacedArticleSpan(
                    paragraph_index=index,
                    paragraph_ordinal=paragraph.ordinal,
                    marker_start=match.start("marker"),
                    marker_end=match.end("marker"),
                    original_marker=marker,
                    normalized_marker=normalized,
                )))
        elif number is not None:
            if (
                left is not None
                and pending
                and number - left - 1 == len(pending)
                and all(n == left + offset for offset, (n, _) in enumerate(pending, 1))
            ):
                accepted.extend(span for _, span in pending)
            left = number
            pending = []
        certain = _scan_boundaries(text, stack, certain)
    return tuple(accepted)
