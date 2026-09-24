from __future__ import annotations

import pytest

from lawyer_agent.infrastructure.documents import prefixed_article
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser


def _document(*texts: str) -> ParsedDocument:
    return ParsedDocument(
        tuple(ParsedParagraph(text, ordinal=index + 10) for index, text in enumerate(texts)),
        "synthetic",
    )


def _parse(profile: str | None, *texts: str):
    return LegalStructureParser(profile=profile).parse(_document(*texts))


def test_v4_recovers_closed_single_candidate_without_changing_source_text_or_spans():
    texts = ("第一条 甲。", "\ue004 \t第二条 乙。", "第三条 丙。")
    parsed = _parse("corpus-docx-v4", *texts)

    assert [article.provision_no for article in parsed.articles] == [
        "第一条", "第二条", "第三条",
    ]
    assert parsed.articles[1].text == texts[1]
    assert parsed.articles[1].paragraphs == (texts[1],)
    assert [(article.char_start, article.char_end) for article in parsed.articles] == [
        (0, len(texts[0])),
        (len(texts[0]), len(texts[0]) + len(texts[1])),
        (len(texts[0]) + len(texts[1]), sum(map(len, texts))),
    ]
    assert len(parsed.prefixed_article_spans) == 1
    span = parsed.prefixed_article_spans[0]
    assert (span.paragraph_index, span.paragraph_ordinal) == (1, 11)
    assert span.marker_start == 3
    assert span.prefix == "\ue004 \t"


@pytest.mark.parametrize("separator", [" ", "\t", " \t"])
def test_v4_recovers_empty_parenthesis_prefix_without_changing_source(
    separator: str,
):
    texts = ("第一条 甲。", f"（）{separator}第二条 乙。", "第三条 丙。")
    document = _document(*texts)

    parsed = LegalStructureParser(profile="corpus-docx-v4").parse(document)

    assert [article.provision_no for article in parsed.articles] == [
        "第一条", "第二条", "第三条",
    ]
    assert parsed.articles[1].text == texts[1]
    assert parsed.articles[1].paragraphs == (texts[1],)
    assert [(article.char_start, article.char_end) for article in parsed.articles] == [
        (0, len(texts[0])),
        (len(texts[0]), len(texts[0]) + len(texts[1])),
        (len(texts[0]) + len(texts[1]), sum(map(len, texts))),
    ]
    assert document.paragraphs[1].text == texts[1]
    assert len(parsed.prefixed_article_spans) == 1
    span = parsed.prefixed_article_spans[0]
    assert (span.paragraph_index, span.paragraph_ordinal) == (1, 11)
    expected_prefix = f"（）{separator}"
    assert span.marker_start == len(expected_prefix)
    assert span.prefix == expected_prefix
    if separator == " ":
        assert span.marker_start == 3


def test_v4_recovers_an_exact_consecutive_candidate_run_with_continuations():
    parsed = _parse(
        "corpus-docx-v4",
        "第八条 甲。", "续文。", "\ue004 第九条 乙。", "普通续段。",
        "\ue004 第十条 丙。", "第十一条 丁。",
    )
    assert [article.provision_no for article in parsed.articles] == [
        "第八条", "第九条", "第十条", "第十一条",
    ]
    assert parsed.articles[1].paragraphs == ("\ue004 第九条 乙。", "普通续段。")
    assert [span.marker_start for span in parsed.prefixed_article_spans] == [2, 2]


def test_v4_recovers_consecutive_mixed_legacy_and_empty_parenthesis_prefixes():
    parsed = _parse(
        "corpus-docx-v4",
        "第八条 甲。", "（） 第九条 乙。", "\ue004\t第十条 丙。", "第十一条 丁。",
    )

    assert [article.provision_no for article in parsed.articles] == [
        "第八条", "第九条", "第十条", "第十一条",
    ]
    assert [span.prefix for span in parsed.prefixed_article_spans] == ["（） ", "\ue004\t"]
    assert [span.marker_start for span in parsed.prefixed_article_spans] == [3, 2]


def test_v4_rejects_huge_anchor_distance_without_iterating_the_numeric_interval(
    monkeypatch: pytest.MonkeyPatch,
):
    real_range = range

    def bounded_range(*args: int) -> range:
        if len(args) == 2 and args[1] - args[0] > 100:
            raise AssertionError("numeric article interval must stay bounded")
        return real_range(*args)

    monkeypatch.setattr(prefixed_article, "range", bounded_range, raising=False)

    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。",
        "\ue004 第二条 乙。",
        "第999999999999999999999999条 丙。",
    )

    assert parsed.prefixed_article_spans == ()


@pytest.mark.parametrize(
    "supplement",
    ("第二条之一 补充。", "\ue004 第二条之一 补充。"),
)
def test_v4_treats_supplementary_heading_as_interval_barrier(supplement: str):
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。",
        "\ue004 第二条 乙。",
        supplement,
        "第三条 丙。",
    )

    assert parsed.prefixed_article_spans == ()


def test_default_v3_and_nearby_profile_do_not_enable_prefixed_recovery():
    for prefix in ("\ue004 ", "（） "):
        texts = ("第一条 甲。", f"{prefix}第二条 乙。", "第三条 丙。")
        for profile in (None, "corpus-docx-v3", "corpus-docx-v4-extra"):
            parsed = _parse(profile, *texts)
            assert [article.provision_no for article in parsed.articles] == [
                "第一条", "第三条",
            ]
            assert parsed.prefixed_article_spans == ()
            assert texts[1] in parsed.articles[0].text


@pytest.mark.parametrize(
    "candidate",
    [
        "() 第二条 乙。",
        "（甲） 第二条 乙。",
        "（ ） 第二条 乙。",
        "（）第二条 乙。",
        "（）（） 第二条 乙。",
        "（）\ue004 第二条 乙。",
        "\ue004（） 第二条 乙。",
        "（）\n第二条 乙。",
    ],
)
def test_v4_rejects_non_exact_empty_parenthesis_prefixes(candidate: str):
    parsed = _parse("corpus-docx-v4", "第一条 甲。", candidate, "第三条 丙。")

    assert parsed.prefixed_article_spans == ()
    assert not any(article.text == candidate for article in parsed.articles[1:])


@pytest.mark.parametrize(
    "texts",
    [
        ("（） 第一条 甲。", "第二条 乙。"),
        ("第一条 甲。", "（） 第二条 乙。"),
        ("第一条 甲。", "（） 第三条 丙。", "第四条 丁。"),
        ("第一条 甲。", "（） 第二条 乙。", "（） 第二条 又乙。", "第三条 丙。"),
        ("第一条 甲。", "（） 第二条 乙。", "第四条 丁。"),
        ("第一条 甲。", "（） 第二条第一款规定办理。", "第三条 丙。"),
        ("第一条 甲。", "（） 第一条之二 补充。", "第二条 乙。"),
        ("第一条 甲。", "（） 第二条乙。", "第三条 丙。"),
        ("第一条 甲。", "（） 第二条 \n乙。", "第三条 丙。"),
        ("第一条 甲。", "（） 第二条 乙。", "第二条之一 补充。", "第三条 丙。"),
        ("第一条 甲。", "（） 第二条 乙。", "第二章 屏障", "第三条 丙。"),
    ],
)
def test_v4_applies_existing_anchor_and_barrier_gates_to_empty_parenthesis_prefix(
    texts: tuple[str, ...],
):
    parsed = _parse("corpus-docx-v4", *texts)

    assert parsed.prefixed_article_spans == ()


@pytest.mark.parametrize("opener, closer", [("“", "”"), ("（", "）"), ('"', '"')])
def test_v4_rejects_empty_parenthesis_candidate_inside_multiline_boundary(
    opener: str,
    closer: str,
):
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。", f"引文{opener}", "（） 第二条 乙。", f"引文结束{closer}", "第三条 丙。",
    )

    assert parsed.prefixed_article_spans == ()


def test_v4_rejects_empty_parenthesis_candidate_after_mismatched_boundary():
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。", "错配）", "（） 第二条 乙。", "第三条 丙。",
    )

    assert parsed.prefixed_article_spans == ()


@pytest.mark.parametrize(
    "texts",
    [
        ("第一条 甲。", "\ue005 第二条 乙。", "第三条 丙。"),
        ("第一条 甲。\ue004 第二条 乙。", "第三条 丙。"),
        ("\ue004 第一条 甲。", "第二条 乙。"),
        ("第一条 甲。", "\ue004 第二条 乙。"),
        ("第一条 甲。", "\ue004 第三条 丙。", "第四条 丁。"),
        ("第一条 甲。", "\ue004 第二条 乙。", "\ue004 第二条 又乙。", "第三条 丙。"),
        ("第一条 甲。", "\ue004 第二条 乙。", "第四条 丁。"),
        ("第一条 甲。", "\ue004 第二条第一款规定办理。", "第三条 丙。"),
        ("第一条 甲。", "\ue004 第一条之二 补充。", "第二条 乙。"),
        ("第一条 甲。", "\ue004 第二条乙。", "第三条 丙。"),
        ("第一条 甲。", "\ue004 第二条 \n乙。", "第三条 丙。"),
        ("第一条 甲。", "\ue004\ue004 第二条 乙。", "第三条 丙。"),
    ],
)
def test_v4_rejects_unclosed_or_non_exact_candidates(texts: tuple[str, ...]):
    parsed = _parse("corpus-docx-v4", *texts)
    assert parsed.prefixed_article_spans == ()
    assert not any(article.text.startswith("\ue004") for article in parsed.articles)


@pytest.mark.parametrize(
    "opener, closer",
    [
        ("“", "”"), ("《", "》"), ("（", "）"), ("(", ")"),
        ("【", "】"), ("[", "]"), ('"', '"'), ("「", "」"),
    ],
)
def test_v4_rejects_candidate_inside_quote_or_bracket_across_paragraphs(opener, closer):
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。", f"引文{opener}", "\ue004 第二条 乙。", f"引文结束{closer}", "第三条 丙。",
    )
    assert [article.provision_no for article in parsed.articles] == ["第一条", "第三条"]
    assert parsed.prefixed_article_spans == ()


def test_v4_rejects_candidate_when_the_right_anchor_is_inside_an_open_quote():
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。", "\ue004 第二条 乙。", "引文“", "第三条 丙。", "引文结束”",
    )
    assert parsed.prefixed_article_spans == ()


def test_v4_keeps_leading_title_article_reference_out_of_anchor_sequence():
    parsed = _parse(
        "corpus-docx-v4",
        "某机关关于《某法》", "第一百条的解释", "（2020年1月2日通过）",
        "第一条 甲。", "\ue004 第二条 乙。", "第三条 丙。",
    )
    assert [article.provision_no for article in parsed.articles] == ["第一条", "第二条", "第三条"]
    assert len(parsed.leading_title_spans) == 2
    assert len(parsed.prefixed_article_spans) == 1


@pytest.mark.parametrize("count", [1, 2, 8])
@pytest.mark.parametrize("separator", ["", " ", "\t", "\u3000"])
def test_v4_recovers_zero_width_prefix_preserving_all_characters_and_coordinates(
    count: int, separator: str,
):
    prefix = "\u200b" * count + separator
    texts = ("第二十一条 甲。", f"{prefix}第二十二条　乙。\u200b", "第二十三条 丙。")
    document = _document(*texts)

    parsed = LegalStructureParser(profile="corpus-docx-v4").parse(document)

    assert [article.provision_no for article in parsed.articles] == [
        "第二十一条", "第二十二条", "第二十三条",
    ]
    assert tuple(article.text for article in parsed.articles) == texts
    assert parsed.articles[1].paragraphs == (texts[1],)
    assert tuple(paragraph.text for paragraph in document.paragraphs) == texts
    assert [(article.char_start, article.char_end) for article in parsed.articles] == [
        (0, len(texts[0])),
        (len(texts[0]), len(texts[0]) + len(texts[1])),
        (len(texts[0]) + len(texts[1]), sum(map(len, texts))),
    ]
    assert parsed.prefixed_article_spans == (
        prefixed_article.PrefixedArticleSpan(1, 11, len(prefix), prefix),
    )


def test_v4_recovers_mixed_zero_width_and_existing_prefix_run():
    texts = (
        "第八条 甲。", "\u200b第九条 乙。", "普通续段。",
        "（） 第十条 丙。", "\ue004\t第十一条 丁。", "第十二条 戊。",
    )
    parsed = _parse("corpus-docx-v4", *texts)

    assert [article.provision_no for article in parsed.articles] == [
        "第八条", "第九条", "第十条", "第十一条", "第十二条",
    ]
    assert parsed.articles[1].paragraphs == texts[1:3]
    assert [span.prefix for span in parsed.prefixed_article_spans] == [
        "\u200b", "（） ", "\ue004\t",
    ]


@pytest.mark.parametrize("profile", [None, "corpus-docx-v3", "corpus-docx-v4-extra"])
def test_zero_width_prefix_recovery_requires_exact_v4_profile(profile: str | None):
    candidate = "\u200b\u200b第二条 乙。\u200b"
    parsed = _parse(profile, "第一条 甲。", candidate, "第三条 丙。")

    assert [article.provision_no for article in parsed.articles] == ["第一条", "第三条"]
    assert parsed.prefixed_article_spans == ()
    assert candidate in parsed.articles[0].text


@pytest.mark.parametrize(
    "prefix",
    [
        "\u200b" * 9, "\u200b \u200b", "\u200b\ue004 ", "\ue004\u200b",
        "\u200b（） ", "（） \u200b", "\u200b\u200c", "\u200b\ufeff",
        "\u200b\x00", " \u200b", "\u200b\n", "\u200b\r", "\u200b\v",
        "\u200b\f", "\u200b\u2028", "\u200b\u2029", "\u200b \n",
        "\u200b\x1c", "\u200b\x1d", "\u200b\x1e", "\u200b\x1f", "\u200b\x85",
    ],
)
def test_v4_rejects_non_exact_zero_width_prefix(prefix: str):
    parsed = _parse("corpus-docx-v4", "第一条 甲。", f"{prefix}第二条 乙。", "第三条 丙。")

    assert parsed.prefixed_article_spans == ()


@pytest.mark.parametrize(
    "texts",
    [
        ("\u200b第一条 甲。", "第二条 乙。"),
        ("第一条 甲。", "\u200b第二条 乙。"),
        ("第一条 甲。", "\u200b第三条 丙。", "第四条 丁。"),
        ("第一条 甲。", "\u200b第二条 乙。", "\u200b第二条 又乙。", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条 乙。", "第四条 丁。"),
        ("第一条 甲。", "\u200b第二条第一款规定办理。", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条之二 补充。", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条乙。", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条 ", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条 \n乙。", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条 乙。", "第二条之一 补充。", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条 乙。", "\u200b第二条之一 补充。", "第三条 丙。"),
        ("第一条 甲。", "\u200b第二条 乙。", "第二章 屏障", "第三条 丙。"),
        ("第一条 甲。", "错配）", "\u200b第二条 乙。", "第三条 丙。"),
    ],
)
def test_v4_applies_anchor_body_and_barrier_gates_to_zero_width_prefix(texts):
    assert _parse("corpus-docx-v4", *texts).prefixed_article_spans == ()


@pytest.mark.parametrize("opener, closer", [("“", "”"), ("（", "）"), ('"', '"')])
def test_v4_rejects_zero_width_candidate_inside_multiline_boundary(opener, closer):
    parsed = _parse(
        "corpus-docx-v4", "第一条 甲。", f"引文{opener}",
        "\u200b第二条 乙。", f"引文结束{closer}", "第三条 丙。",
    )
    assert parsed.prefixed_article_spans == ()


def test_v4_zero_width_recovery_excludes_leading_title_article_references():
    parsed = _parse(
        "corpus-docx-v4", "某机关关于《某法》", "第一百条的解释",
        "（2020年1月2日通过）", "第一条 甲。", "\u200b第二条 乙。", "第三条 丙。",
    )
    assert [article.provision_no for article in parsed.articles] == ["第一条", "第二条", "第三条"]
    assert len(parsed.leading_title_spans) == 2
    assert len(parsed.prefixed_article_spans) == 1


@pytest.mark.parametrize("side", ["left", "right"])
@pytest.mark.parametrize(
    "suffix", ["第一款规定的事项。", "第（一）项规定的事项。", "无分隔正文。", ""],
)
def test_zero_width_candidate_requires_body_separated_ordinary_anchors(side, suffix):
    left = "第三十四条" + (suffix if side == "left" else " 甲。")
    right = "第三十六条" + (suffix if side == "right" else " 丙。")
    candidate = "\u200b第三十五条 乙。"
    document = _document(left, candidate, right)

    parsed = LegalStructureParser(profile="corpus-docx-v4").parse(document)

    assert parsed.prefixed_article_spans == ()
    assert tuple(p.text for p in document.paragraphs) == (left, candidate, right)
    if side == "right":
        assert candidate in parsed.articles[0].text


@pytest.mark.parametrize("prefix", ["\ue004 ", "（） "])
def test_legacy_prefix_anchor_contract_is_unchanged_by_zero_width_anchor_gate(prefix):
    parsed = _parse(
        "corpus-docx-v4", "第三十四条第一款规定的事项。",
        prefix + "第三十五条 乙。", "第三十六条 丙。",
    )
    assert len(parsed.prefixed_article_spans) == 1
    assert parsed.prefixed_article_spans[0].prefix == prefix
