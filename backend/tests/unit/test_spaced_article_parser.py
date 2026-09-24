from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

import pytest

from lawyer_agent.application.legal_chunk_structure import derive_hierarchical_chunks
from lawyer_agent.application.legal_corpus_import import LegalCorpusImportService
from lawyer_agent.application.legal_index_chunks import select_index_leaves
from lawyer_agent.cli.corpus_publish import _InputError, _prepare_import
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser
from tests.unit.test_corpus_publish_v4_profile import _args
from tests.unit.test_v4_exact_provision_text import _Repo


def _parse(*texts: str, profile: str | None = "corpus-docx-v4"):
    document = ParsedDocument(
        tuple(ParsedParagraph(text, ordinal=i + 10) for i, text in enumerate(texts)),
        "synthetic",
    )
    return LegalStructureParser(profile=profile).parse(document)


@pytest.mark.parametrize("gap", [" ", "\t", "\u3000", " \t\u3000"])
@pytest.mark.parametrize("left, numeral, right, identity", [
    ("第三十四条", "三十{gap}五", "第三十六条", "第三十五条"),
    ("第34条", "3{gap}5", "第36条", "35"),
    ("第３４条", "３{gap}５", "第３６条", "３５"),
])
def test_recovers_internal_horizontal_space_with_exact_text_and_offsets(
    gap, left, numeral, right, identity,
):
    marker = "第" + numeral.format(gap=gap) + "条"
    texts = (left + " 甲。", marker + " 乙。", "续段。", right + " 丙。")
    parsed = _parse(*texts)
    assert len(parsed.articles) == 3
    article = parsed.articles[1]
    assert article.provision_no == identity
    assert article.text == texts[1] + texts[2]
    assert article.paragraphs == texts[1:3]
    assert (article.char_start, article.char_end) == (
        len(texts[0]), sum(map(len, texts[:3])),
    )
    span, = parsed.spaced_article_spans
    assert (span.paragraph_index, span.paragraph_ordinal) == (1, 11)
    assert (span.marker_start, span.marker_end) == (0, len(marker))
    assert span.original_marker == marker
    assert span.normalized_marker == marker.replace(gap, "")


def test_recovers_consecutive_candidates_after_closed_boundaries():
    parsed = _parse(
        "第三十四条 “闭合”（说明）。", "第三十 五条 乙。",
        "第三 十 六条 丙。", "第三十七条 丁。",
    )
    assert [a.provision_no for a in parsed.articles] == [
        "第三十四条", "第三十五条", "第三十六条", "第三十七条",
    ]


@pytest.mark.parametrize("profile", [None, "corpus-docx-v3", "corpus-docx-v4-extra"])
def test_other_profiles_remain_unchanged(profile):
    parsed = _parse("第三十四条 甲。", "第三十\t五条 乙。", "第三十六条 丙。", profile=profile)
    assert len(parsed.articles) == 2
    assert parsed.spaced_article_spans == ()


@pytest.mark.parametrize("candidate", [
    "第三十\n五条 乙。", "第三十\r五条 乙。", "第三十\v五条 乙。",
    "第三十\f五条 乙。", "第三十\u2028五条 乙。", "第三十\u2029五条 乙。",
    "第三十x 五条 乙。", "第三十 五条乙。", "第三十 五条第一款规定。",
    "第三十 五条之一 乙。", "第三十 五条 \n乙。", "第三十 五条 \t",
])
def test_rejects_non_horizontal_or_nonordinary_or_empty_candidates(candidate):
    parsed = _parse("第三十四条 甲。", candidate, "第三十六条 丙。")
    assert parsed.spaced_article_spans == ()


@pytest.mark.parametrize("texts", [
    ("第三十 五条 乙。", "第三十六条 丙。"),
    ("第三十四条 甲。", "第三十 五条 乙。"),
    ("第三十三条 甲。", "第三十 五条 乙。", "第三十六条 丙。"),
    ("第三十四条 甲。", "第三十 五条 乙。", "第三十七条 丙。"),
    ("第三十四条 甲。", "第三十 五条 乙。", "第三十 五条 又乙。", "第三十六条 丙。"),
])
def test_rejects_missing_anchors_gaps_and_duplicates(texts):
    assert _parse(*texts).spaced_article_spans == ()


@pytest.mark.parametrize("barrier", [
    "第二章 章名", "第二节 节名", "第二编 编名", "序言",
    "第三十五条之一 补充。", "（） 第三十五条之一 补充。",
    "第三十 五条之一 补充。",
])
def test_rejects_structural_barriers(barrier):
    parsed = _parse("第三十四条 甲。", "第三十 五条 乙。", barrier, "第三十六条 丙。")
    assert parsed.spaced_article_spans == ()


@pytest.mark.parametrize("opening, closing", [
    ("“", "”"), ("（", "）"), ('"', '"'), ("[", "]"), ("（", "]"), ("“", ""),
])
def test_rejects_quoted_bracketed_and_uncertain_boundaries(opening, closing):
    parsed = _parse("第三十四条 甲。", opening, "第三十 五条 乙。", closing, "第三十六条 丙。")
    assert parsed.spaced_article_spans == ()


def test_rejects_prior_mismatched_closer_and_unclear_right_anchor():
    assert _parse(
        "第三十四条 甲。）", "第三十 五条 乙。", "第三十六条 丙。",
    ).spaced_article_spans == ()
    assert _parse(
        "第三十四条 甲。", "第三十 五条 乙。“", "第三十六条 丙。”",
    ).spaced_article_spans == ()


@pytest.mark.parametrize("left,right", [
    ("第三十四条第一款规定的事项。", "第三十六条 丙。"),
    ("第三十四条 甲。", "第三十六条第一款规定的事项。"),
])
def test_subarticle_reference_cannot_anchor_recovery(left, right):
    assert _parse(left, "第三十 五条 乙。", right).spaced_article_spans == ()


@pytest.mark.parametrize("separator", ["\u0085", "\x1c", "\x1d", "\x1e", "\x1f"])
def test_control_separators_are_not_horizontal(separator):
    assert _parse(
        "第三十四条 甲。", f"第三十{separator}五条 乙。", "第三十六条 丙。",
    ).spaced_article_spans == ()


def test_cli_rejects_mixed_recovery_duplicate(tmp_path: Path):
    path = tmp_path / "duplicate.docx"
    texts = (
        "第三十四条 甲。", "第三十 五条 乙。", "（） 第三十五条 又乙。", "第三十六条 丙。",
    )
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body>' + ''.join(f'<w:p><w:r><w:t>{t}</w:t></w:r></w:p>' for t in texts)
        + '</w:body></w:document>'
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    with pytest.raises(_InputError, match="duplicate article numbers"):
        _prepare_import(_args(path, "corpus-docx-v4"))


async def test_cli_word_tab_to_import_replay_and_hierarchical_coverage(tmp_path: Path):
    path = tmp_path / "synthetic.docx"
    body = "正文。" * 120
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>第三十四条 甲。</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>第三十</w:t><w:tab/><w:t>五条 ' + body + '</w:t></w:r></w:p>'
        '<w:p><w:r><w:t>第三十六条 丙。</w:t></w:r></w:p></w:body></w:document>'
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    original = path.read_bytes()
    command, articles = _prepare_import(_args(path, "corpus-docx-v4"))
    assert [a.provision_no for a in articles] == ["第三十四条", "第三十五条", "第三十六条"]
    expected = "第三十\t五条 " + body
    assert articles[1].text == expected
    repo = _Repo()
    imported = await LegalCorpusImportService(repo).import_version(command)
    replay = await LegalCorpusImportService(repo).import_version(command)
    assert replay.replayed and replay.version_id == imported.version_id
    assert repo.provisions[1].full_text == expected
    chunks = derive_hierarchical_chunks(
        version_id=imported.version_id, provisions=tuple(repo.provisions), articles=articles,
        parser_version="corpus-docx-v4/hierarchical-v2",
        max_leaf_chars=120, window_chars=80, overlap_chars=20,
    )
    parent = next(c for c in chunks if c.content == expected)
    covered = set()
    for leaf in select_index_leaves(chunks):
        if leaf.provision_id != repo.provisions[1].id:
            continue
        start, end = leaf.parent_relative_char_start, leaf.parent_relative_char_end
        assert start is not None and end is not None
        assert leaf.content == parent.content[start:end]
        covered.update(range(start, end))
    assert covered == set(range(len(expected)))
    assert path.read_bytes() == original
