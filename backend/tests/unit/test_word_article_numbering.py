from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest

from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader, ExportReadError

NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def payload(*, start=4, override=None, template="第%1条", num_id="1", numbering=True,
            ilvl="0", texts=("甲正文。", "乙正文。")):
    paragraphs = ''.join(
        f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{ilvl}"/><w:numId w:val="{num_id}"/>'
        f'</w:numPr></w:pPr>{f"<w:r><w:t>{text}</w:t></w:r>" if text else ""}</w:p>'
        for text in texts
    )
    extra = (f'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="{override}"/>'
             '</w:lvlOverride>') if override is not None else ''
    definition = (
        f'<w:numbering {NS}><w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="{ilvl}">'
        f'<w:start w:val="{start}"/><w:numFmt w:val="chineseCounting"/>'
        f'<w:lvlText w:val="{template}"/><w:suff w:val="space"/>'
        '</w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/>'
        f'{extra}</w:num></w:numbering>'
    ).encode()
    stream = BytesIO()
    with ZipFile(stream, 'w') as archive:
        archive.writestr('word/document.xml',
                         f'<w:document {NS}><w:body>{paragraphs}</w:body></w:document>')
        if numbering:
            archive.writestr('word/numbering.xml', definition)
    return stream.getvalue(), definition


@pytest.mark.parametrize(('start', 'override', 'marker'), [(4, None, '四'), (6, 6, '六'),
                                                         (10, None, '十'), (3, None, '三')])
def test_explicit_article_numbering_is_restored_with_provenance(start, override, marker):
    raw, definitions = payload(start=start, override=override)
    result = DocxExportReader().read_bytes(raw)
    assert result.paragraphs[0].text == f'第{marker}条 甲正文。'
    evidence = result.paragraphs[0].numbering_provenance
    assert evidence.original_text == '甲正文。'
    assert evidence.numbering_sha256 == sha256(definitions).hexdigest()
    assert (evidence.num_id, evidence.abstract_num_id, evidence.ilvl) == (1, 0, 0)
    assert evidence.start == start
    assert evidence.value == start and evidence.xml_paragraph_ordinal == 0
    assert evidence.start_override == override
    assert result.loader_version == 'docx-export-reader-v2:numbering-v1'
    assert 'article_numbering_restored' in result.quality_flags


def test_article_template_allows_trailing_ideographic_space():
    raw, _ = payload(template="第%1条　")
    assert DocxExportReader().read_bytes(raw).paragraphs[0].text == "第四条 甲正文。"


@pytest.mark.parametrize('options', [{'template': '%1.'}, {'num_id': '0'}, {'numbering': False}])
def test_ordinary_unknown_and_cancelled_lists_keep_existing_text(options):
    raw, _ = payload(**options)
    result = DocxExportReader().read_bytes(raw)
    assert result.paragraphs[0].text == '甲正文。'
    assert result.loader_version == 'docx-export-reader-v2'


def test_ordinary_numbering_keeps_unresolved_quality_flag():
    raw, _ = payload(template="%1.")
    result = DocxExportReader().read_bytes(raw)
    assert "automatic_numbering_not_resolved" in result.quality_flags


def test_override_is_authoritative_and_sequence_advances():
    raw, _ = payload(start=1, override=6)
    assert [p.text for p in DocxExportReader().read_bytes(raw).paragraphs] == [
        '第六条 甲正文。', '第七条 乙正文。',
    ]


def test_numbering_xml_has_same_dtd_and_size_limits():
    raw, _ = payload()
    stream = BytesIO()
    with ZipFile(BytesIO(raw)) as old, ZipFile(stream, 'w') as new:
        new.writestr('word/document.xml', old.read('word/document.xml'))
        new.writestr('word/numbering.xml', '<!DOCTYPE x><x/>')
    with pytest.raises(ExportReadError, match='xml_entities_forbidden'):
        DocxExportReader().read_bytes(stream.getvalue())


def test_ambiguous_numbering_definition_is_not_guessed():
    raw, definitions = payload()
    duplicate = definitions.replace(
        b'</w:numbering>',
        b'<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num></w:numbering>',
    )
    stream = BytesIO()
    with ZipFile(BytesIO(raw)) as old, ZipFile(stream, 'w') as new:
        new.writestr('word/document.xml', old.read('word/document.xml'))
        new.writestr('word/numbering.xml', duplicate)
    result = DocxExportReader().read_bytes(stream.getvalue())
    assert result.paragraphs[0].text == '甲正文。'
    assert result.paragraphs[0].numbering_provenance is None


def test_valid_and_invalid_duplicate_num_id_fails_closed():
    raw, definitions = payload()
    duplicate = definitions.replace(
        b'</w:numbering>',
        b'<w:num w:numId="1"><w:abstractNumId w:val="999"/></w:num></w:numbering>',
    )
    result = DocxExportReader().read_bytes(_replace_numbering(raw, duplicate))
    assert [paragraph.text for paragraph in result.paragraphs] == ['甲正文。', '乙正文。']
    assert all(paragraph.numbering_provenance is None for paragraph in result.paragraphs)
    assert 'automatic_numbering_not_resolved' in result.quality_flags


def test_duplicate_start_override_fails_closed():
    raw, definitions = payload(start=4, override=6)
    duplicate = definitions.replace(
        b'</w:num>',
        b'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="7"/></w:lvlOverride></w:num>',
    )
    result = DocxExportReader().read_bytes(_replace_numbering(raw, duplicate))
    assert [paragraph.text for paragraph in result.paragraphs] == ['甲正文。', '乙正文。']
    assert all(paragraph.numbering_provenance is None for paragraph in result.paragraphs)


def test_empty_numbered_paragraph_preserves_visible_article_heading_and_sequence():
    raw, _ = payload(texts=('', '甲正文。', '乙正文。'))
    result = DocxExportReader().read_bytes(raw)
    assert [paragraph.text for paragraph in result.paragraphs] == [
        '第四条', '第五条 甲正文。', '第六条 乙正文。',
    ]
    assert [paragraph.numbering_provenance.value for paragraph in result.paragraphs] == [4, 5, 6]
    assert [paragraph.numbering_provenance.xml_paragraph_ordinal
            for paragraph in result.paragraphs] == [0, 1, 2]
    assert [paragraph.numbering_provenance.original_text
            for paragraph in result.paragraphs] == ['', '甲正文。', '乙正文。']


def test_empty_numbered_heading_keeps_following_plain_body_out_of_previous_article():
    from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
    from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser

    raw, _ = payload(start=3, texts=('甲正文。', ''))
    raw = _append_plain_paragraph(raw, '乙正文。')
    exported = DocxExportReader().read_bytes(raw)
    assert [paragraph.text for paragraph in exported.paragraphs] == [
        '第三条 甲正文。', '第四条', '乙正文。',
    ]
    parsed = LegalStructureParser().parse(ParsedDocument(
        paragraphs=tuple(ParsedParagraph(paragraph.text, ordinal=paragraph.ordinal)
                         for paragraph in exported.paragraphs),
        source_ref='memory.docx',
    ))
    assert [(article.provision_no, article.text) for article in parsed.articles] == [
        ('第三条', '第三条 甲正文。'),
        ('第四条', '第四条乙正文。'),
    ]


def test_nonzero_level_article_template_is_not_guessed():
    raw, _ = payload(ilvl="1")
    result = DocxExportReader().read_bytes(raw)
    assert [paragraph.text for paragraph in result.paragraphs] == ['甲正文。', '乙正文。']
    assert all(paragraph.numbering_provenance is None for paragraph in result.paragraphs)
    assert 'automatic_numbering_not_resolved' in result.quality_flags


def _replace_numbering(raw: bytes, numbering: bytes) -> bytes:
    stream = BytesIO()
    with ZipFile(BytesIO(raw)) as old, ZipFile(stream, 'w') as new:
        new.writestr('word/document.xml', old.read('word/document.xml'))
        new.writestr('word/numbering.xml', numbering)
    return stream.getvalue()


def _append_plain_paragraph(raw: bytes, text: str) -> bytes:
    stream = BytesIO()
    with ZipFile(BytesIO(raw)) as old, ZipFile(stream, 'w') as new:
        document = old.read('word/document.xml').replace(
            b'</w:body>', f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>'.encode(),
        )
        new.writestr('word/document.xml', document)
        new.writestr('word/numbering.xml', old.read('word/numbering.xml'))
    return stream.getvalue()


def test_corpus_source_preserves_numbering_audit(tmp_path):
    from lawyer_agent.infrastructure.documents.corpus_source import (
        CorpusSourceRequest,
        prepare_corpus_source,
    )

    raw, _ = payload(start=4)
    source = tmp_path / 'numbered.docx'
    source.write_bytes(raw)
    prepared = prepare_corpus_source(CorpusSourceRequest(
        source=source,
        source_root=tmp_path,
        expected_source_sha256=sha256(raw).hexdigest(),
    ))
    paragraph = prepared.document.paragraphs[0]
    assert paragraph.text == '第四条 甲正文。'
    assert paragraph.numbering_provenance is not None
    assert paragraph.numbering_provenance.original_text == '甲正文。'
    assert prepared.source_sha256 == prepared.input_sha256 == sha256(raw).hexdigest()
