"""Read-only DOCX loader built on the stdlib ZIP and XML parsers.

Only word/document.xml text is extracted; no macros, OLE, or external
relationships are executed or followed. The loader never mutates the source
file and never reads .docm/.zip payloads.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import PurePath
from xml.parsers import expat

from lawyer_agent.domain.common import require_uuid7  # noqa: F401  (kept for hashing parity)
from lawyer_agent.infrastructure.documents.loader import (
    ParsedDocument,
    ParsedParagraph,
)

_WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_PARAGRAPH_TAG = f"{_WORD_NS}p"
_TEXT_TAG = f"{_WORD_NS}t"
_BREAK_TAG = f"{_WORD_NS}br"
_STYLE_TAG = f"{_WORD_NS}pStyle"
_MAX_PARAGRAPHS = 200_000
_MAX_TEXT_BYTES = 32 * 1024 * 1024
_MAX_XML_BYTES = 16 * 1024 * 1024
_MAX_EXPANDED_BYTES = 64 * 1024 * 1024
_MAX_ZIP_MEMBERS = 2048
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InvalidDocx(ValueError):
    pass


class ZipDocxLoader:
    """Extracts normalized paragraphs from a DOCX byte payload."""

    loader_version = "docx-zip-v1"

    def __init__(self, source_ref: str) -> None:
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("source ref must be non-empty text")
        self._source_ref = source_ref

    def load(self, source_ref: str, payload: bytes) -> ParsedDocument:
        del source_ref
        if not payload or len(payload) > _MAX_TEXT_BYTES:
            raise InvalidDocx("docx payload has an invalid size")
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(payload)) as archive:
                infos = archive.infolist()
                if len(infos) > _MAX_ZIP_MEMBERS:
                    raise InvalidDocx("docx ZIP member count exceeds the limit")
                if sum(info.file_size for info in infos) > _MAX_EXPANDED_BYTES:
                    raise InvalidDocx("docx expanded archive size exceeds the limit")
                if any(
                    name.lower().endswith((".docm", ".doc", ".zip", ".7z", ".rar"))
                    for name in archive.namelist()
                ):
                    raise InvalidDocx("macro or archive payloads are not supported")
                try:
                    if archive.getinfo("word/document.xml").file_size > _MAX_XML_BYTES:
                        raise InvalidDocx("docx expanded document XML size exceeds the limit")
                    xml_bytes = archive.read("word/document.xml")
                except KeyError as exc:
                    raise InvalidDocx("docx is missing word/document.xml") from exc
        except zipfile.BadZipFile as exc:
            raise InvalidDocx("payload is not a valid zip archive") from exc
        except ET.ParseError as exc:
            raise InvalidDocx("docx document xml cannot be parsed") from exc

        try:
            root = _parse_without_entities(xml_bytes)
        except (ET.ParseError, expat.ExpatError, InvalidDocx) as exc:
            if isinstance(exc, InvalidDocx):
                raise
            raise InvalidDocx("docx document xml cannot be parsed") from exc

        paragraphs: list[ParsedParagraph] = []
        for ordinal, element in enumerate(root.iter(_PARAGRAPH_TAG)):
            if len(paragraphs) >= _MAX_PARAGRAPHS:
                raise InvalidDocx("docx has too many paragraphs")
            text_parts: list[str] = []
            style: str | None = None
            for child in element.iter():
                tag = child.tag
                if tag == _TEXT_TAG and child.text:
                    text_parts.append(child.text)
                elif tag == _BREAK_TAG:
                    text_parts.append("\n")
                elif tag == _STYLE_TAG:
                    style_val = child.get(f"{_WORD_NS}val")
                    if style_val:
                        style = style_val
            raw = "".join(text_parts)
            text = _CONTROL.sub("", raw).rstrip("\n")
            paragraphs.append(ParsedParagraph(text=text, style=style, ordinal=ordinal))
        return ParsedDocument(
            paragraphs=tuple(paragraphs),
            source_ref=self._source_ref,
            loader_version=self.loader_version,
        )


def _parse_without_entities(xml_bytes: bytes) -> ET.Element:
    """Parse untrusted XML with DTD/entity declarations rejected up front.

    stdlib ElementTree does not resolve external entities, but internal entity
    expansion can still be large; rejecting any DOCTYPE keeps document XML safe.
    """
    parser = expat.ParserCreate()

    def reject(*_args: object) -> None:
        raise InvalidDocx("docx document xml must not declare a DTD or entities")

    # Let the XML parser detect encoding (including BOM-less UTF-16). Raw byte
    # substring checks cannot reject declarations consistently across encodings.
    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.Parse(xml_bytes, True)
    # DTD/ENTITY have been rejected before constructing the document tree.
    return ET.fromstring(xml_bytes)  # noqa: S314


def is_safe_source_ref(source_ref: str) -> bool:
    path = PurePath(source_ref)
    return not path.name.startswith("~") and not path.name.startswith(".")
