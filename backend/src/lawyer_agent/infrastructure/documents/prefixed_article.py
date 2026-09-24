"""Conservative v4 classification of approved prefixed article headings."""

from __future__ import annotations

import re
from dataclasses import dataclass

from lawyer_agent.application.legal_corpus_publish import _ARTICLE_NO, _cn_number
from lawyer_agent.domain.legal_article_heading import ARTICLE_HEADING
from lawyer_agent.infrastructure.documents.loader import ParsedDocument

_PREFIX = re.compile(
    r"^(?:(?:\ue004|（）)[^\S\r\n\v\f\u2028\u2029]+"
    r"|\u200b{1,8}[^\S\r\n\v\f\x1c-\x1f\x85\u2028\u2029]*(?=第))"
)
_BODY_AFTER_SEPARATOR = re.compile(r"^[^\S\r\n\v\f\u2028\u2029]+\S")
_ZERO_WIDTH_ANCHOR_BODY = re.compile(r"[^\S\r\n\v\f\x1c-\x1f\x85\u2028\u2029]+\S")
_STRUCTURAL_BARRIER = re.compile(
    r"^\s*(?:序言|第[一二三四五六七八九十百千0-9０-９]+[编章节])"
)
_PAIRS = {
    "“": "”", "‘": "’", "《": "》", "（": "）", "(": ")",
    "【": "】", "[": "]", "{": "}", "「": "」", "『": "』", "〔": "〕",
}
_CLOSE_TO_OPEN = {close: open_ for open_, close in _PAIRS.items()}
_SYMMETRIC_QUOTES = {'"', "'"}


@dataclass(frozen=True, slots=True)
class PrefixedArticleSpan:
    paragraph_index: int
    paragraph_ordinal: int
    marker_start: int
    prefix: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    span: PrefixedArticleSpan
    number: int
    boundary_is_clear: bool


def detect_prefixed_article_spans(
    document: ParsedDocument,
    *,
    excluded_paragraphs: set[int] | None = None,
) -> tuple[PrefixedArticleSpan, ...]:
    """Return candidates proven by adjacent ordinary article-number anchors."""
    excluded = excluded_paragraphs or set()
    anchors: list[tuple[int, int, bool]] = []
    candidates: dict[int, _Candidate] = {}
    boundary_stack: list[str] = []
    boundary_is_certain = True

    for index, paragraph in enumerate(document.paragraphs):
        text = paragraph.text
        if index not in excluded:
            candidate = _candidate(
                index,
                paragraph.ordinal,
                text,
                not boundary_stack and boundary_is_certain,
            )
            if candidate is not None:
                candidates[index] = candidate
            else:
                number = _ordinary_article_number(text)
                if number is not None:
                    anchors.append(
                        (index, number, not boundary_stack and boundary_is_certain)
                    )
        boundary_is_certain = _scan_boundaries(text, boundary_stack, boundary_is_certain)

    accepted: list[PrefixedArticleSpan] = []
    for (
        left_index, left_number, left_boundary_is_clear,
    ), (
        right_index, right_number, right_boundary_is_clear,
    ) in zip(
        anchors, anchors[1:], strict=False,
    ):
        group = [
            candidate
            for index, candidate in candidates.items()
            if left_index < index < right_index
        ]
        if (
            not group
            or not left_boundary_is_clear
            or not right_boundary_is_clear
            or not all(item.boundary_is_clear for item in group)
        ):
            continue
        # The new zero-width grammar requires real body-separated anchors;
        # preserve the historical anchor contract for legacy prefix groups.
        if any(item.span.prefix.startswith("\u200b") for item in group) and not all(
            _is_zero_width_anchor(document.paragraphs[index].text)
            for index in (left_index, right_index)
        ):
            continue
        if any(
            (
                _STRUCTURAL_BARRIER.match(document.paragraphs[index].text)
                or _is_supplementary_article_heading(document.paragraphs[index].text)
            )
            for index in range(left_index + 1, right_index)
            if index not in candidates
        ):
            continue
        if right_number - left_number - 1 != len(group):
            continue
        if all(
            item.number == left_number + offset
            for offset, item in enumerate(group, start=1)
        ):
            accepted.extend(item.span for item in group)
    return tuple(accepted)


def _candidate(
    paragraph_index: int,
    paragraph_ordinal: int,
    text: str,
    boundary_is_clear: bool,
) -> _Candidate | None:
    prefix_match = _PREFIX.match(text)
    if prefix_match is None:
        return None
    marker_start = prefix_match.end()
    view = text[marker_start:]
    heading = ARTICLE_HEADING.match(view)
    if heading is None or heading.group("supplement") is not None:
        return None
    marker_end = heading.end("marker")
    tail = view[marker_end:]
    if _BODY_AFTER_SEPARATOR.match(tail) is None:
        return None
    number = _marker_number(heading.group("marker"))
    if number is None:
        return None
    return _Candidate(
        span=PrefixedArticleSpan(
            paragraph_index=paragraph_index,
            paragraph_ordinal=paragraph_ordinal,
            marker_start=marker_start,
            prefix=text[:marker_start],
        ),
        number=number,
        boundary_is_clear=boundary_is_clear,
    )


def _ordinary_article_number(text: str) -> int | None:
    stripped = text.strip()
    heading = ARTICLE_HEADING.match(stripped)
    if heading is None or heading.group("supplement") is not None:
        return None
    return _marker_number(heading.group("marker"))


def _is_zero_width_anchor(text: str) -> bool:
    stripped = text.strip()
    heading = ARTICLE_HEADING.match(stripped)
    return (
        heading is not None
        and heading.group("supplement") is None
        and _ZERO_WIDTH_ANCHOR_BODY.match(stripped, heading.end("marker")) is not None
    )


def _is_supplementary_article_heading(text: str) -> bool:
    prefix_match = _PREFIX.match(text)
    view = text[prefix_match.end():] if prefix_match is not None else text
    heading = ARTICLE_HEADING.match(view)
    return heading is not None and heading.group("supplement") is not None


def _marker_number(marker: str) -> int | None:
    normalized = re.sub(r"\s+", "", marker)
    match = _ARTICLE_NO.fullmatch(normalized)
    if match is None or match.group(2) is not None:
        return None
    return _cn_number(match.group(1))


def _scan_boundaries(text: str, stack: list[str], certain: bool) -> bool:
    for character in text:
        if character in _SYMMETRIC_QUOTES:
            if stack and stack[-1] == character:
                stack.pop()
            else:
                stack.append(character)
        elif character in _PAIRS:
            stack.append(character)
        elif character in _CLOSE_TO_OPEN:
            if not stack or stack[-1] != _CLOSE_TO_OPEN[character]:
                certain = False
            else:
                stack.pop()
    return certain
