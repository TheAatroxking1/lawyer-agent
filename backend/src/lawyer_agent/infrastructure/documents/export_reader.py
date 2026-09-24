"""Conservative DOCX reader for offline legal-corpus export."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

from lawyer_agent.infrastructure.documents.word_article_numbering import (
    ArticleNumbering,
    NumberingProvenance,
)

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_AUXILIARY_NAMES = {"footnotes.xml", "endnotes.xml"}


class ExportReadError(ValueError):
    """Stable failure at the untrusted DOCX boundary."""


@dataclass(frozen=True, slots=True)
class ExportParagraph:
    text: str
    ordinal: int
    part: str
    location: str
    table_position: tuple[int, int, int] | None = None
    numbering_provenance: NumberingProvenance | None = None


@dataclass(frozen=True, slots=True)
class ExportDocument:
    paragraphs: tuple[ExportParagraph, ...]
    quality_flags: tuple[str, ...]
    loader_version: str = "docx-export-reader-v2"


class DocxExportReader:
    def __init__(
        self,
        *,
        max_member_bytes: int = 16 * 1024 * 1024,
        max_total_bytes: int = 64 * 1024 * 1024,
        max_members: int = 2048,
    ) -> None:
        self.max_member_bytes = max_member_bytes
        self.max_total_bytes = max_total_bytes
        self.max_members = max_members

    def read(self, path: Path) -> ExportDocument:
        return self._read(path, source_suffix=path.suffix)

    def read_bytes(self, payload: bytes, *, source_suffix: str = ".docx") -> ExportDocument:
        """Parse the same immutable bytes whose digest the caller has verified."""
        return self._read(BytesIO(payload), source_suffix=source_suffix)

    def _read(self, source: Path | BytesIO, *, source_suffix: str) -> ExportDocument:
        try:
            with ZipFile(source) as archive:
                infos = archive.infolist()
                if len(infos) > self.max_members:
                    raise ExportReadError("zip_member_count_limit")
                if sum(info.file_size for info in infos) > self.max_total_bytes:
                    raise ExportReadError("expanded_size_limit")
                names = {info.filename for info in infos}
                if "word/document.xml" not in names:
                    raise ExportReadError("document_xml_missing")
                selected = ["word/document.xml"]
                selected.extend(sorted(name for name in names if _is_auxiliary(name)))
                numbering = None
                if "word/numbering.xml" in names:
                    info = archive.getinfo("word/numbering.xml")
                    if info.file_size > self.max_member_bytes:
                        raise ExportReadError("expanded_size_limit")
                    numbering_payload = archive.read("word/numbering.xml")
                    _reject_xml_declarations(numbering_payload)
                    numbering = ArticleNumbering(numbering_payload)
                paragraphs: list[ExportParagraph] = []
                flags: set[str] = set()
                if source_suffix.lower() == ".docm":
                    flags.add("macro_enabled_container_no_execution")
                for name in selected:
                    # Media and embedded objects are never decompressed. They
                    # still count toward the archive-wide limits above.
                    if archive.getinfo(name).file_size > self.max_member_bytes:
                        raise ExportReadError("expanded_size_limit")
                    payload = archive.read(name)
                    _reject_xml_declarations(payload)
                    parsed, part_flags = _read_part(
                        payload,
                        name,
                        len(paragraphs),
                        numbering if name == "word/document.xml" else None,
                    )
                    paragraphs.extend(parsed)
                    flags.update(part_flags)
        except BadZipFile as exc:
            raise ExportReadError("invalid_docx_zip") from exc
        except ET.ParseError as exc:
            raise ExportReadError("invalid_docx_xml") from exc
        except expat.ExpatError as exc:
            raise ExportReadError("invalid_docx_xml") from exc
        if not paragraphs:
            flags.add("no_readable_text")
        restored = any(p.numbering_provenance is not None for p in paragraphs)
        if restored:
            flags.add("article_numbering_restored")
        version = "docx-export-reader-v2:numbering-v1" if restored else "docx-export-reader-v2"
        return ExportDocument(tuple(paragraphs), tuple(sorted(flags)), version)


def _is_auxiliary(name: str) -> bool:
    leaf = name.rsplit("/", 1)[-1]
    return (
        name.startswith("word/header")
        or name.startswith("word/footer")
        or leaf in _AUXILIARY_NAMES
    ) and name.endswith(".xml")


def _read_part(
    payload: bytes, part_name: str, start: int, numbering: ArticleNumbering | None
) -> tuple[list[ExportParagraph], set[str]]:
    # DTD/entity declarations are rejected above and expanded ZIP sizes are bounded.
    root = ET.fromstring(payload)  # noqa: S314
    flags: set[str] = set()
    result: list[ExportParagraph] = []
    is_main = part_name == "word/document.xml"
    if is_main:
        bodies = root.findall(f"{_W}body")
        if root.tag != f"{_W}document" or len(bodies) != 1:
            raise ExportReadError("invalid_document_body")
        body = bodies[0]
        if set(root.iter(f"{_W}p")) != set(body.iter(f"{_W}p")):
            raise ExportReadError("invalid_document_body")
        root = body
    else:
        flags.add("auxiliary_parts_separate")
    tables = tuple(root.iter(f"{_W}tbl"))
    if tables:
        flags.add("table_layout_not_preserved")
    if (
        root.findall(".//{urn:schemas-microsoft-com:vml}shape")
        or root.findall(f".//{_W}drawing")
        or root.findall(f".//{_W}object")
    ):
        flags.add("images_or_textboxes_not_ocr")
    parents = {child: parent for parent in root.iter() for child in parent}
    for xml_ordinal, paragraph in enumerate(root.iter(f"{_W}p")):
        text_nodes: list[str] = []
        for node in paragraph.iter():
            ancestor = parents.get(node)
            while ancestor is not None and ancestor.tag != f"{_W}p":
                ancestor = parents.get(ancestor)
            if ancestor is not paragraph:
                continue
            if node.tag == f"{_W}t":
                text_nodes.append(node.text or "")
            elif node.tag == f"{_W}tab":
                text_nodes.append("\t")
                flags.add("word_control_characters_preserved")
            elif node.tag in {f"{_W}br", f"{_W}cr"}:
                text_nodes.append("\n")
                flags.add("word_control_characters_preserved")
            elif node.tag == f"{_W}noBreakHyphen":
                text_nodes.append("‑")
                flags.add("word_control_characters_preserved")
            elif node.tag == f"{_W}softHyphen":
                text_nodes.append("\u00ad")
                flags.add("word_control_characters_preserved")
        text = "".join(text_nodes).strip()
        provenance = None
        if is_main and numbering is not None:
            text, provenance = numbering.restore(paragraph, text, xml_ordinal)
        if paragraph.find(f"./{_W}pPr/{_W}numPr") is not None and provenance is None:
            flags.add("automatic_numbering_not_resolved")
        if not "".join(text_nodes).strip() and provenance is None:
            continue
        in_table = any(paragraph in table.iter(f"{_W}p") for table in tables)
        location = "auxiliary" if not is_main else ("table" if in_table else "body")
        table_position = _table_position(paragraph, parents, tables) if in_table else None
        result.append(
            ExportParagraph(
                text, start + len(result), part_name, location, table_position, provenance
            )
        )
    return result, flags


def _reject_xml_declarations(payload: bytes) -> None:
    parser = expat.ParserCreate()

    def reject(*_args: object) -> None:
        raise ExportReadError("xml_entities_forbidden")

    parser.StartDoctypeDeclHandler = reject
    parser.EntityDeclHandler = reject
    parser.Parse(payload, True)


def _table_position(
    paragraph: ET.Element,
    parents: dict[ET.Element, ET.Element],
    tables: tuple[ET.Element, ...],
) -> tuple[int, int, int] | None:
    cell = parents.get(paragraph)
    while cell is not None and cell.tag != f"{_W}tc":
        cell = parents.get(cell)
    if cell is None:
        return None
    row = parents.get(cell)
    while row is not None and row.tag != f"{_W}tr":
        row = parents.get(row)
    table = parents.get(row) if row is not None else None
    while table is not None and table.tag != f"{_W}tbl":
        table = parents.get(table)
    if row is None or table is None:
        return None
    return (
        tables.index(table),
        tuple(table.iter(f"{_W}tr")).index(row),
        tuple(row.iter(f"{_W}tc")).index(cell),
    )
