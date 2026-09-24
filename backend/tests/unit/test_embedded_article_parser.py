from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from lawyer_agent.application.legal_chunk_structure import derive_hierarchical_chunks
from lawyer_agent.application.legal_corpus_import import LegalProvisionDraft, _derive_provisions
from lawyer_agent.cli.corpus_publish import _prepare_import, build_parser
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import ProvisionLevel
from lawyer_agent.infrastructure.documents.embedded_article import (
    detect_embedded_article_spans,
)
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser


def _document(*texts: str) -> ParsedDocument:
    return ParsedDocument(
        tuple(ParsedParagraph(text, ordinal=index + 40) for index, text in enumerate(texts)),
        "synthetic",
    )


def _parse(profile: str | None, *texts: str):
    return LegalStructureParser(profile=profile).parse(_document(*texts))


def test_v4_splits_one_embedded_heading_at_original_marker_and_preserves_every_character():
    texts = ("第一条 甲。  \ue004\t第二条  乙。", "第三条 丙。")
    parsed = _parse("corpus-docx-v4", *texts)

    assert [article.provision_no for article in parsed.articles] == [
        "第一条", "第二条", "第三条",
    ]
    assert parsed.articles[0].text == "第一条 甲。  \ue004\t"
    assert parsed.articles[1].text == "第二条  乙。"
    assert "".join(article.text for article in parsed.articles) == "".join(texts)
    assert parsed.articles[0].paragraphs == ("第一条 甲。  \ue004\t",)
    span = parsed.embedded_article_spans[0]
    assert (span.paragraph_index, span.paragraph_ordinal) == (0, 40)
    assert span.marker_start == texts[0].index("第二条")
    assert span.split_start == span.marker_start
    assert span.left_separator == "  \ue004\t"


@pytest.mark.parametrize(
    "separator",
    ["", " ", "\t", "\ue004", "\ue004  ", " \ue004", " \ue004\t"],
)
def test_v4_accepts_the_approved_sentence_separator_and_preserves_it_on_the_left(
    separator: str,
):
    texts = (f"第一条 正文。{separator}第二条 正文。", "第三条 正文。")
    parsed = _parse("corpus-docx-v4", *texts)

    assert [article.provision_no for article in parsed.articles] == [
        "第一条", "第二条", "第三条",
    ]
    assert parsed.articles[0].text == f"第一条 正文。{separator}"
    assert parsed.articles[1].text == "第二条 正文。"
    assert "".join(article.text for article in parsed.articles) == "".join(texts)
    span = parsed.embedded_article_spans[0]
    assert span.marker_start == texts[0].index("第二条")
    assert span.split_start == span.marker_start
    assert span.left_separator == separator


def test_v4_accepts_multiple_candidates_and_a_group_continued_across_paragraphs():
    parsed = _parse(
        "corpus-docx-v4",
        "第八条 甲。 第九条 乙。  第十条 丙。",
        "续段。",
        "第十一条 丁。",
    )
    assert [article.provision_no for article in parsed.articles] == [
        "第八条", "第九条", "第十条", "第十一条",
    ]
    assert [span.marker_start for span in parsed.embedded_article_spans] == [7, 15]
    assert parsed.articles[2].paragraphs == ("第十条 丙。", "续段。")


@pytest.mark.parametrize(
    "middle",
    [
        "第一条 甲。 第二条乙。",  # compact heading/body
        "第一条 甲。 第二条之一 补充。",
        "第一条 甲。 第二条第一款规定办理。",
        "第一条 甲。 第二条的规定继续适用。",
        "第一条 甲。 \ue004\ue004 第二条 乙。",
        "第一条 甲；第二条 乙。",
    ],
)
def test_v4_rejects_non_independent_or_ambiguous_internal_markers(middle: str):
    parsed = _parse("corpus-docx-v4", middle, "第三条 丙。")
    assert parsed.embedded_article_spans == ()
    assert [article.provision_no for article in parsed.articles] == ["第一条", "第三条"]


def test_existing_explicit_line_heading_is_not_reported_as_embedded():
    parsed = _parse("corpus-docx-v4", "第一条 甲。\n第二条 乙。", "第三条 丙。")
    assert [article.provision_no for article in parsed.articles] == [
        "第一条", "第二条", "第三条",
    ]
    assert parsed.embedded_article_spans == ()


@pytest.mark.parametrize("line_break", ["\n", "\r\n", "\v", "\u2028", "\u2029"])
@pytest.mark.parametrize("trailing_horizontal", ["", "  "])
def test_v4_bounds_embedded_candidates_to_the_current_explicit_line(
    line_break: str,
    trailing_horizontal: str,
):
    text = f"第一条 甲。第二条 乙。{trailing_horizontal}{line_break}第三条 丙。"
    parsed = _parse("corpus-docx-v4", text)

    assert [article.provision_no for article in parsed.articles] == [
        "第一条", "第二条", "第三条",
    ]
    assert [article.text for article in parsed.articles] == [
        "第一条 甲。", "第二条 乙。", "第三条 丙。",
    ]
    assert len(parsed.embedded_article_spans) == 1
    assert parsed.embedded_article_spans[0].marker_start == text.index("第二条")


def test_v4_rejects_an_embedded_marker_whose_only_body_is_on_the_next_line():
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。第二条  \n正文。",
        "第三条 丙。",
    )

    assert parsed.embedded_article_spans == ()


def test_embedded_candidate_scan_copies_only_linear_source_text():
    class CopyRecorder:
        copied = 0

    class RecordingStr(str):
        def __new__(cls, value: str, recorder: CopyRecorder):
            instance = super().__new__(cls, value)
            instance.recorder = recorder
            return instance

        def __getitem__(self, key):
            result = super().__getitem__(key)
            if isinstance(key, slice):
                self.recorder.copied += len(result)
                return RecordingStr(result, self.recorder)
            return result

    recorder = CopyRecorder()
    text = RecordingStr("第一条 正文。" + "甲。" * 600, recorder)
    document = ParsedDocument((ParsedParagraph(text, ordinal=40),), "synthetic")

    assert detect_embedded_article_spans(document) == ()
    assert recorder.copied <= len(text) * 12


@pytest.mark.parametrize(
    "texts",
    [
        ("第一条 甲。 第三条 丙。", "第四条 丁。"),
        ("第一条 甲。 第二条 乙。 第四条 丁。", "第五条 戊。"),
        ("第二条 甲。 第二条 乙。", "第三条 丙。"),
        ("第二条 甲。 第一条 乙。", "第三条 丙。"),
        ("第一条 甲。 第二条 乙。", "第四条 丁。"),
    ],
)
def test_v4_requires_an_exact_ordered_candidate_run_between_anchors(texts: tuple[str, ...]):
    assert _parse("corpus-docx-v4", *texts).embedded_article_spans == ()


@pytest.mark.parametrize(
    "barrier",
    ["第二章 范围", "序言", "第二条之一 补充。", "\ue004 第九条 未解前缀。"],
)
def test_structural_and_unresolved_prefix_events_block_cross_event_closure(barrier: str):
    parsed = _parse(
        "corpus-docx-v4", "第一条 甲。 第二条 乙。", barrier, "第三条 丙。"
    )
    assert parsed.embedded_article_spans == ()


def test_unresolved_prefixed_heading_cannot_serve_as_the_left_anchor():
    parsed = _parse(
        "corpus-docx-v4",
        "\ue004 第一条 未经证明。 第二条 乙。",
        "第三条 丙。",
    )
    assert parsed.prefixed_article_spans == ()
    assert parsed.embedded_article_spans == ()


@pytest.mark.parametrize("right", ["第三条丙。", "第三条的规定继续适用。"])
def test_compact_or_reference_like_heading_cannot_close_a_candidate(right: str):
    parsed = _parse("corpus-docx-v4", "第一条 甲。 第二条 乙。", right)
    assert parsed.embedded_article_spans == ()


def test_candidate_and_anchor_must_be_outside_cross_paragraph_quotes_and_brackets():
    candidate_quoted = _parse(
        "corpus-docx-v4", "第一条 甲。引文“", "内容。 第二条 乙。", "结束”", "第三条 丙。"
    )
    anchor_quoted = _parse(
        "corpus-docx-v4", "第一条 甲。 第二条 乙。", "引文（", "第三条 丙。", "结束）"
    )
    mismatched = _parse(
        "corpus-docx-v4", "第一条 甲。 第二条 乙。", "错配]", "第三条 丙。"
    )
    assert candidate_quoted.embedded_article_spans == ()
    assert anchor_quoted.embedded_article_spans == ()
    assert mismatched.embedded_article_spans == ()


def test_large_article_numbers_are_compared_only_to_the_bounded_candidate_count(
):
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 甲。 第二条 乙。",
        "第999999999999999999999999条 丙。",
    )
    assert parsed.embedded_article_spans == ()


def test_default_v3_and_near_profile_keep_the_original_unsplit_view():
    text = "第一条 甲。 第二条 乙。"
    for profile in (None, "corpus-docx-v3", "corpus-docx-v4-extra"):
        parsed = _parse(profile, text, "第三条 丙。")
        assert [article.provision_no for article in parsed.articles] == ["第一条", "第三条"]
        assert parsed.articles[0].text == text
        assert parsed.embedded_article_spans == ()


def _write_docx(path: Path, lines: tuple[str, ...]) -> None:
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
        + "</w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def test_prepare_enables_embedded_recovery_only_for_exact_v4(tmp_path: Path):
    path = tmp_path / "embedded.docx"
    _write_docx(path, ("第一条 甲。 第二条 乙。", "第三条 丙。"))

    def args(version: str):
        return build_parser().parse_args([
            "--docx", str(path), "--instrument-title", "合成法",
            "--issuing-authority", "合成机关", "--parser-version", version,
        ])

    _, v4 = _prepare_import(args("corpus-docx-v4"))
    _, near = _prepare_import(args("corpus-docx-v4-extra"))
    assert [article.provision_no for article in v4] == ["第一条", "第二条", "第三条"]
    assert [article.provision_no for article in near] == ["第一条", "第三条"]


def test_v4_import_and_exact_chunks_preserve_split_whitespace_and_cover_parent():
    parsed = _parse(
        "corpus-docx-v4",
        "第一条 " + "甲。" * 180 + "  \ue004\t第二条  " + "乙。" * 180,
        "第三条 丙。",
    )
    version_id = new_uuid7()
    drafts = tuple(
        LegalProvisionDraft(
            provision_no=article.provision_no,
            level=ProvisionLevel.ARTICLE,
            structure_path=article.structure_path,
            title=None,
            full_text=article.text,
        )
        for article in parsed.articles
    )
    provisions = _derive_provisions(
        version_id, drafts, parser_version="corpus-docx-v4"
    )
    chunks = derive_hierarchical_chunks(
        version_id=version_id,
        provisions=provisions,
        articles=parsed.articles,
        parser_version="corpus-docx-v4/hierarchical-v2",
        max_leaf_chars=120,
        window_chars=100,
        overlap_chars=20,
    )
    for provision, article in zip(provisions, parsed.articles, strict=True):
        assert provision.full_text == "".join(article.paragraphs)
        parent = next(
            chunk
            for chunk in chunks
            if chunk.provision_id == provision.id and chunk.parent_chunk_id is None
        )
        assert parent.content == provision.full_text
