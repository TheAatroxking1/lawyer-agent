from __future__ import annotations

from datetime import date

import pytest

from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportError,
    LegalImportCommand,
)
from lawyer_agent.application.legal_corpus_import_mapping import (
    LegalImportMetadata,
    map_parsed_articles,
)
from lawyer_agent.domain.legal_corpus import LegalCategory, LegalVersionStatus, ProvisionLevel
from lawyer_agent.infrastructure.documents.loader import ParsedDocument, ParsedParagraph
from lawyer_agent.infrastructure.documents.parsers import (
    LegalStructureParser,
    ParsedArticle,
)

TITLE = "中华人民共和国民法典"
AUTHORITY = "全国人民代表大会"
JURISDICTION = "national"


def _metadata(**overrides: object) -> LegalImportMetadata:
    values: dict[str, object] = {
        "title": TITLE,
        "issuing_authority": AUTHORITY,
        "jurisdiction": JURISDICTION,
        "region_code": None,
        "version_label": "2020-05-28 公布版",
        "status": LegalVersionStatus.CURRENT,
        "published_on": date(2020, 5, 28),
        "effective_on": date(2021, 1, 1),
        "repealed_on": None,
        "law_number": "主席令第四十五号",
        "source_ref": "object://corpus/civil-code.docx",
        "dataset_version": "dataset_v1",
        "parser_version": "docx-v1",
        "category": LegalCategory.LAW,
    }
    values.update(overrides)
    return LegalImportMetadata(**values)


def _article(
    provision_no: str = "第一条",
    path: tuple[str, ...] = ("第一编 总则", "第一章 一般规定"),
    text: str = "第一条 为了保护民事主体的合法权益，制定本法。",
    char_start: int = 0,
) -> ParsedArticle:
    return ParsedArticle(
        provision_no=provision_no,
        structure_path=path,
        text=text,
        char_start=char_start,
        char_end=char_start + len(text),
    )


def test_map_parsed_articles_builds_import_command() -> None:
    articles = (
        _article(
            provision_no="第一条",
            text="第一条 为了保护民事主体的合法权益，制定本法。",
        ),
        _article(
            provision_no="第二条",
            path=("第一编 总则",),
            text="第二条 民法调整平等主体的自然人、法人和非法人组织之间的关系。",
        ),
    )
    command = map_parsed_articles(_metadata(), articles)
    assert isinstance(command, LegalImportCommand)
    assert command.title == TITLE
    assert command.issuing_authority == AUTHORITY
    assert command.jurisdiction == JURISDICTION
    assert command.region_code is None
    assert command.version_label == "2020-05-28 公布版"
    assert command.status is LegalVersionStatus.CURRENT
    assert command.published_on == date(2020, 5, 28)
    assert command.effective_on == date(2021, 1, 1)
    assert command.law_number == "主席令第四十五号"
    assert command.source_ref == "object://corpus/civil-code.docx"
    assert command.dataset_version == "dataset_v1"
    assert command.parser_version == "docx-v1"
    assert command.category is LegalCategory.LAW
    assert len(command.provisions) == 2

    first, second = command.provisions
    assert first.provision_no == "第一条"
    assert first.level is ProvisionLevel.ARTICLE
    assert first.structure_path == ("第一编 总则", "第一章 一般规定")
    assert first.title is None
    assert first.full_text == "第一条 为了保护民事主体的合法权益，制定本法。"
    assert second.provision_no == "第二条"
    assert second.structure_path == ("第一编 总则",)
    assert second.full_text == (
        "第二条 民法调整平等主体的自然人、法人和非法人组织之间的关系。"
    )


def test_map_parsed_articles_preserves_input_order() -> None:
    articles = (
        _article(provision_no="第十条", text="第十条 甲。", path=()),
        _article(provision_no="第三条", text="第三条 乙。", path=()),
        _article(provision_no="第一条", text="第一条 丙。", path=()),
    )
    command = map_parsed_articles(_metadata(), articles)
    assert [draft.provision_no for draft in command.provisions] == [
        "第十条",
        "第三条",
        "第一条",
    ]
    assert all(draft.title is None for draft in command.provisions)


def test_map_parsed_articles_rejects_empty_article_list() -> None:
    with pytest.raises(LegalCorpusImportError, match="at least one article"):
        map_parsed_articles(_metadata(), ())


def test_map_parsed_articles_rejects_article_missing_fields() -> None:
    class _Broken:
        provision_no = "第一条"

    with pytest.raises(LegalCorpusImportError, match="structure_path"):
        map_parsed_articles(_metadata(), (_Broken(),))


def test_map_parsed_articles_rejects_blank_full_text() -> None:
    with pytest.raises(LegalCorpusImportError, match="full text"):
        map_parsed_articles(_metadata(), (_article(text="   "),))


def test_map_parsed_articles_rejects_blank_provision_number() -> None:
    with pytest.raises(LegalCorpusImportError, match="provision number"):
        map_parsed_articles(_metadata(), (_article(provision_no="  "),))


def test_map_parsed_articles_rejects_duplicate_provision_numbers() -> None:
    with pytest.raises(LegalCorpusImportError, match="duplicate"):
        map_parsed_articles(
            _metadata(),
            (
                _article(provision_no="第一条"),
                _article(provision_no="第一条", text="第一条 重复条文。"),
            ),
        )


def test_map_parsed_articles_rejects_untyped_metadata() -> None:
    with pytest.raises(LegalCorpusImportError, match="metadata"):
        map_parsed_articles({"title": TITLE}, (_article(),))  # type: ignore[arg-type]


def test_map_parsed_articles_composes_with_structure_parser() -> None:
    document = ParsedDocument(
        paragraphs=tuple(
            ParsedParagraph(text=line, ordinal=index)
            for index, line in enumerate(
                [
                    "序言",
                    "本序言内容。",
                    "第一编 总则",
                    "第一章 一般规定",
                    "第一条 为了保护民事主体的合法权益，制定本法。",
                    "本款为该条的延续。",
                    "第二条 民法调整平等主体之间的民事关系。",
                ]
            )
        ),
        source_ref="object://corpus/sample",
    )
    instrument = LegalStructureParser().parse(document)
    command = map_parsed_articles(_metadata(), instrument.articles)
    assert [draft.provision_no for draft in command.provisions] == [
        "第一条",
        "第二条",
    ]
    first, second = command.provisions
    assert first.structure_path == ("第一编 总则", "第一章 一般规定")
    assert first.full_text == (
        "第一条 为了保护民事主体的合法权益，制定本法。本款为该条的延续。"
    )
    assert second.structure_path == ("第一编 总则", "第一章 一般规定")
