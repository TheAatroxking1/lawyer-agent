from __future__ import annotations

from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser


def _document(lines: list[str]) -> ParsedDocument:
    return ParsedDocument(
        paragraphs=tuple(
            ParsedParagraph(text=line, ordinal=index)
            for index, line in enumerate(lines)
        ),
        source_ref="object://corpus/sample",
    )


def test_parser_groups_articles_under_chapters_and_sections() -> None:
    document = _document(
        [
            "序言",
            "本序言内容。",
            "第一章 总则",
            "第一节 一般规定",
            "第一条 为了保护民事权益，制定本法。",
            "本款为该条的延续。",
            "第二条 调整民事关系。",
            "第二章 分则",
            "第三条 分则内容。",
        ]
    )
    instrument = LegalStructureParser().parse(document)
    assert len(instrument.articles) == 3
    assert instrument.articles[0].provision_no == "第一条"
    assert instrument.articles[0].structure_path == ("第一章 总则", "第一节 一般规定")
    assert "本款为该条的延续。" in instrument.articles[0].text
    assert instrument.articles[1].structure_path == ("第一章 总则", "第一节 一般规定")
    assert instrument.articles[2].structure_path == ("第二章 分则",)
    assert instrument.articles[0].char_start < instrument.articles[0].char_end


def test_parser_rejects_cross_article_joining_by_heading() -> None:
    document = _document(
        [
            "第一条 甲。",
            "第二条 乙。",
        ]
    )
    instrument = LegalStructureParser().parse(document)
    assert [article.provision_no for article in instrument.articles] == [
        "第一条",
        "第二条",
    ]
    assert instrument.articles[0].text == "第一条 甲。"
    assert instrument.articles[1].text == "第二条 乙。"
