"""Conservative leading-title classification for explicitly versioned parsers."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from lawyer_agent.domain.legal_article_heading import ARTICLE_HEADING
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph

_MAX_NONEMPTY_PARAGRAPHS = 12
_MAX_CHARACTERS = 4096
_LINE_BREAK = re.compile(r"[\r\n\v\u2028\u2029]+")
_DATE = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日")
_HISTORY_ACTION = re.compile(r"通过|批准|公布")
_LAW_NUMBER = re.compile(r"^法释(?P<open>〔|\[|【)\d{4}(?P<close>〕|\]|】)\d+号$")
_REFERENCE_BOUNDARY = re.compile(r"《[^《》]+》|<[^<>]+>")
_BODY_SIGNAL = re.compile(r"[。；;：:]|[“”‘’\"']|(?<!基)本法|本条例|本解释|为了|应当")
_TITLE_TAIL = re.compile(
    r"^第[一二三四五六七八九十百千零〇0-9０-９]+条"
    r"(?:第[一二三四五六七八九十百千零〇0-9０-９]+款)?"
    r"(?:[、和]第[一二三四五六七八九十百千零〇0-9０-９]+条"
    r"(?:第[一二三四五六七八九十百千零〇0-9０-９]+款)?)*"
    r"(?:的解释|(?:使用|适用)问题的解释|处罚权限规定的决定|规定的决定|"
    r"有关问题的批复|的批复)$"
)
_BRACKET_CLOSE = {"（": "）", "(": ")"}
_BRACKET_OPEN = {close: open_ for open_, close in _BRACKET_CLOSE.items()}
_LAW_NUMBER_CLOSE = {"〔": "〕", "[": "]", "【": "】"}


@dataclass(frozen=True, slots=True)
class LeadingTitleSpan:
    paragraph_index: int
    paragraph_ordinal: int
    start: int
    end: int
    text: str

    @property
    def ordinal(self) -> int:
        """Compatibility alias; ordinals are source facts, not identifiers."""
        return self.paragraph_ordinal


def detect_leading_title_spans(document: ParsedDocument) -> tuple[LeadingTitleSpan, ...]:
    all_nonempty = [
        (index, paragraph)
        for index, paragraph in enumerate(document.paragraphs)
        if paragraph.text.strip()
    ]
    if not all_nonempty:
        return ()
    nonempty = all_nonempty[:_MAX_NONEMPTY_PARAGRAPHS]

    first_line = next(
        (line.strip() for line in _LINE_BREAK.split(nonempty[0][1].text) if line.strip()), ""
    )
    if ARTICLE_HEADING.match(first_line):
        return ()

    history_range = _find_history_range(nonempty)
    if history_range is None or history_range[0] == 0:
        return ()
    history_start, history_end = history_range
    if sum(len(paragraph.text) for _, paragraph in nonempty[:history_end]) > _MAX_CHARACTERS:
        return ()
    title_end = history_start
    law_numbers = [
        offset
        for offset in range(history_start)
        if _is_law_number(nonempty[offset][1].text.strip())
    ]
    if law_numbers:
        if law_numbers != [history_start - 1]:
            return ()
        title_end -= 1
    if title_end <= 0:
        return ()

    title_items = nonempty[:title_end]
    normalized = "".join(re.sub(r"\s+", "", paragraph.text) for _, paragraph in title_items)
    if "关于" not in normalized or _REFERENCE_BOUNDARY.search(normalized) is None:
        return ()
    if _BODY_SIGNAL.search(normalized):
        return ()
    if not normalized.endswith(("解释", "决定", "批复")):
        return ()

    article_segments: list[str] = []
    for _, paragraph in title_items:
        for line in _LINE_BREAK.split(paragraph.text):
            candidate = line.strip()
            if ARTICLE_HEADING.match(candidate):
                article_segments.append(re.sub(r"\s+", "", candidate))
    if not article_segments or any(
        _TITLE_TAIL.fullmatch(segment) is None
        for segment in article_segments
    ):
        return ()

    return tuple(
        LeadingTitleSpan(
            paragraph_index=index,
            paragraph_ordinal=paragraph.ordinal,
            start=0,
            end=len(paragraph.text),
            text=paragraph.text,
        )
        for index, paragraph in title_items
    )


def _find_history_range(
    nonempty: Sequence[tuple[int, ParsedParagraph]],
) -> tuple[int, int] | None:
    for start in range(1, len(nonempty)):
        for count in range(1, 4):
            group = nonempty[start:start + count]
            if len(group) != count:
                break
            texts = [paragraph.text.strip() for _, paragraph in group]
            combined = "".join(texts)
            if _is_history(combined, texts):
                return start, start + count
    return None


def _is_history(combined: str, paragraphs: list[str]) -> bool:
    if not combined or combined[0] not in _BRACKET_CLOSE:
        return False
    if not _has_balanced_history_brackets(combined):
        return False
    if any(
        ARTICLE_HEADING.match(line.strip())
        for paragraph in paragraphs
        for line in _LINE_BREAK.split(paragraph)
        if line.strip()
    ):
        return False
    return _DATE.search(combined) is not None and _HISTORY_ACTION.search(combined) is not None


def _has_balanced_history_brackets(text: str) -> bool:
    stack: list[str] = []
    for character in text:
        if character in _BRACKET_CLOSE:
            stack.append(character)
        elif character in _BRACKET_OPEN:
            if not stack or stack.pop() != _BRACKET_OPEN[character]:
                return False
    return not stack


def _is_law_number(text: str) -> bool:
    match = _LAW_NUMBER.fullmatch(text)
    return bool(match and _LAW_NUMBER_CLOSE[match.group("open")] == match.group("close"))
