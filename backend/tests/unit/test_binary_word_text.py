from __future__ import annotations

import io
import json
import os
import struct
from zipfile import ZipFile

import pytest

from tests.unit.binary_word_fixture import binary_doc, patch_word, with_mini_stream


def read(payload):
    from lawyer_agent.infrastructure.documents.binary_word_text import read_binary_word_text

    return read_binary_word_text(payload)


def test_main_header_and_field_results_stay_separate():
    main = '第一条 合成\x13 HYPERLINK "javascript:void(0);" \x14\x15正文。\r第二条 甲\t乙\x0c丙。\r'
    header = "— \x13PAGE  \x142\x15 —\r"
    document = read(binary_doc(main, header))
    assert document.main.raw_text == main
    assert document.main.visible_text == "第一条 合成正文。\r第二条 甲\t乙\x0c丙。\r"
    assert document.header.raw_text == header
    assert document.header.visible_text == "— 2 —\r"
    assert document.main.fields[0].result == ""
    assert "javascript" in document.main.fields[0].instruction
    assert "field_results_static_not_recalculated" in document.quality_flags


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: patch_word(b, 0, "<H", 0),
        lambda b: patch_word(b, 10, "<H", 0x1100),
        lambda b: patch_word(b, 32, "<H", 15),
        lambda b: patch_word(b, 154 + 164 * 8 + 2, "<H", 0x999),
        lambda b: patch_word(b, 80, "<I", 1),
        lambda b: patch_word(b, 154 + 33 * 8, "<II", 4090, 21),
        lambda b: patch_word(b, 64, "<I", 2050),
    ],
)
def test_invalid_fib_and_unsupported_stories_are_rejected(mutate):
    with pytest.raises(ValueError):
        read(mutate(binary_doc("第一条 正文较长内容。\r")))


def test_compressed_piece_encoding_is_rejected():
    with pytest.raises(ValueError, match="compressed"):
        read(binary_doc(compressed=True))


@pytest.mark.parametrize(
    "main",
    [
        "第一条 \x01对象。\r",
        "第一条 \x13PAGE\x142\r",
        "第一条 \x15正文。\r",
        "第一条 \x13UNKNOWN\x14结果\x15。\r",
        "第一条 \x13PAGE\x13PAGE\x142\x15\x15。\r",
    ],
)
def test_unknown_controls_and_unbalanced_or_nested_fields_are_rejected(main):
    with pytest.raises(ValueError):
        read(binary_doc(main))


@pytest.mark.parametrize(
    "kind", ["cycle", "crosslink", "out_of_bounds", "truncated", "directory_cycle"]
)
def test_malformed_cfb_chains_are_rejected(kind):
    payload = bytearray(binary_doc())
    if kind == "cycle":
        struct.pack_into("<I", payload, 512 * 26 + 4, 1)
    elif kind == "crosslink":
        struct.pack_into("<I", payload, 512 + 256 + 116, 1)
    elif kind == "out_of_bounds":
        struct.pack_into("<I", payload, 512 * 26 + 4, 9999)
    elif kind == "directory_cycle":
        struct.pack_into("<I", payload, 512 + 128 + 72, 1)
    else:
        del payload[-20:]
    with pytest.raises(ValueError):
        read(bytes(payload))


def test_adapter_is_deterministic_and_never_overwrites_inputs(tmp_path):
    from lawyer_agent.infrastructure.documents.binary_word_text import BinaryWordTextConverter
    from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader

    converter = BinaryWordTextConverter()
    source = tmp_path / "source.doc"
    original = binary_doc("第一条 中文。\r", "— \x13PAGE\x142\x15 —\r")
    source.write_bytes(original)
    first, second = tmp_path / "first.docx", tmp_path / "second.docx"
    assert converter(source, first) == "binary-word-static-text-v1"
    assert converter(source, second) == "binary-word-static-text-v1"
    assert first.read_bytes() == second.read_bytes()
    assert source.read_bytes() == original and converter.fingerprint == converter.fingerprint
    paragraphs = DocxExportReader().read(first).paragraphs
    assert [(p.text, p.location) for p in paragraphs] == [
        ("第一条 中文。", "body"),
        ("— 2 —", "auxiliary"),
    ]
    with ZipFile(io.BytesIO(first.read_bytes())) as archive:
        metadata = json.loads(archive.read("word/static-text-recovery.json"))
        assert metadata["stories"]["header"]["fields"][0]["instruction"] == "PAGE"
        assert "word/header1.xml" in archive.namelist()
    with pytest.raises((ValueError, FileExistsError)):
        converter(source, first)
    with pytest.raises(ValueError):
        converter(source, source)


def test_adapter_rejects_invalid_input_before_creating_target(tmp_path):
    from lawyer_agent.infrastructure.documents.binary_word_text import BinaryWordTextConverter

    source, target = tmp_path / "source.doc", tmp_path / "result.docx"
    source.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        BinaryWordTextConverter()(source, target)
    assert not target.exists()


def test_mini_streams_are_checked_without_becoming_document_text():
    document = read(with_mini_stream(binary_doc()))
    assert document.main.visible_text == "第一条 合成正文。\r"


def test_crosslinked_mini_streams_are_rejected():
    with pytest.raises(ValueError, match="crosslinked"):
        read(with_mini_stream(binary_doc(), crosslink=True))


def test_cyclic_mini_stream_is_rejected():
    payload = bytearray(with_mini_stream(binary_doc()))
    struct.pack_into("<I", payload, 28 * 512, 0)
    with pytest.raises(ValueError, match="cyclic"):
        read(bytes(payload))


@pytest.mark.parametrize("kind", ["cp", "marker", "missing", "terminal"])
def test_field_control_table_must_match_actual_text(kind):
    payload = binary_doc("第一条 \x13PAGE\x142\x15正文。\r")
    if kind == "missing":
        payload = patch_word(payload, 154 + 16 * 8, "<II", 0, 0)
    else:
        changed = bytearray(payload)
        field_offset = 18 * 512 + 512
        if kind == "cp":
            struct.pack_into("<I", changed, field_offset, 0)
        elif kind == "terminal":
            struct.pack_into("<I", changed, field_offset + 12, 0)
        else:
            changed[field_offset + 16] = 0x14
        payload = bytes(changed)
    with pytest.raises(ValueError, match="field"):
        read(payload)


@pytest.mark.parametrize("kind", ["start_cp", "end_cp", "length", "prm", "fc", "unpaired_utf16"])
def test_piece_table_and_text_encoding_are_strict(kind):
    payload = bytearray(binary_doc())
    clx_offset = 18 * 512 + 256
    if kind == "start_cp":
        struct.pack_into("<I", payload, clx_offset + 5, 1)
    elif kind == "end_cp":
        struct.pack_into("<I", payload, clx_offset + 9, 1)
    elif kind == "length":
        struct.pack_into("<I", payload, clx_offset + 1, 15)
    elif kind == "prm":
        struct.pack_into("<H", payload, clx_offset + 19, 1)
    elif kind == "fc":
        struct.pack_into("<I", payload, clx_offset + 15, 1)
    else:
        struct.pack_into("<H", payload, 1024 + 2048, 0xD800)
    with pytest.raises(ValueError):
        read(bytes(payload))


def test_fib97_layout_is_supported_and_supplementary_unicode_keeps_cp_offsets():
    payload = binary_doc("第一条 合成😀。\r")
    payload = patch_word(payload, 152, "<H", 93)
    payload = patch_word(payload, 154 + 93 * 8, "<H", 0)
    assert read(payload).main.visible_text == "第一条 合成😀。\r"


def test_raw_formfeed_and_tab_are_explicit_docx_controls(tmp_path):
    from lawyer_agent.infrastructure.documents.binary_word_text import BinaryWordTextConverter
    from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader

    source, target = tmp_path / "source.doc", tmp_path / "out.docx"
    source.write_bytes(binary_doc("第一条 甲\t乙\v丙\x0c丁。\r\r"))
    BinaryWordTextConverter()(source, target)
    assert DocxExportReader().read(target).paragraphs[0].text == "第一条 甲\t乙\n丙\n丁。"
    with ZipFile(target) as archive:
        assert b"instrText" not in archive.read("word/document.xml")
        assert b"fldChar" not in archive.read("word/document.xml")
        assert "header1.xml" in archive.read("word/_rels/document.xml.rels").decode()


def test_adapter_bounds_source_before_reading(tmp_path, monkeypatch):
    from lawyer_agent.infrastructure.documents import binary_word_text

    source = tmp_path / "source.doc"
    source.write_bytes(b"x" * 1025)
    monkeypatch.setattr(binary_word_text, "MAX_SOURCE_BYTES", 1024)
    with pytest.raises(ValueError, match="size_limit"):
        binary_word_text.BinaryWordTextConverter()(source, tmp_path / "out.docx")
    assert not (tmp_path / "out.docx").exists()


def test_unknown_storage_stream_cannot_be_treated_as_text():
    payload = bytearray(with_mini_stream(binary_doc()))
    position = 512 + 3 * 128
    payload[position : position + 64] = ("VBA\0".encode("utf-16-le")).ljust(64, b"\0")
    struct.pack_into("<H", payload, position + 64, 8)
    with pytest.raises(ValueError, match="unsupported"):
        read(bytes(payload))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction boundary")
@pytest.mark.parametrize("direction", ["source", "target"])
def test_adapter_rejects_redirected_source_or_target_parent(tmp_path, direction):
    from lawyer_agent.infrastructure.documents.binary_word_text import BinaryWordTextConverter
    from tests.unit.test_word_driver import junction

    folder = tmp_path / "real"
    folder.mkdir()
    source = folder / "source.doc"
    source.write_bytes(binary_doc())
    link = tmp_path / "redirected"
    junction(link, folder)
    try:
        original = link / "source.doc" if direction == "source" else source
        target = link / "out.docx" if direction == "target" else tmp_path / "out.docx"
        with pytest.raises(ValueError, match="reparse|redirect"):
            BinaryWordTextConverter()(original, target)
        assert not (folder / "out.docx").exists()
        assert not (tmp_path / "out.docx").exists()
    finally:
        os.rmdir(link)
