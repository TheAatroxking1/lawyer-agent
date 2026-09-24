from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from lawyer_agent.cli.corpus_publish import _InputError, _prepare_import, build_parser
from lawyer_agent.domain.legal_corpus import LegalCategory, LegalVersionStatus


@pytest.fixture
def legal_docx(tmp_path: Path) -> Path:
    path = tmp_path / "合成示例法.docx"
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>第一条 合成内容。</w:t></w:r></w:p></w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return path


def _args(path: Path, *extra: str):
    return build_parser().parse_args(
        ["--docx", str(path), "--instrument-title", "合成示例法",
         "--issuing-authority", "示例机关", *extra]
    )


def test_unknown_legal_dates_and_status_are_preserved(legal_docx: Path) -> None:
    command, articles = _prepare_import(_args(legal_docx))
    assert command.status is LegalVersionStatus.STATUS_UNKNOWN
    assert command.category is LegalCategory.UNKNOWN
    assert command.published_on is command.effective_on is command.repealed_on is None
    assert command.version_label == "source-sha256:" + sha256(legal_docx.read_bytes()).hexdigest()
    assert command.source_ref == legal_docx.resolve().as_uri()
    assert articles[0].paragraphs == ("第一条 合成内容。",)
    repeated, _ = _prepare_import(_args(legal_docx))
    assert command == repeated


def test_explicit_legal_metadata_is_kept(legal_docx: Path) -> None:
    command, _ = _prepare_import(_args(
        legal_docx, "--status", "historical", "--category", "law",
        "--published-on", "2020-01-02", "--effective-on", "2020-03-01",
        "--repealed-on", "2024-01-01", "--version-label", "核对后的历史版",
        "--source-ref", "source-system:synthetic-42",
    ))
    assert command.status is LegalVersionStatus.HISTORICAL
    assert command.category is LegalCategory.LAW
    assert str(command.published_on) == "2020-01-02"
    assert str(command.effective_on) == "2020-03-01"
    assert str(command.repealed_on) == "2024-01-01"
    assert command.version_label == "核对后的历史版"
    assert command.source_ref == "source-system:synthetic-42"


def test_published_date_label_does_not_supply_effective_date(legal_docx: Path) -> None:
    command, _ = _prepare_import(_args(legal_docx, "--published-on", "2020-01-02"))
    assert command.version_label == "2020-01-02 导入版"
    assert command.effective_on is None
    assert command.status is LegalVersionStatus.STATUS_UNKNOWN


@pytest.mark.parametrize("value", ["20200102", "2020-13-01", "2020-01-02T00:00:00", ""])
def test_bad_dates_are_input_errors(legal_docx: Path, value: str) -> None:
    with pytest.raises(_InputError, match="YYYY-MM-DD"):
        _prepare_import(_args(legal_docx, "--published-on", value))


def test_supplement_articles_prepare_distinct_import_drafts(tmp_path: Path) -> None:
    path = tmp_path / "合成增补条文.docx"
    lines = ["第一条 基础条文。", "第一条之一 增补条文甲。", "第一条之二 增补条文乙。"]
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
        + "</w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    command, articles = _prepare_import(_args(path))
    assert [draft.provision_no for draft in command.provisions] == [
        "第一条", "第一条之一", "第一条之二",
    ]
    assert [draft.full_text for draft in command.provisions] == lines
    assert [article.text for article in articles] == lines
