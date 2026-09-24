from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from lawyer_agent.application.legal_corpus_export import ExportConfig, ExportError, export_corpus
from lawyer_agent.application.legal_export_verification import verify_export
from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader, ExportReadError
from lawyer_agent.infrastructure.documents.word_conversion import convert_document

OLE = bytes.fromhex("d0cf11e0a1b11ae1")
XML = (
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    '<w:body><w:p><w:r><w:t>第一条 合成条文。</w:t></w:r></w:p></w:body></w:document>'
).encode()


def docx(*, media: bytes = b"", main: bytes = XML, auxiliary: bytes = b"") -> bytes:
    stream = io.BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", main)
        if media:
            archive.writestr("word/media/image.emf", media)
        if auxiliary:
            archive.writestr("word/header1.xml", auxiliary)
    return stream.getvalue()


@pytest.mark.parametrize(
    ("suffix", "payload", "expected"),
    [(".doc", b"legacy", True), (".DOCX", OLE + docx(), True),
     (".docx", docx(), False), (".docx", b"short", False),
     (".docm", OLE + docx(), False), (".txt", OLE, False)],
    ids=["legacy-doc", "ole-with-trailing-zip", "native-docx", "short-docx", "macro", "other"],
)
def test_source_format_uses_extension_and_bounded_ole_header(tmp_path, suffix, payload, expected):
    from lawyer_agent.infrastructure.documents.source_format import is_legacy_word_source

    path = tmp_path / ("source" + suffix)
    path.write_bytes(payload)
    assert is_legacy_word_source(path) is expected


class SyntheticConverter:
    fingerprint = "synthetic-source-format-v1"

    def __call__(self, source: Path, target: Path) -> str:
        assert source.read_bytes().startswith(OLE)
        target.write_bytes(docx())
        return "synthetic"


def test_disguised_ole_docx_conversion_export_and_verification(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    source = root / "实际旧文档.docx"
    original = OLE + docx()  # A trailing ZIP must not override the OLE header.
    source.write_bytes(original)
    output = tmp_path / "export"
    pending = export_corpus(ExportConfig(root, output))
    assert (pending.pending_conversion, pending.failed) == (1, 0)
    converted = tmp_path / "converted"
    record = convert_document(
        source, source_root=root, output_root=converted, converter=SyntheticConverter(),
    )
    assert record.status == "converted"
    manifest = converted / "manifest.jsonl"
    manifest.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    result = export_corpus(ExportConfig(
        root, output, conversion_manifest=manifest, converted_root=converted,
    ))
    assert (result.completed, result.failed, result.pending_conversion) == (1, 0, 0)
    checked = verify_export(
        source_root=root, output_root=output,
        conversion_manifest=manifest, converted_root=converted,
    )
    assert checked.complete and checked.verified_documents == 1
    assert source.read_bytes() == original
    row = json.loads((output / "manifest.jsonl").read_text(encoding="utf-8"))
    assert row["source_path"] == str(source)


@pytest.mark.parametrize("suffix", [".docx", ".docm"])
def test_native_xml_and_macro_container_cannot_enter_conversion(tmp_path, suffix):
    root = tmp_path / "source"
    root.mkdir()
    source = root / ("source" + suffix)
    source.write_bytes(docx() if suffix == ".docx" else OLE + docx())
    with pytest.raises(ValueError, match="source"):
        convert_document(
            source, source_root=root, output_root=tmp_path / "out", converter=None,
        )


@pytest.mark.parametrize("suffix", [".docx", ".docm"])
def test_manifest_cannot_substitute_native_xml_or_docm_source(tmp_path, suffix):
    from hashlib import sha256

    root = tmp_path / "source"
    root.mkdir()
    source = root / ("source" + suffix)
    source.write_bytes(docx())
    converted = tmp_path / "converted"
    converted.mkdir()
    target = converted / "result.docx"
    target.write_bytes(docx())
    manifest = converted / "manifest.jsonl"
    manifest.write_text(json.dumps({
        "schema_version": "word-conversion-v1", "status": "converted",
        "source_path": str(source), "source_sha256": sha256(source.read_bytes()).hexdigest(),
        "converted_path": str(target), "converted_sha256": sha256(target.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    output = tmp_path / "out"
    export_corpus(ExportConfig(root, output))
    with pytest.raises(ExportError, match="invalid_conversion_extensions"):
        export_corpus(ExportConfig(
            root, output, conversion_manifest=manifest, converted_root=converted,
        ))
    checked = verify_export(source_root=root, output_root=output,
                            conversion_manifest=manifest, converted_root=converted)
    assert not checked.complete
    assert any(error.code == "invalid_conversion_paths" for error in checked.errors)


def test_unread_large_media_is_not_decompressed(tmp_path, monkeypatch):
    source = tmp_path / "media.docx"
    source.write_bytes(docx(media=b"x" * 2000))
    original_read = ZipFile.read
    reads = []

    def read(archive, name, *args, **kwargs):
        reads.append(name)
        assert name == "word/document.xml"
        return original_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(ZipFile, "read", read)
    document = DocxExportReader(max_member_bytes=500, max_total_bytes=3000).read(source)
    assert document.paragraphs[0].text == "第一条 合成条文。"
    assert reads == ["word/document.xml"]


@pytest.mark.parametrize("part", ["main", "auxiliary"])
def test_selected_xml_member_remains_bounded(tmp_path, part):
    source = tmp_path / "large.docx"
    source.write_bytes(docx(**{part: XML + b" " * 1000}))
    with pytest.raises(ExportReadError, match="expanded_size_limit"):
        DocxExportReader(max_member_bytes=500).read(source)


def test_unread_media_still_counts_toward_archive_total(tmp_path):
    source = tmp_path / "large.docx"
    source.write_bytes(docx(media=b"x" * 2000))
    with pytest.raises(ExportReadError, match="expanded_size_limit"):
        DocxExportReader(max_member_bytes=500, max_total_bytes=2000).read(source)


def test_unread_media_still_counts_toward_member_count(tmp_path):
    source = tmp_path / "large.docx"
    source.write_bytes(docx(media=b"x" * 2000))
    with pytest.raises(ExportReadError, match="zip_member_count_limit"):
        DocxExportReader(max_members=1).read(source)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["ole-docx", "native-docx", "docm"])
def test_both_word_drivers_gate_format_before_word(tmp_path, monkeypatch, batch, kind):
    from lawyer_agent.infrastructure.documents import word_driver
    from lawyer_agent.infrastructure.documents.conversion_paths import ConversionPaths
    from lawyer_agent.infrastructure.documents.word_driver import (
        BatchPowerShellWordConverter,
        PowerShellWordConverter,
    )

    class ReachedWordBoundary(Exception):
        pass

    def stop():
        raise ReachedWordBoundary

    monkeypatch.setattr(word_driver, "os", SimpleNamespace(
        **(vars(word_driver.os) | {"name": "posix"}),
    ))
    monkeypatch.setattr(word_driver, "shutil", SimpleNamespace(
        **(vars(word_driver.shutil) | {"which": lambda _name: None}),
    ))

    cls = BatchPowerShellWordConverter if batch else PowerShellWordConverter
    driver = object.__new__(cls)
    root = tmp_path / "source"
    root.mkdir()
    driver._paths = ConversionPaths(root, tmp_path / "out")
    driver._process = None
    monkeypatch.setattr(driver, "_start" if batch else "recover_pending", stop)
    path = root / ("source.docm" if kind == "docm" else "source.docx")
    path.write_bytes((OLE if kind != "native-docx" else b"") + docx())
    error = ReachedWordBoundary if kind == "ole-docx" else ValueError
    with pytest.raises(error):
        driver(path, tmp_path / "out/result.docx")


def test_cli_selects_only_legacy_sources_including_disguised_docx(tmp_path, monkeypatch):
    from lawyer_agent.cli import corpus_convert

    root = tmp_path / "source"
    root.mkdir()
    for name, prefix in [("legacy.doc", OLE), ("disguised.docx", OLE),
                         ("native.docx", b""), ("macro.docm", OLE)]:
        (root / name).write_bytes(prefix + docx())

    class CLIConverter(SyntheticConverter):
        def __init__(self, **kwargs):
            pass

        def recover_pending(self):
            pass

    monkeypatch.setattr(corpus_convert, "PowerShellWordConverter", CLIConverter)
    output = tmp_path / "out"
    assert corpus_convert.main(["--source-root", str(root), "--output-root", str(output)]) == 0
    records = [json.loads(row) for row in (output / "manifest.jsonl").read_text(
        encoding="utf-8",
    ).splitlines()]
    assert {Path(row["source_path"]).name for row in records} == {"legacy.doc", "disguised.docx"}
