from io import BytesIO
from zipfile import ZipFile

import pytest

from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader, ExportReadError

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
PARAGRAPH = "<w:p><w:r><w:t>第一条 合成正文。</w:t></w:r></w:p>"


def archive(xml: str) -> bytes:
    stream = BytesIO()
    with ZipFile(stream, "w") as zipped:
        zipped.writestr("word/document.xml", xml)
    return stream.getvalue()


def test_reader_uses_immutable_bytes_and_preserves_docm_flag():
    payload = archive(f"<w:document {NS}><w:body>{PARAGRAPH}</w:body></w:document>")
    result = DocxExportReader().read_bytes(payload, source_suffix=".docm")
    assert [p.text for p in result.paragraphs] == ["第一条 合成正文。"]
    assert "macro_enabled_container_no_execution" in result.quality_flags


@pytest.mark.parametrize(
    "xml",
    [
        f"<w:hdr {NS}>{PARAGRAPH}</w:hdr>",
        f"<w:document {NS}>{PARAGRAPH}</w:document>",
        f"<w:document {NS}><w:body>{PARAGRAPH}</w:body><w:body/></w:document>",
    ],
)
def test_main_part_requires_document_and_single_direct_body(tmp_path, xml):
    path = tmp_path / "synthetic.docx"
    path.write_bytes(archive(xml))
    with pytest.raises(ExportReadError, match="invalid_document_body"):
        DocxExportReader().read(path)


def test_main_part_rejects_paragraphs_outside_body(tmp_path):
    xml = f"<w:document {NS}>{PARAGRAPH}<w:body>{PARAGRAPH}</w:body></w:document>"
    path = tmp_path / "synthetic.docx"
    path.write_bytes(archive(xml))
    with pytest.raises(ExportReadError, match="invalid_document_body"):
        DocxExportReader().read(path)


def test_bytes_reader_keeps_zip_and_expansion_limits():
    with pytest.raises(ExportReadError, match="invalid_docx_zip"):
        DocxExportReader().read_bytes(b"not a zip")
    payload = archive(f"<w:document {NS}><w:body>{PARAGRAPH}</w:body></w:document>")
    with pytest.raises(ExportReadError, match="expanded_size_limit"):
        DocxExportReader(max_member_bytes=1).read_bytes(payload)
