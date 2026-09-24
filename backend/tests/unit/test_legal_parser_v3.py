import pytest

from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser


def parse(*texts):
    return LegalStructureParser().parse(ParsedDocument(
        tuple(ParsedParagraph(text, ordinal=i) for i, text in enumerate(texts)), "synthetic",
    )).articles


@pytest.mark.parametrize("marker", ["第一百零一条", "第一百〇一条", "第101条", "第１０１条"])
def test_zero_and_numeric_article_heads_are_retained(marker):
    articles = parse("第一百条 甲。", marker + " 乙。", "第一百零二条 丙。")
    assert len(articles) == 3
    assert articles[1].text == marker + " 乙。"


def test_horizontal_space_inside_marker_preserves_body_but_canonicalizes_identity():
    articles = parse("第二十八条 甲。", "第二十九  条乙。", "第三十条 丙。")
    assert [a.provision_no for a in articles] == ["第二十八条", "第二十九条", "第三十条"]
    assert articles[1].text == "第二十九  条乙。"


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\v", "\u2028", "\u2029"])
def test_embedded_clear_head_is_separate_article_without_cross_article_text(newline):
    articles = parse("第一条 甲。" + newline + "  第二条 乙。", "第三条 丙。")
    assert [a.provision_no for a in articles] == ["第一条", "第二条", "第三条"]
    assert articles[0].text == "第一条 甲。"
    assert articles[1].paragraphs == ("第二条 乙。",)


def test_inline_reference_and_continuation_newline_stay_unchanged():
    text = "第一条 甲。\n依照第二条规定办理。\n续文。"
    article, = parse(text)
    assert article.text == text and article.paragraphs == (text,)


@pytest.mark.parametrize('reference', [
    '第四条第（八）、第（九）项规定的事项，依规定执行。',
    '第十八条第一款规定范围内还禁止其他事项。',
    '第十三条第二款所列建设工程由有关部门办理。',
])
def test_explicit_subarticle_reference_is_continuation(reference):
    articles = parse('第一条 甲。', reference, '第二条 乙。')
    assert len(articles) == 2
    assert reference in articles[0].text


def test_compact_internal_head_is_ambiguous_and_fails_closed():
    with pytest.raises(ValueError):
        parse("第一条 甲。\n第二条规定的事项。")


def test_internal_ambiguous_supplement_does_not_merge_into_previous_article():
    with pytest.raises(ValueError):
        parse("第一条 甲。\n第一条之二规定的事项。")
