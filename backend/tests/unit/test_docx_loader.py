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


def _docx_bytes(document_xml: str | bytes = _MINIMAL_DOC) -> bytes:
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


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
@pytest.mark.parametrize("declaration", [
    '<!DOCTYPE w:document [<!ENTITY e "ENTITY_EXPANDED">]>',
    '<!DOCTYPE w:document SYSTEM "file:///never-read.dtd">',
])
def test_loader_rejects_xml_declarations_including_bomless_utf16(encoding, declaration):
    xml = (
        declaration
        + '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>'
        + ('&e;' if 'ENTITY' in declaration else '安全正文')
        + '</w:t></w:r></w:p></w:body></w:document>'
    )
    payload = _docx_bytes(xml.encode(encoding))
    with pytest.raises(InvalidDocx, match="DTD|entities"):
        ZipDocxLoader("synthetic.docx").load("synthetic.docx", payload)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be"])
def test_loader_preserves_legal_chinese_across_xml_encodings(encoding):
    xml = _MINIMAL_DOC.split("?>", 1)[1]
    result = ZipDocxLoader("synthetic.docx").load(
        "synthetic.docx", _docx_bytes(xml.encode(encoding)),
    )
    assert [p.text for p in result.paragraphs] == [
        "中华人民共和国民法典", "第一编 总则", "第一条 为了保护民事主体的合法权益，制定本法。",
    ]


def _archive_parts(parts: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.mark.parametrize("limit", ["xml", "total", "members"])
def test_loader_checks_expanded_limits_before_reading_members(monkeypatch, limit):
    from lawyer_agent.infrastructure.documents import docx_loader

    parts = {"word/document.xml": _MINIMAL_DOC.encode()}
    if limit == "xml":
        monkeypatch.setattr(docx_loader, "_MAX_XML_BYTES", 100, raising=False)
    elif limit == "total":
        parts["word/media/image.emf"] = b"x" * 2000
        monkeypatch.setattr(docx_loader, "_MAX_EXPANDED_BYTES", 2000, raising=False)
    else:
        parts["unused.xml"] = b"unused"
        monkeypatch.setattr(docx_loader, "_MAX_ZIP_MEMBERS", 1, raising=False)
    payload = _archive_parts(parts)

    def forbidden_read(*args, **kwargs):
        pytest.fail("expanded ZIP must be rejected before any member is read")

    monkeypatch.setattr(zipfile.ZipFile, "read", forbidden_read)
    with pytest.raises(InvalidDocx, match="limit"):
        ZipDocxLoader("synthetic.docx").load("synthetic.docx", payload)


def test_loader_does_not_read_media_or_apply_xml_limit_to_media(monkeypatch):
    from lawyer_agent.infrastructure.documents import docx_loader

    monkeypatch.setattr(docx_loader, "_MAX_XML_BYTES", 1000, raising=False)
    payload = _archive_parts({
        "word/document.xml": _MINIMAL_DOC.encode(), "word/media/image.emf": b"x" * 2000,
    })
    original_read = zipfile.ZipFile.read
    reads = []

    def read(archive, name, *args, **kwargs):
        reads.append(name)
        assert name == "word/document.xml"
        return original_read(archive, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", read)
    result = ZipDocxLoader("synthetic.docx").load("synthetic.docx", payload)
    assert len(result.paragraphs) == 3
    assert reads == ["word/document.xml"]


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16-le"])
def test_loader_keeps_invalid_xml_in_stable_error_boundary(encoding):
    with pytest.raises(InvalidDocx, match="cannot be parsed"):
        ZipDocxLoader("synthetic.docx").load(
            "synthetic.docx", _docx_bytes("<broken>".encode(encoding)),
        )


def test_loader_keeps_existing_paragraph_style_break_and_empty_semantics():
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
        '<w:r><w:t>第一章</w:t><w:br/><w:t>总则</w:t><w:br/></w:r></w:p>'
        '<w:p/></w:body></w:document>'
    )
    result = ZipDocxLoader("synthetic.docx").load("synthetic.docx", _docx_bytes(xml))
    assert [(p.text, p.style, p.ordinal) for p in result.paragraphs] == [
        ("第一章\n总则", "Heading1", 0), ("", None, 1),
    ]
