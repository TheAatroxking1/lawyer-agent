from __future__ import annotations

from pathlib import Path
from zipfile import ZipFile

from lawyer_agent.cli.corpus_publish import _prepare_import, build_parser


def _write_docx(path: Path) -> None:
    lines = [
        "某机关关于《某条例》",
        "第七十七条适用问题的解释",
        "（2020年1月2日通过）",
        "第一条 正文。",
    ]
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
        + "</w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)


def _args(path: Path, parser_version: str):
    return build_parser().parse_args([
        "--docx", str(path),
        "--instrument-title", "合成解释",
        "--issuing-authority", "合成机关",
        "--parser-version", parser_version,
    ])


def test_prepare_selects_leading_title_profile_only_for_exact_v4_label(tmp_path: Path):
    path = tmp_path / "synthetic.docx"
    _write_docx(path)
    _, v4_articles = _prepare_import(_args(path, "corpus-docx-v4"))
    _, other_articles = _prepare_import(_args(path, "corpus-docx-v4-extra"))
    assert [article.provision_no for article in v4_articles] == ["第一条"]
    assert [article.provision_no for article in other_articles] == ["第七十七条", "第一条"]
