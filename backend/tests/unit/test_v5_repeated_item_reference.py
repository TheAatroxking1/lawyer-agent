"""Repeated item locators are body references only in the new parser profile."""

import pytest

from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser


def document(reference: str, *, internal: bool = False) -> ParsedDocument:
    texts = ["第一条 适用范围。", "第二条 下列机构应当消毒。"]
    if internal:
        texts[-1] += "\n" + reference
    else:
        texts.append(reference)
    texts.append("第三条 监督管理。")
    return ParsedDocument(
        tuple(ParsedParagraph(t, ordinal=i) for i, t in enumerate(texts)), "synthetic.docx"
    )


@pytest.mark.parametrize(
    "locator",
    [
        "第（八）、第（九）、第（十）、第（十一）项",
        "第(1)、第(2)项",
        "第（１）、第（２）款",
    ],
)
@pytest.mark.parametrize("internal", [False, True])
def test_v5_preserves_complete_repeated_item_reference_in_current_article(locator, internal):
    reference = f"第一条{locator}规定的消毒管理，按照有关规定执行。"
    parsed = LegalStructureParser(profile="corpus-docx-v5").parse(
        document(reference, internal=internal)
    )
    assert [a.provision_no for a in parsed.articles] == ["第一条", "第二条", "第三条"]
    assert reference in parsed.articles[1].text
    assert parsed.articles[1].paragraphs[-1].endswith(reference)


@pytest.mark.parametrize("profile", [None, "corpus-docx-v3", "corpus-docx-v4", "corpus-docx-v50"])
def test_existing_profiles_keep_their_previous_interpretation(profile):
    reference = "第一条第（八）、第（九）、第（十）、第（十一）项规定的处理。"
    parsed = LegalStructureParser(profile=profile).parse(document(reference))
    assert [a.provision_no for a in parsed.articles] == ["第一条", "第二条", "第一条", "第三条"]


@pytest.mark.parametrize(
    "reference",
    [
        "第一条第（八）、第（九）应当核实。",
        "第一条第（八）、第（九)项规定的事项。",
        "第一条第（八）、第（九）、项规定的事项。",
        "第一条以公开、公平为原则。",
        "第一条 普通条文。",
    ],
)
def test_v5_does_not_hide_incomplete_locators_or_true_duplicate_headings(reference):
    parsed = LegalStructureParser(profile="corpus-docx-v5").parse(document(reference))
    assert [a.provision_no for a in parsed.articles].count("第一条") == 2


def test_old_internal_reference_ambiguity_is_not_silently_reinterpreted():
    with pytest.raises(ValueError, match="ambiguous internal article heading"):
        LegalStructureParser(profile="corpus-docx-v4").parse(
            document("第一条第（八）、第（九）项规定的事项。", internal=True)
        )


def test_v5_still_rejects_compact_unreviewed_internal_heading():
    with pytest.raises(ValueError, match="ambiguous internal article heading"):
        LegalStructureParser(profile="corpus-docx-v5").parse(
            document("第三十九条以有关规定为依据。", internal=True)
        )


@pytest.mark.parametrize("marker", ["第二条之二", "第三条"])
@pytest.mark.parametrize("separator", [" ", "\t", "\u3000"])
def test_v5_preserves_article_heading_before_spaced_item_list(marker, separator):
    text = marker + separator + "第（一）、第（二）项申请材料应当留存。"
    source = ParsedDocument(
        tuple(
            ParsedParagraph(t, ordinal=i)
            for i, t in enumerate(["第一条 范围。", "第二条 要求。", text, "第四条 监督。"])
        ),
        "synthetic.docx",
    )
    parsed = LegalStructureParser(profile="corpus-docx-v5").parse(source)
    assert [a.provision_no for a in parsed.articles] == ["第一条", "第二条", marker, "第四条"]
    assert parsed.articles[2].text == text


@pytest.mark.parametrize(
    "suffix",
    [
        "（以下简称材料）应当留存。",
        "(以下简称材料)应当留存。",
        "、第二条第（三）项规定的事项。",
        "第一个条件应当满足。",
    ],
)
@pytest.mark.parametrize("internal", [False, True])
def test_v5_completed_reference_allows_following_sentence_syntax(suffix, internal):
    reference = "第一条第（一）、第（二）项" + suffix
    parsed = LegalStructureParser(profile="corpus-docx-v5").parse(
        document(reference, internal=internal)
    )
    assert [a.provision_no for a in parsed.articles] == ["第一条", "第二条", "第三条"]
    assert reference in parsed.articles[1].text
