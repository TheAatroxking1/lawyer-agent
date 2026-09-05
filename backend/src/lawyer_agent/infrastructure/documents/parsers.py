"""Structural parser for legal instruments.

Classifies paragraphs as part/chapter/section/article headings and article
bodies, building provision-level structure without cross-article joining.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lawyer_agent.infrastructure.documents.loader import ParsedDocument

_ARTICLE_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+条\s*")
_SECTION_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+节\s*")
_CHAPTER_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+章\s*")
_PART_HEADING = re.compile(r"^\s*第[一二三四五六七八九十百千0-9０-９]+编\s*")
_PREAMBLE = "序言"


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


class LegalStructureParser:
    """Turns a parsed document into headings plus complete articles.

    Every article keeps the *paragraphs* it was built from (body pieces in
    order) so downstream hierarchical chunking can split 款/项/目 at real
    paragraph boundaries instead of re-inferring them from joined text.
    """

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

        for paragraph in document.paragraphs:
            text = paragraph.text.strip()
            if not text:
                continue
            heading_match = _classify_heading(text)
            if heading_match is not None:
                level, heading_text = heading_match
                if level == "article":
                    flush()
                    provision_no = _article_number(text)
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
            if current_no is not None:
                current_pieces.append(text)
            cursor += len(text)

        flush()
        return ParsedInstrument(paragraphs=tuple(headings), articles=tuple(articles))


def _classify_heading(text: str) -> tuple[str, str] | None:
    if text.startswith(_PREAMBLE):
        return "preamble", text
    if _ARTICLE_HEADING.match(text):
        return "article", text
    if _SECTION_HEADING.match(text):
        return "section", text
    if _CHAPTER_HEADING.match(text):
        return "chapter", text
    if _PART_HEADING.match(text):
        return "part", text
    return None


def _article_number(text: str) -> str:
    match = _ARTICLE_HEADING.match(text)
    if match is None:
        return text[:8]
    raw = match.group(0).strip()
    return re.sub(r"[^\d0-9０-９]+", "", raw) or raw
