"""Conservative v4 recovery of article heads embedded in one source segment."""

from __future__ import annotations

import re
from dataclasses import dataclass

from lawyer_agent.domain.legal_article_heading import (
    ARTICLE_HEADING,
    ARTICLE_SUPPLEMENT_CANDIDATE,
)
from lawyer_agent.infrastructure.documents.loader import ParsedDocument
from lawyer_agent.infrastructure.documents.prefixed_article import (
    PrefixedArticleSpan,
    _marker_number,
    _scan_boundaries,
)

_LINE_BREAK = re.compile(r"[\r\n\v\u2028\u2029]+")
_HORIZONTAL = r"[^\S\r\n\v\f\u2028\u2029]"
_SENTENCE_BOUNDARY = re.compile(rf"。(?P<separator>{_HORIZONTAL}*(?:\ue004{_HORIZONTAL}*)?)")
_BODY_AFTER_MARKER = re.compile(rf"^{_HORIZONTAL}+\S")
_STRUCTURAL_BARRIER = re.compile(
    r"^\s*(?:序言|第[一二三四五六七八九十百千0-9０-９]+[编章节])"
)
_REFERENCE_AFTER_MARKER = re.compile(
    r"^(?:的规定|第?[（(]?[一二三四五六七八九十百千零〇0-9０-９]+"
    r"[）)]?[款项]|第[一二三四五六七八九十百千零〇0-9０-９]+款)"
)

# These bounded variants stay mechanically tied to the shared grammar while
# allowing matches at an original-string offset (``^`` ignores ``re.Pattern.pos``).
_ARTICLE_HEADING_AT = re.compile(ARTICLE_HEADING.pattern.removeprefix("^"))
_ARTICLE_SUPPLEMENT_AT = re.compile(
    ARTICLE_SUPPLEMENT_CANDIDATE.pattern.removeprefix("^")
)
_BODY_AFTER_MARKER_AT = re.compile(_BODY_AFTER_MARKER.pattern.removeprefix("^"))
_STRUCTURAL_BARRIER_AT = re.compile(_STRUCTURAL_BARRIER.pattern.removeprefix("^"))
_REFERENCE_AFTER_MARKER_AT = re.compile(
    _REFERENCE_AFTER_MARKER.pattern.removeprefix("^")
)


@dataclass(frozen=True, slots=True)
class EmbeddedArticleSpan:
    paragraph_index: int
    paragraph_ordinal: int
    marker_start: int
    split_start: int
    left_separator: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    span: EmbeddedArticleSpan
    number: int


@dataclass(frozen=True, slots=True)
class _Event:
    kind: str
    number: int | None = None
    candidate: _Candidate | None = None


def detect_embedded_article_spans(
    document: ParsedDocument,
    *,
    excluded_paragraphs: set[int] | None = None,
    prefixed_spans: tuple[PrefixedArticleSpan, ...] = (),
) -> tuple[EmbeddedArticleSpan, ...]:
    """Return original offsets for exact candidate runs closed by ordinary anchors."""
    excluded = excluded_paragraphs or set()
    prefixed_markers = {span.paragraph_index: span.marker_start for span in prefixed_spans}
    events: list[_Event] = []
    boundary_stack: list[str] = []
    boundary_is_certain = True

    for paragraph_index, paragraph in enumerate(document.paragraphs):
        if paragraph_index in excluded:
            events.append(_Event("barrier"))
            boundary_is_certain = _scan_boundaries(
                paragraph.text, boundary_stack, boundary_is_certain
            )
            continue
        line_start = 0
        boundaries = (*_LINE_BREAK.finditer(paragraph.text), None)
        for boundary in boundaries:
            line_end = boundary.start() if boundary is not None else len(paragraph.text)
            marker_override = (
                prefixed_markers.get(paragraph_index) if line_start == 0 else None
            )
            view_start = marker_override if marker_override is not None else line_start
            event = _start_event(
                paragraph.text,
                view_start,
                line_end,
                not boundary_stack and boundary_is_certain,
                prefixed_is_accepted=marker_override is not None,
            )
            if event is not None:
                events.append(event)

            scan_at = line_start
            for match in _SENTENCE_BOUNDARY.finditer(
                paragraph.text, line_start, line_end
            ):
                marker_start = match.end("separator")
                boundary_is_certain = _scan_boundaries(
                    paragraph.text[scan_at:marker_start],
                    boundary_stack,
                    boundary_is_certain,
                )
                scan_at = marker_start
                candidate = _embedded_candidate(
                    paragraph_index=paragraph_index,
                    paragraph_ordinal=paragraph.ordinal,
                    paragraph_text=paragraph.text,
                    marker_start=marker_start,
                    line_end=line_end,
                    left_separator=match.group("separator"),
                )
                if candidate is None or boundary_stack or not boundary_is_certain:
                    if _looks_like_article_boundary(
                        paragraph.text, marker_start, line_end
                    ):
                        events.append(_Event("barrier"))
                    continue
                events.append(_Event("candidate", candidate.number, candidate))
            boundary_is_certain = _scan_boundaries(
                paragraph.text[scan_at:line_end], boundary_stack, boundary_is_certain
            )
            if boundary is None:
                break
            boundary_is_certain = _scan_boundaries(
                boundary.group(), boundary_stack, boundary_is_certain
            )
            line_start = boundary.end()

    accepted: list[EmbeddedArticleSpan] = []
    left_number: int | None = None
    pending: list[_Candidate] = []
    for event in events:
        if event.kind == "barrier":
            left_number = None
            pending = []
        elif event.kind == "candidate":
            if left_number is not None and event.candidate is not None:
                pending.append(event.candidate)
        elif event.kind == "anchor" and event.number is not None:
            if left_number is not None and pending and _is_exact_run(
                left_number, pending, event.number
            ):
                accepted.extend(candidate.span for candidate in pending)
            left_number = event.number
            pending = []
    return tuple(accepted)


def _start_event(
    text: str,
    start: int,
    end: int,
    boundary_is_clear: bool,
    *,
    prefixed_is_accepted: bool,
) -> _Event | None:
    start, end = _trim_bounds(text, start, end)
    if start == end:
        return None
    if not boundary_is_clear:
        if _ARTICLE_HEADING_AT.match(text, start, end):
            return _Event("barrier")
        return None
    if text.startswith("\ue004", start, end):
        if not prefixed_is_accepted:
            return _Event("barrier")
        start = _skip_whitespace(text, start + 1, end)
    heading = _ARTICLE_HEADING_AT.match(text, start, end)
    if heading is not None:
        if heading.group("supplement") is not None:
            return _Event("barrier")
        marker_end = heading.end("marker")
        if (
            _BODY_AFTER_MARKER_AT.match(text, marker_end, end) is None
            or _REFERENCE_AFTER_MARKER_AT.match(
                text, _skip_whitespace(text, marker_end, end), end
            )
        ):
            return _Event("barrier")
        number = _marker_number(heading.group("marker"))
        return _Event("anchor", number) if number is not None else _Event("barrier")
    if _STRUCTURAL_BARRIER_AT.match(
        text, start, end
    ) or _ARTICLE_SUPPLEMENT_AT.match(text, start, end):
        return _Event("barrier")
    return None


def _embedded_candidate(
    *,
    paragraph_index: int,
    paragraph_ordinal: int,
    paragraph_text: str,
    marker_start: int,
    line_end: int,
    left_separator: str,
) -> _Candidate | None:
    if not paragraph_text.startswith("第", marker_start, line_end):
        return None
    heading = _ARTICLE_HEADING_AT.match(paragraph_text, marker_start, line_end)
    if heading is None or heading.group("supplement") is not None:
        return None
    marker_end = heading.end("marker")
    if _BODY_AFTER_MARKER_AT.match(paragraph_text, marker_end, line_end) is None:
        return None
    reference_start = _skip_whitespace(paragraph_text, marker_end, line_end)
    if _REFERENCE_AFTER_MARKER_AT.match(paragraph_text, reference_start, line_end):
        return None
    number = _marker_number(heading.group("marker"))
    if number is None:
        return None
    span = EmbeddedArticleSpan(
        paragraph_index=paragraph_index,
        paragraph_ordinal=paragraph_ordinal,
        marker_start=marker_start,
        split_start=marker_start,
        left_separator=left_separator,
    )
    return _Candidate(span=span, number=number)


def _looks_like_article_boundary(text: str, start: int, end: int) -> bool:
    if not text.startswith("第", start, end):
        return False
    return bool(
        _ARTICLE_HEADING_AT.match(text, start, end)
        or _ARTICLE_SUPPLEMENT_AT.match(text, start, end)
    )


def _trim_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def _skip_whitespace(text: str, start: int, end: int) -> int:
    while start < end and text[start].isspace():
        start += 1
    return start


def _is_exact_run(left: int, candidates: list[_Candidate], right: int) -> bool:
    if right - left - 1 != len(candidates):
        return False
    return all(candidate.number == left + offset for offset, candidate in enumerate(candidates, 1))
