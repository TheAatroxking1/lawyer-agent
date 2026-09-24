import json
from zipfile import ZipFile

import pytest

from lawyer_agent.cli import corpus_publish as cli
from lawyer_agent.domain.legal_corpus import ProvisionLevel


def prepare(tmp_path, paragraphs, mode="non_article_document"):
    source = tmp_path / "synthetic.docx"
    with ZipFile(source, "w") as archive:
        archive.writestr("word/document.xml", (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body>' + ''.join(f'<w:p><w:r><w:t>{p}</w:t></w:r></w:p>' for p in paragraphs)
            + '</w:body></w:document>'
        ))
    args = cli.build_parser().parse_args([
        "--docx", str(source), "--instrument-title", "合成批复", "--issuing-authority", "合成机关",
        "--jurisdiction", "CN", "--content-mode", mode, "--preflight",
    ])
    return cli._prepare(args)


def test_body_is_one_truthful_parent_and_proof_bound(tmp_path):
    prepared = prepare(tmp_path, ["合成批复", "收悉。", "批复正文。", "此复"])
    draft, = prepared.command.provisions
    assert draft.level is ProvisionLevel.PARAGRAPH and draft.provision_no == "正文"
    assert draft.full_text == "合成批复收悉。批复正文。此复"
    assert "non_article_document" in cli._source_proof(prepared).quality_flags
    report = cli._preflight_report(prepared)
    assert report["article_count"] == 0 and report["provision_count"] == 1
    assert report["content_mode"] == "non_article_document"


@pytest.mark.parametrize("paragraphs,mode", [
    (["第一条 正文。"], "non_article_document"), (["没有条号"], "articles"),
    ([" "], "non_article_document"),
])
def test_body_mode_cannot_bypass_articles_or_empty_source(tmp_path, paragraphs, mode):
    with pytest.raises(cli._InputError):
        prepare(tmp_path, paragraphs, mode)


def test_long_body_has_bounded_leaves_and_full_parent(tmp_path):
    from lawyer_agent.application.legal_chunk_structure import derive_hierarchical_chunks
    from lawyer_agent.application.legal_corpus_import import _derive_provisions
    from lawyer_agent.application.legal_index_chunks import select_index_leaves
    from lawyer_agent.domain.common import new_uuid7

    prepared = prepare(tmp_path, ["合成批复", "正文" * 1000])
    version_id = new_uuid7()
    provisions = _derive_provisions(version_id, prepared.command.provisions)
    chunks = derive_hierarchical_chunks(
        version_id=version_id, provisions=provisions, articles=prepared.articles,
        parser_version="test/hierarchical-v1",
    )
    assert chunks[0].content == provisions[0].full_text
    assert all(len(leaf.content) <= 600 for leaf in select_index_leaves(chunks))


def test_ordinary_structure_digest_unchanged(tmp_path):
    from hashlib import sha256

    prepared = prepare(tmp_path, ["第一条 原条文。"], "articles")
    article, = prepared.articles
    original = [{"provision_no": article.provision_no, "structure_path": article.structure_path,
                 "text": article.text, "paragraphs": article.paragraphs}]
    digest = sha256(json.dumps(original, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode()).hexdigest()
    assert prepared.structure_sha256 == digest


def test_parser_failure_is_not_reinterpreted_as_body(tmp_path, monkeypatch):
    from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser

    def reject(*args):
        raise ValueError("ambiguous supplement article heading")

    monkeypatch.setattr(LegalStructureParser, "parse", reject)
    with pytest.raises(cli._InputError, match="structure parsing failed"):
        prepare(tmp_path, ["合成正文"])
