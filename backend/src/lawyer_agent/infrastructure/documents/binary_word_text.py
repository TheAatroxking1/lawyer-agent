"""Restricted, non-executing text recovery for explicitly selected legacy DOCs.

This is not an Office repair engine. Only flat CFB v3, known FIB layouts,
uncompressed UTF-16 pieces and main/header stories with verified controls are
accepted. Unsupported content is rejected rather than silently omitted.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from lawyer_agent.infrastructure.documents.conversion_paths import unredirected_path

VERSION = "binary-word-static-text-v1"
MAX_SOURCE_BYTES = 16 * 1024 * 1024
_FREE, _END, _FAT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"


class BinaryWordTextError(ValueError):
    """Stable rejection of unsupported or inconsistent binary Word input."""


def _check(condition: bool, code: str) -> None:
    if not condition:
        raise BinaryWordTextError(code)


def _u16(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<H", data, offset)[0])


def _u32(data: bytes, offset: int) -> int:
    return int(struct.unpack_from("<I", data, offset)[0])


@dataclass(frozen=True, slots=True)
class BinaryWordField:
    instruction: str
    result: str
    start_cp: int
    end_cp: int


@dataclass(frozen=True, slots=True)
class BinaryWordStory:
    raw_text: str
    visible_text: str
    fields: tuple[BinaryWordField, ...]

    @property
    def paragraphs(self) -> tuple[str, ...]:
        # A final paragraph mark terminates the last paragraph; internal empty
        # paragraphs are retained, including an otherwise empty header story.
        return tuple(self.visible_text[:-1].split("\r")) if self.visible_text else ()


@dataclass(frozen=True, slots=True)
class BinaryWordTextDocument:
    main: BinaryWordStory
    header: BinaryWordStory
    quality_flags: tuple[str, ...]
    source_sha256: str


@dataclass(frozen=True, slots=True)
class _DirectoryEntry:
    name: str
    kind: int
    start: int
    size: int
    left: int
    right: int
    child: int


class _CompoundFile:
    def __init__(self, payload: bytes) -> None:
        _check(512 <= len(payload) <= MAX_SOURCE_BYTES, "binary_source_size_limit")
        _check(payload[:8] == bytes.fromhex("d0cf11e0a1b11ae1"), "invalid_cfb_signature")
        _check(_u16(payload, 26) == 3 and _u16(payload, 28) == 0xFFFE, "unsupported_cfb_version")
        _check(_u16(payload, 30) == 9 and _u16(payload, 32) == 6, "unsupported_sector_size")
        _check(len(payload) % 512 == 0, "truncated_cfb")
        _check(_u32(payload, 40) == 0 and _u32(payload, 56) == 4096, "unsupported_cfb_layout")
        _check(_u32(payload, 72) == 0 and _u32(payload, 68) == _END, "unsupported_extended_difat")
        self.payload = payload
        self.sectors = len(payload) // 512 - 1
        self.used: set[int] = set()
        self.mini_used: set[int] = set()
        fat_ids = [_u32(payload, 76 + i * 4) for i in range(109)]
        fat_ids = [sid for sid in fat_ids if sid != _FREE]
        _check(len(fat_ids) == _u32(payload, 44) and bool(fat_ids), "invalid_fat_count")
        self.fat: tuple[int, ...] = tuple(
            value for sid in fat_ids for value in struct.unpack("<128I", self._sector(sid))
        )
        _check(len(self.fat) >= self.sectors, "fat_too_short")
        for sid in fat_ids:
            self._claim(sid, self.used)
            _check(self.fat[sid] == _FAT, "invalid_fat_marker")
        directory = self._regular(_u32(payload, 48), None)
        _check(len(directory) // 128 <= 2048, "directory_entry_limit")
        entries: dict[int, _DirectoryEntry] = {}
        for index in range(len(directory) // 128):
            raw = directory[index * 128 : (index + 1) * 128]
            kind = raw[66]
            if kind == 0:
                continue
            _check(kind == (5 if index == 0 else 2), "unsupported_cfb_storage")
            name_length = _u16(raw, 64)
            _check(2 <= name_length <= 64 and name_length % 2 == 0, "invalid_cfb_name")
            _check(raw[name_length - 2 : name_length] == b"\0\0", "invalid_cfb_name")
            name = raw[: name_length - 2].decode("utf-16-le")
            _check(bool(name) and "\0" not in name, "invalid_cfb_name")
            entries[index] = _DirectoryEntry(
                name,
                kind,
                _u32(raw, 116),
                int(struct.unpack_from("<Q", raw, 120)[0]),
                _u32(raw, 68),
                _u32(raw, 72),
                _u32(raw, 76),
            )
        root = entries[0]
        _check(root.left == root.right == _FREE, "invalid_root_siblings")
        seen: set[int] = set()
        pending = [root.child]
        while pending:
            index = pending.pop()
            if index == _FREE:
                continue
            _check(index != 0 and index in entries and index not in seen, "invalid_directory_tree")
            seen.add(index)
            entry = entries[index]
            _check(entry.child == _FREE, "unsupported_nested_storage")
            pending.extend((entry.left, entry.right))
        _check(seen == set(entries) - {0}, "unreachable_directory_entry")
        mini_stream = self._regular(root.start, root.size)
        mini_fat_blob = self._regular(_u32(payload, 60), _u32(payload, 64) * 512)
        mini_fat = struct.unpack("<" + "I" * (len(mini_fat_blob) // 4), mini_fat_blob)
        allowed = {
            "WordDocument",
            "0Table",
            "1Table",
            "Data",
            "WpsCustomData",
            "\x05SummaryInformation",
            "\x05DocumentSummaryInformation",
        }
        self.streams: dict[str, bytes] = {}
        for index in sorted(seen):
            entry = entries[index]
            _check(
                entry.name in allowed and entry.name not in self.streams,
                "unsupported_or_duplicate_stream",
            )
            if entry.size >= 4096:
                data = self._regular(entry.start, entry.size)
            elif entry.size:
                ids = self._chain(entry.start, mini_fat, len(mini_stream) // 64, self.mini_used)
                _check(len(ids) == (entry.size + 63) // 64, "mini_chain_size_mismatch")
                data = b"".join(mini_stream[sid * 64 : (sid + 1) * 64] for sid in ids)[: entry.size]
            else:
                _check(entry.start in (_END, _FREE, 0), "invalid_empty_stream")
                data = b""
            self.streams[entry.name] = data
        _check(not self.streams.get("Data"), "unsupported_object_data")

    def _sector(self, sid: int) -> bytes:
        _check(0 <= sid < self.sectors, "sector_out_of_bounds")
        return self.payload[(sid + 1) * 512 : (sid + 2) * 512]

    @staticmethod
    def _claim(sid: int, used: set[int]) -> None:
        _check(sid not in used, "cyclic_or_crosslinked_sector")
        used.add(sid)

    def _chain(self, start: int, table: tuple[int, ...], bound: int, used: set[int]) -> list[int]:
        result: list[int] = []
        cursor = start
        while cursor != _END:
            _check(0 <= cursor < min(bound, len(table)), "chain_out_of_bounds")
            self._claim(cursor, used)
            result.append(cursor)
            cursor = table[cursor]
        return result

    def _regular(self, start: int, size: int | None) -> bytes:
        if size == 0:
            _check(start in (_END, _FREE, 0), "invalid_empty_chain")
            return b""
        _check(size is None or size <= MAX_SOURCE_BYTES, "stream_size_limit")
        ids = self._chain(start, self.fat, self.sectors, self.used)
        if size is not None:
            _check(len(ids) == (size + 511) // 512, "stream_chain_size_mismatch")
        return b"".join(self._sector(sid) for sid in ids)[:size]


def _table_slice(table: bytes, pair: tuple[int, int]) -> bytes:
    offset, length = pair
    _check(length == 0 or offset + length <= len(table), "table_range_out_of_bounds")
    return table[offset : offset + length] if length else b""


def _cp_positions(text: str) -> list[int]:
    result = [0]
    for char in text:
        result.append(result[-1] + (2 if ord(char) > 0xFFFF else 1))
    return result


def _story(raw: str, field_table: bytes, total_cp: int) -> BinaryWordStory:
    _check(not raw or raw.endswith("\r"), "unterminated_story")
    _check(
        all(ord(char) >= 32 or char in "\t\v\f\r\x13\x14\x15" for char in raw),
        "unsupported_text_control",
    )
    _check(not any(char in raw for char in "\ufffe\uffff"), "invalid_xml_character")
    cps = _cp_positions(raw)
    markers = [(cps[index], ord(char)) for index, char in enumerate(raw) if char in "\x13\x14\x15"]
    if not markers:
        _check(not field_table, "field_table_without_controls")
    else:
        n = len(markers)
        _check(len(field_table) == 6 * n + 4, "field_table_size_mismatch")
        _check(_u32(field_table, n * 4) == total_cp, "field_terminal_cp_mismatch")
        for index, (cp, marker) in enumerate(markers):
            _check(_u32(field_table, index * 4) == cp, "field_cp_mismatch")
            _check(field_table[4 * (n + 1) + index * 2] & 0x1F == marker, "field_marker_mismatch")
    visible: list[str] = []
    fields: list[BinaryWordField] = []
    cursor = 0
    while cursor < len(raw):
        char = raw[cursor]
        if char != "\x13":
            _check(char not in "\x14\x15", "unbalanced_field")
            visible.append(char)
            cursor += 1
            continue
        separator = raw.find("\x14", cursor + 1)
        end = raw.find("\x15", cursor + 1)
        _check(cursor < separator < end, "unbalanced_field")
        instruction = raw[cursor + 1 : separator]
        result = raw[separator + 1 : end]
        _check(
            not any(c in instruction + result for c in "\x13\x14\x15"), "nested_field_unsupported"
        )
        _check(
            bool(re.fullmatch(r'\s*(?:PAGE|HYPERLINK\s+"[^"\r\n]+")\s*', instruction)),
            "unsupported_field_instruction",
        )
        _check(not any(ord(c) < 32 for c in instruction), "unsupported_field_control")
        fields.append(BinaryWordField(instruction, result, cps[cursor], cps[end + 1]))
        visible.append(result)
        cursor = end + 1
    return BinaryWordStory(raw, "".join(visible), tuple(fields))


def _read(payload: bytes) -> BinaryWordTextDocument:
    streams = _CompoundFile(payload).streams
    word = streams["WordDocument"]
    _check(_u16(word, 0) == 0xA5EC, "invalid_fib_signature")
    flags = _u16(word, 10)
    _check(not flags & 0x0100 and _u32(word, 14) == 0, "encrypted_doc_unsupported")
    _check(not flags & 3 and bool(flags & 0x1000), "unsupported_fib_flags")
    _check(_u16(word, 32) == 14 and _u16(word, 62) == 22, "unsupported_fib_layout")
    pair_count = _u16(word, 152)
    _check(pair_count in (93, 164), "unsupported_fib_version")
    pairs = tuple((_u32(word, 154 + i * 8), _u32(word, 158 + i * 8)) for i in range(pair_count))
    new_pos = 154 + pair_count * 8
    new_count = _u16(word, new_pos)
    version = _u16(word, new_pos + 2) if new_count else _u16(word, 2)
    _check(
        (version, pair_count, new_count) in ((0xC1, 93, 0), (0x10C, 164, 2)),
        "unsupported_fib_version",
    )
    fib_end = new_pos + 2 + new_count * 2
    cb_mac = _u32(word, 64)
    _check(fib_end <= cb_mac <= len(word), "invalid_cbMac")
    counts = tuple(_u32(word, 64 + i * 4) for i in range(3, 11))
    _check(
        all(count == 0 for i, count in enumerate(counts) if i not in (0, 2)),
        "unsupported_text_story",
    )
    main_cp, header_cp = counts[0], counts[2]
    expected_cp = main_cp + header_cp + (1 if header_cp else 0)
    _check(0 < main_cp <= expected_cp <= MAX_SOURCE_BYTES // 2, "invalid_story_counts")
    table = streams["1Table" if flags & 0x200 else "0Table"]
    clx = _table_slice(table, pairs[33])
    _check(len(clx) >= 5 and clx[0] == 2, "unsupported_clx_prc")
    plc_size = _u32(clx, 1)
    _check(
        plc_size == len(clx) - 5 and plc_size >= 16 and (plc_size - 4) % 12 == 0,
        "invalid_piece_table_size",
    )
    plc, n = clx[5:], (plc_size - 4) // 12
    _check(n <= 4096, "piece_count_limit")
    cps = [_u32(plc, i * 4) for i in range(n + 1)]
    _check(
        cps[0] == 0
        and cps[-1] == expected_cp
        and all(a < b for a, b in zip(cps, cps[1:], strict=False)),
        "invalid_piece_cp_sequence",
    )
    pieces: list[bytes] = []
    intervals: list[tuple[int, int]] = []
    for index in range(n):
        pos = 4 * (n + 1) + index * 8
        _check(_u16(plc, pos) == 0 and _u16(plc, pos + 6) == 0, "unsupported_piece_properties")
        fc = _u32(plc, pos + 2)
        _check(not fc & 0x40000000, "compressed_piece_unsupported")
        _check(not fc & 0x80000000 and fc % 2 == 0, "invalid_piece_offset")
        end = fc + 2 * (cps[index + 1] - cps[index])
        _check(fib_end <= fc < end <= cb_mac, "piece_out_of_bounds")
        _check(
            all(end <= start or fc >= finish for start, finish in intervals),
            "overlapping_text_pieces",
        )
        intervals.append((fc, end))
        pieces.append(word[fc:end])
    text_bytes = b"".join(pieces)
    _check(not header_cp or text_bytes[-2:] == b"\r\0", "invalid_story_terminator")
    main_raw = text_bytes[: main_cp * 2].decode("utf-16-le")
    header_raw = text_bytes[main_cp * 2 : (main_cp + header_cp) * 2].decode("utf-16-le")
    hdd = _table_slice(table, pairs[11])
    if header_cp:
        _check(len(hdd) >= 8 and len(hdd) % 4 == 0, "invalid_header_positions")
        boundaries = [_u32(hdd, i) for i in range(0, len(hdd), 4)]
        _check(
            boundaries[0] == 0
            and all(a <= b for a, b in zip(boundaries, boundaries[1:], strict=False))
            and boundaries[-1] <= header_cp + 2,
            "invalid_header_positions",
        )
    else:
        _check(not hdd, "unexpected_header_positions")
    main = _story(main_raw, _table_slice(table, pairs[16]), expected_cp)
    header = _story(header_raw, _table_slice(table, pairs[17]), expected_cp)
    _check(bool(main.visible_text.strip()), "no_readable_text")
    flags_out = [
        "legacy_binary_static_text_recovery",
        "original_layout_and_visibility_not_preserved",
        "images_objects_and_automatic_numbering_not_recovered",
        "header_sections_not_preserved",
    ]
    if main.fields or header.fields:
        flags_out.extend(
            ("field_results_static_not_recalculated", "field_instructions_not_executed")
        )
    return BinaryWordTextDocument(
        main, header, tuple(flags_out), hashlib.sha256(payload).hexdigest()
    )


def read_binary_word_text(payload: bytes) -> BinaryWordTextDocument:
    """Read only supported text stories, rejecting incomplete or ambiguous input."""
    try:
        return _read(payload)
    except (struct.error, UnicodeError, IndexError, KeyError) as exc:
        raise BinaryWordTextError("malformed_binary_word") from exc


def _story_xml(story: BinaryWordStory, *, header: bool) -> bytes:
    root = ET.Element(f"{{{_W}}}hdr" if header else f"{{{_W}}}document")
    body = root if header else ET.SubElement(root, f"{{{_W}}}body")
    for paragraph in story.paragraphs:
        p = ET.SubElement(body, f"{{{_W}}}p")
        r = ET.SubElement(p, f"{{{_W}}}r")
        for part in re.split(r"([\t\v\f])", paragraph):
            if part == "\t":
                ET.SubElement(r, f"{{{_W}}}tab")
            elif part in ("\v", "\f"):
                attributes = {f"{{{_W}}}type": "page"} if part == "\f" else {}
                ET.SubElement(r, f"{{{_W}}}br", attributes)
            elif part:
                ET.SubElement(
                    r, f"{{{_W}}}t", {"{http://www.w3.org/XML/1998/namespace}space": "preserve"}
                ).text = part
    if not header:
        section = ET.SubElement(body, f"{{{_W}}}sectPr")
        ET.SubElement(
            section,
            f"{{{_W}}}headerReference",
            {f"{{{_W}}}type": "default", f"{{{_R}}}id": "rIdHeader"},
        )
    return bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))


def _docx(document: BinaryWordTextDocument) -> bytes:
    types = ET.Element(f"{{{_TYPES}}}Types")
    for extension, content_type in (
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("json", "application/json"),
    ):
        ET.SubElement(types, f"{{{_TYPES}}}Default", Extension=extension, ContentType=content_type)
    for part, kind in (("/word/document.xml", "document.main"), ("/word/header1.xml", "header")):
        ET.SubElement(
            types,
            f"{{{_TYPES}}}Override",
            PartName=part,
            ContentType=f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml",
        )
    relationships = ET.Element(f"{{{_PKG}}}Relationships")
    ET.SubElement(
        relationships,
        f"{{{_PKG}}}Relationship",
        Id="rId1",
        Type=_R + "/officeDocument",
        Target="word/document.xml",
    )
    document_relationships = ET.Element(f"{{{_PKG}}}Relationships")
    ET.SubElement(
        document_relationships,
        f"{{{_PKG}}}Relationship",
        Id="rIdHeader",
        Type=_R + "/header",
        Target="header1.xml",
    )
    metadata = {
        "schema_version": VERSION,
        "source_sha256": document.source_sha256,
        "quality_flags": document.quality_flags,
        "stories": {"main": asdict(document.main), "header": asdict(document.header)},
    }
    parts = {
        "[Content_Types].xml": ET.tostring(types, encoding="utf-8", xml_declaration=True),
        "_rels/.rels": ET.tostring(relationships, encoding="utf-8", xml_declaration=True),
        "word/_rels/document.xml.rels": ET.tostring(
            document_relationships, encoding="utf-8", xml_declaration=True
        ),
        "word/document.xml": _story_xml(document.main, header=False),
        "word/header1.xml": _story_xml(document.header, header=True),
        "word/static-text-recovery.json": json.dumps(
            metadata, ensure_ascii=False, sort_keys=True
        ).encode("utf-8"),
    }
    result = io.BytesIO()
    with ZipFile(result, "w") as archive:
        for name, payload in sorted(parts.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, payload)
    return result.getvalue()


class BinaryWordTextConverter:
    @property
    def fingerprint(self) -> str:
        code = unredirected_path(Path(__file__)).read_bytes()
        return hashlib.sha256(VERSION.encode() + b"\0" + code).hexdigest()

    def __call__(self, source: Path, target: Path) -> str:
        source, target = unredirected_path(source), unredirected_path(target)
        _check(source != target and target.suffix.lower() == ".docx", "unsafe_static_target")
        _check(source.suffix.lower() in (".doc", ".docx"), "unsupported_source_extension")
        _check(
            source.is_file() and source.stat().st_size <= MAX_SOURCE_BYTES,
            "binary_source_size_limit",
        )
        with source.open("rb") as stream:
            payload = stream.read(MAX_SOURCE_BYTES + 1)
        document = read_binary_word_text(payload)
        output = _docx(document)
        unredirected_path(target)
        with target.open("xb") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        return VERSION
