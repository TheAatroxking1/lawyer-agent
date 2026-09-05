from __future__ import annotations

import pytest

from lawyer_agent.application.legal_source_diff import (
    LegalSourceDiffError,
    ModifiedSourceEntry,
    SourceDiffEntry,
    diff_source_articles,
)
from lawyer_agent.infrastructure.documents.parsers import ParsedArticle


def _article(
    provision_no: str = "第一条",
    path: tuple[str, ...] = (),
    text: str = "第一条 内容。",
) -> ParsedArticle:
    return ParsedArticle(
        provision_no=provision_no,
        structure_path=path,
        text=text,
        char_start=0,
        char_end=len(text),
    )


def _entries(diff: object) -> dict[str, tuple[SourceDiffEntry, ...]]:
    import dataclasses

    return {
        field.name: tuple(getattr(diff, field.name))
        for field in dataclasses.fields(diff)
        if field.name in {"added", "removed", "unchanged"}
    }


def test_diff_source_articles_groups_all_four_categories() -> None:
    old_articles = (
        _article(provision_no="第一条", text="第一条 保留条文。"),
        _article(provision_no="第二条", text="第二条 被删除条文。"),
        _article(provision_no="第三条", text="第三条 修改前条文。"),
    )
    new_articles = (
        _article(provision_no="第一条", text="第一条 保留条文。"),
        _article(provision_no="第三条", text="第三条 修改后条文。"),
        _article(provision_no="第四条", text="第四条 新增条文。"),
    )
    diff = diff_source_articles(old_articles, new_articles)
    assert diff.changed is True
    groups = _entries(diff)
    assert [entry.provision_no for entry in groups["removed"]] == ["第二条"]
    assert [entry.provision_no for entry in groups["added"]] == ["第四条"]
    assert [entry.provision_no for entry in groups["unchanged"]] == ["第一条"]
    assert [item.provision_no for item in diff.modified] == ["第三条"]
    (modified,) = diff.modified
    assert isinstance(modified, ModifiedSourceEntry)
    assert modified.previous.full_text == "第三条 修改前条文。"
    assert modified.current.full_text == "第三条 修改后条文。"
    assert modified.current.structure_path == ()


def test_diff_source_articles_keeps_source_order_in_groups() -> None:
    old_articles = (
        _article(provision_no="第十条", text="第十条 甲。"),
        _article(provision_no="第三条", text="第三条 乙。"),
        _article(provision_no="第一条", text="第一条 丙。"),
    )
    new_articles = (
        _article(provision_no="第十条", text="第十条 甲改。"),
        _article(provision_no="第五条", text="第五条 新。"),
    )
    diff = diff_source_articles(old_articles, new_articles)
    assert [entry.provision_no for entry in diff.removed] == [
        "第三条",
        "第一条",
    ]
    assert [entry.provision_no for entry in diff.added] == ["第五条"]
    assert [item.provision_no for item in diff.modified] == ["第十条"]


def test_diff_source_articles_identical_documents_changed_false() -> None:
    old_articles = (
        _article(provision_no="第一条", text="第一条 内容。"),
        _article(provision_no="第二条", text="第二条 内容。"),
    )
    diff = diff_source_articles(old_articles, old_articles)
    assert diff.changed is False
    assert diff.added == ()
    assert diff.removed == ()
    assert diff.modified == ()
    assert [entry.provision_no for entry in diff.unchanged] == [
        "第一条",
        "第二条",
    ]


def test_diff_source_articles_empty_inputs_are_safe() -> None:
    empty = diff_source_articles((), ())
    assert empty.changed is False
    assert empty.added == () and empty.removed == () and empty.modified == ()
    assert empty.unchanged == ()
    only_new = diff_source_articles((), (_article(provision_no="第一条"),))
    assert only_new.changed is True
    assert [entry.provision_no for entry in only_new.added] == ["第一条"]
    only_old = diff_source_articles((_article(provision_no="第一条"),), ())
    assert only_old.changed is True
    assert [entry.provision_no for entry in only_old.removed] == ["第一条"]


def test_diff_source_articles_rejects_duplicate_numbers() -> None:
    with pytest.raises(LegalSourceDiffError, match="duplicate"):
        diff_source_articles(
            (_article(provision_no="第一条"),),
            (
                _article(provision_no="第一条", text="第一条 甲。"),
                _article(provision_no="第一条", text="第一条 乙。"),
            ),
        )
    with pytest.raises(LegalSourceDiffError, match="duplicate"):
        diff_source_articles(
            (
                _article(provision_no="第一条", text="第一条 甲。"),
                _article(provision_no="第一条", text="第一条 乙。"),
            ),
            (_article(provision_no="第一条"),),
        )


def test_diff_source_articles_rejects_blank_or_malformed_views() -> None:
    with pytest.raises(LegalSourceDiffError, match="provision number"):
        diff_source_articles(
            (_article(provision_no="   "),), (_article(provision_no="第一条"),)
        )
    with pytest.raises(LegalSourceDiffError, match="text"):
        diff_source_articles(
            (_article(text="   "),), (_article(provision_no="第一条"),)
        )


def test_diff_source_articles_ignores_structure_path_for_matching() -> None:
    old_articles = (_article(provision_no="第一条", path=("第一章",), text="第一条 甲。"),)
    new_articles = (
        _article(provision_no="第一条", path=("第二章",), text="第一条 甲。"),
    )
    diff = diff_source_articles(old_articles, new_articles)
    # Same number and same full text => unchanged even if the path moved.
    assert diff.changed is False
    assert [entry.provision_no for entry in diff.unchanged] == ["第一条"]
