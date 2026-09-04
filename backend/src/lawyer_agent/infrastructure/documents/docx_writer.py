"""Write-only DOCX report generator built on the stdlib ZIP and XML writers.

The writer turns strongly typed report segments into a minimal .docx package.
It never parses untrusted XML, never emits DOCTYPE/ENTITY declarations, macros
or external relationships, and XML-escapes every text segment. Output can be
read back by the read-only ``ZipDocxLoader`` for round-trip verification.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO
from xml.sax.saxutils import escape

_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_MAX_SEGMENT_CHARS = 200_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Types xmlns="{_CT_NS}">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.'
    'relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/>'
    "</Types>"
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<Relationships xmlns="{_REL_NS}">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/'
    '2006/relationships/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)


class ReportSegmentKind(StrEnum):
    TITLE = "title"
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    NOTE = "note"


@dataclass(frozen=True, slots=True)
class ReportSegment:
    """One strongly typed text segment of an exported report."""

    kind: ReportSegmentKind
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReportSegmentKind):
            raise ValueError("report segment kind must be strongly typed")
        if not isinstance(self.text, str):
            raise ValueError("report segment text must be text")
        if len(self.text) > _MAX_SEGMENT_CHARS:
            raise ValueError("report segment text is too long")
        if _CONTROL.search(self.text):
            raise ValueError("report segment text contains a control character")


class ZipDocxReportWriter:
    """Serializes typed segments into a minimal read-only-safe DOCX package."""

    writer_version = "docx-zip-report-v1"

    def write(self, segments: tuple[ReportSegment, ...] | list[ReportSegment]) -> bytes:
        body = "".join(_paragraph(segment) for segment in segments)
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{_WORD_NS}">'
            f"<w:body>{body}</w:body>"
            "</w:document>"
        )
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _CONTENT_TYPES)
            archive.writestr("_rels/.rels", _RELS)
            archive.writestr("word/document.xml", document)
        return buffer.getvalue()


def _paragraph(segment: ReportSegment) -> str:
    style = _style_for_kind(segment.kind)
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    text = escape(segment.text)
    # Keep meaningful spaces/lines intact inside a run.
    return f"<w:p>{props}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r></w:p>"


def _style_for_kind(kind: ReportSegmentKind) -> str | None:
    if kind is ReportSegmentKind.TITLE:
        return "Title"
    if kind is ReportSegmentKind.HEADING:
        return "Heading1"
    if kind is ReportSegmentKind.LIST_ITEM:
        return "ListParagraph"
    if kind is ReportSegmentKind.NOTE:
        return "IntenseQuote"
    return None
