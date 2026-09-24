from __future__ import annotations

import pytest

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


def test_ordinary_articles_without_spaces_keep_separate_boundaries() -> None:
    lines = ["第一条甲。", "第二条乙。"]
    articles = LegalStructureParser().parse(_document(lines)).articles
    assert [article.provision_no for article in articles] == ["第一条", "第二条"]
    assert [article.text for article in articles] == lines
    assert [article.paragraphs for article in articles] == [(lines[0],), (lines[1],)]
    assert articles[0].char_end == articles[1].char_start


def test_ordinary_compact_article_between_spaced_articles_is_not_joined() -> None:
    lines = ["第一条 甲。", "第二条乙。", "第三条 丙。"]
    articles = LegalStructureParser().parse(_document(lines)).articles
    assert [article.provision_no for article in articles] == ["第一条", "第二条", "第三条"]
    assert [article.text for article in articles] == lines


@pytest.mark.parametrize("prefix", [[], ["第一条 甲。"]])
def test_ambiguous_supplement_cannot_fall_back_or_join_previous_article(
    prefix: list[str],
) -> None:
    lines = [*prefix, "第一条之一新增独立义务。", "第二条 乙。"]
    with pytest.raises(ValueError, match="ambiguous supplement article heading"):
        LegalStructureParser().parse(_document(lines))


@pytest.mark.parametrize(
    ("base", "base_no", "suffixes"),
    [
        ("第一百二十条", "第一百二十条", ("一", "二")),
        ("第120条", "120", ("1", "2")),
        ("第１２０条", "１２０", ("１", "２")),
        ("第120条", "120", ("一", "二")),
    ],
)
def test_supplement_articles_keep_distinct_identifiers_and_source_boundaries(
    base: str, base_no: str, suffixes: tuple[str, str],
) -> None:
    lines = [
        f"{base} 基础条文。",
        f"{base}之{suffixes[0]}\u3000增补条文甲。",
        "增补条文甲的第二款。",
        f"{base}之{suffixes[1]} 增补条文乙。",
    ]
    articles = LegalStructureParser().parse(_document(lines)).articles
    assert [article.provision_no for article in articles] == [
        base_no, f"{base}之{suffixes[0]}", f"{base}之{suffixes[1]}",
    ]
    assert [article.paragraphs for article in articles] == [
        (lines[0],), (lines[1], lines[2]), (lines[3],),
    ]
    joined = "".join(lines)
    assert "".join(article.text for article in articles) == joined
    for article in articles:
        assert joined[article.char_start:article.char_end] == article.text


@pytest.mark.parametrize(
    "reference",
    [
        "依照第一条之一规定办理。",
        "依照第120条之12规定的情形，依照前款处理。",
        "依照第１２０条之１２规定的情形，依照前款处理。",
    ],
)
def test_supplement_references_inside_body_remain_body(reference: str) -> None:
    lines = ["第三条 合成条文。", reference, "第四条 后续条文。"]
    articles = LegalStructureParser().parse(_document(lines)).articles
    assert [article.provision_no for article in articles] == ["第三条", "第四条"]
    assert articles[0].paragraphs == (lines[0], reference)


def test_supplement_heading_can_end_paragraph_or_precede_bracketed_title() -> None:
    lines = ["第一条之一", "增补条文正文。", "第一条之二【合成标题】另一条文。"]
    articles = LegalStructureParser().parse(_document(lines)).articles
    assert [article.provision_no for article in articles] == ["第一条之一", "第一条之二"]
    assert articles[0].text == "".join(lines[:2])
    assert articles[1].text == lines[2]
