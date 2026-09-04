from __future__ import annotations

import io
import zipfile

import pytest

from lawyer_agent.infrastructure.documents.docx_loader import InvalidDocx, ZipDocxLoader
from lawyer_agent.infrastructure.documents.loader import ParsedDocument

_MINIMAL_DOC = (
    "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
    "<w:p><w:r><w:t>中华人民共和国民法典</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>第一编 总则</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>第一条 为了保护民事主体的合法权益，制定本法。</w:t></w:r></w:p>"
    "</w:body></w:document>"
)


def _docx_bytes(document_xml: str = _MINIMAL_DOC) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" '
                'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml'
                '.document.main+xml"/></Types>'
            ),
        )
        archive.writestr("word/document.xml", document_xml)
    return buffer.getvalue()


def test_loader_extracts_paragraphs_in_order() -> None:
    loader = ZipDocxLoader(source_ref="object://corpus/minimal.docx")
    document = loader.load("object://corpus/minimal.docx", _docx_bytes())
    assert isinstance(document, ParsedDocument)
    assert len(document.paragraphs) == 3
    assert document.paragraphs[0].text == "中华人民共和国民法典"
    assert document.paragraphs[1].text == "第一编 总则"
    assert document.paragraphs[2].text == "第一条 为了保护民事主体的合法权益，制定本法。"


def test_loader_rejects_doctype_and_entities() -> None:
    loader = ZipDocxLoader(source_ref="object://corpus/evil.docx")
    payload = _docx_bytes(
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        "<w:document><w:body></w:body></w:document>"
    )
    with pytest.raises(InvalidDocx, match="DTD"):
        loader.load("object://corpus/evil.docx", payload)


def test_loader_rejects_macros_and_non_zip() -> None:
    loader = ZipDocxLoader(source_ref="object://corpus/macro.docm")
    with pytest.raises(InvalidDocx):
        loader.load("object://corpus/macro.docm", b"not-a-zip")
