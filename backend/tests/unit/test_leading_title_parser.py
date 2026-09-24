from __future__ import annotations

import pytest

from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser


def _document(*texts: str, ordinals: tuple[int, ...] | None = None) -> ParsedDocument:
    values = ordinals or tuple(range(len(texts)))
    return ParsedDocument(
        tuple(ParsedParagraph(text, ordinal=values[index]) for index, text in enumerate(texts)),
        "synthetic",
    )


def _parse_v4(*texts: str, ordinals: tuple[int, ...] | None = None):
    return LegalStructureParser(profile="corpus-docx-v4").parse(
        _document(*texts, ordinals=ordinals)
    )


@pytest.mark.parametrize(
    "texts",
    [
        (
            "某机关关于《某法》第一百条、",
            "第一百零一条和第一百零二条的解释",
            "（2020年1月2日通过）",
            "第一条 本解释所称术语。",
            "第二条 其他规定。",
        ),
        (
            "某机关关于\n<某法>\n第七十七条适用问题的解释",
            "（2020年1月2日批准）",
            "第一条 施行。",
        ),
        (
            "某机关关于《某条例》",
            "第四十四条处罚权限规定的决定",
            "（2020年1月2日公布）",
            "第一条 决定正文。",
        ),
        (
            "某机关关于《某法》",
            "第三条第一款的批复",
            "（2020年1月2日通过）",
            "第一条 批复正文。",
        ),
        (
            "某机关关于《某地区基本法》",
            "第一百零四条的解释",
            "（2020年1月2日通过）",
            "第一条 解释正文。",
        ),
        (
            "某机关关于《某法》",
            "第三百四十四条有关问题的批复",
            "（2020年1月2日通过）",
            "第一条 批复正文。",
        ),
        (
            "某机关关于《某法》",
            "第一百条的解释",
            "（2020年1月2日通过（补充））",
            "第一条 解释正文。",
        ),
    ],
)
def test_v4_classifies_bounded_leading_title_and_preserves_real_articles(texts):
    parsed = _parse_v4(*texts)
    assert [article.provision_no for article in parsed.articles] == [
        "第一条",
        *( ["第二条"] if texts[-1].startswith("第二条") else []),
    ]
    assert parsed.leading_title_spans
    for span in parsed.leading_title_spans:
        paragraph = texts[span.paragraph_index]
        assert paragraph[span.start:span.end] == span.text
        assert span.ordinal == span.paragraph_ordinal


def test_v4_accepts_split_closed_history_and_one_strict_law_number():
    parsed = _parse_v4(
        "某机关关于《某法》",
        "第一百条的解释",
        "法释〔2020〕2号",
        "（2020年1月2日经某会议",
        "通过）",
        "第一条 正文。",
        ordinals=(8, 8, 9, 10, 11, 12),
    )
    assert [article.provision_no for article in parsed.articles] == ["第一条"]
    assert {span.paragraph_index for span in parsed.leading_title_spans} == {0, 1}
    assert [span.paragraph_ordinal for span in parsed.leading_title_spans] == [8, 8]


@pytest.mark.parametrize(
    "texts",
    [
        ("某机关关于《某法》", "第一条的解释", "第一条 正文。"),
        ("某机关关于《某法》", "第一条的解释", "（2020年1月2日通过"),
        ("某机关关于《某法》", "第一条的解释", "（2020年1月2日通过]"),
        ("某机关关于某法", "第一条的解释", "（2020年1月2日通过）"),
        ("第一条 本法适用于合成事项。", "关于《某法》的解释", "（2020年1月2日通过）"),
        ("某机关关于《某法》", "第一条的解释如下：", "（2020年1月2日通过）"),
        ("某机关：关于《某法》", "第一条的解释", "（2020年1月2日通过）"),
        ("某机关关于《某法》", "第一条 本条例应当施行。", "（2020年1月2日通过）"),
        ("某机关关于《某法》本法应当施行", "第一条的解释", "（2020年1月2日通过）"),
        ("某机关关于《本法应当施行。》", "第一条的解释", "（2020年1月2日通过）"),
        ("某机关关于《某法：解释》", "第一条的解释", "（2020年1月2日通过）"),
        ("某机关关于《某法》", "第三百四十四条相关问题的批复", "（2020年1月2日通过）"),
        ("某机关关于《某法》", "第一条的解释", "（2020年1月1日通过(补充）"),
        ("某机关关于《某法》", "第一条的解释", "（2020年1月1日通过（补充)）"),
        (
            "某机关关于《某法》", "第一条的解释", "法释〔2020〕2号",
            "法释〔2020〕3号", "（2020年1月2日通过）",
        ),
    ],
)
def test_v4_rejects_uncertain_or_body_like_leading_regions(texts):
    parsed = _parse_v4(*texts)
    assert parsed.leading_title_spans == ()


def test_v4_refuses_probe_limits():
    too_many = tuple([
        "某机关关于《某法》第一条的解释",
        *(["空白标题"] * 11),
        "（2020年1月2日通过）",
    ])
    assert _parse_v4(*too_many).leading_title_spans == ()
    too_long = "某机关关于《某法》" + "甲" * 4070 + "第一条的解释"
    assert _parse_v4(too_long, "（2020年1月2日通过）").leading_title_spans == ()


def test_v4_probe_limit_does_not_count_body_paragraphs_after_history():
    parsed = _parse_v4(
        "某机关关于《某法》",
        "第一百条的解释",
        "（2020年1月2日通过）",
        *(f"第{index}条 正文。" for index in range(1, 20)),
    )
    assert parsed.leading_title_spans
    assert len(parsed.articles) == 19


def test_repeated_ordinal_does_not_exempt_body_like_title_segment():
    parsed = _parse_v4(
        "某机关关于《某法》",
        "第一条 本法应当施行。",
        "（2020年1月2日通过）",
        ordinals=(4, 4, 5),
    )
    assert parsed.leading_title_spans == ()


def test_default_and_v3_profiles_keep_existing_article_classification():
    document = _document(
        "某机关关于《某法》", "第一百条的解释", "（2020年1月2日通过）", "第一条 正文。"
    )
    default_articles = LegalStructureParser().parse(document).articles
    assert [article.provision_no for article in default_articles] == ["第一百条", "第一条"]
    assert [
        article.provision_no
        for article in LegalStructureParser(profile="corpus-docx-v3").parse(document).articles
    ] == ["第一百条", "第一条"]
