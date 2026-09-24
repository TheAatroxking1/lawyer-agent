from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest

from lawyer_agent.application.legal_chunk_structure import (
    LegalChunkStructureError,
    derive_hierarchical_chunks,
)
from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalCorpusImportService,
    LegalProvisionDraft,
    _content_hash,
    provision_text_for_parser,
)
from lawyer_agent.application.legal_corpus_replay import (
    assert_provisions_match,
    persist_or_replay_chunk_graph,
)
from lawyer_agent.application.legal_dataset_quality import ReleaseQualityService
from lawyer_agent.application.legal_index_chunks import select_index_leaves
from lawyer_agent.cli.corpus_publish import _prepare, _source_proof, build_parser
from lawyer_agent.domain.legal_corpus import (
    LegalChunk,
    LegalInstrument,
    LegalVersion,
    Provision,
    ProvisionLevel,
)
from lawyer_agent.domain.legal_dataset_quality import ReleaseConfiguration, ReleaseSelection
from lawyer_agent.domain.legal_source_proof import LegalSourceProof
from lawyer_agent.infrastructure.documents.parsers import ParsedArticle
from tests.unit.test_v4_exact_provision_text import _command, _Repo


def _draft(text: str = "甲") -> LegalProvisionDraft:
    return LegalProvisionDraft("第一条", ProvisionLevel.ARTICLE, ("第一条",), None, text)


def _hash_payload(draft: LegalProvisionDraft) -> bytes:
    def field(name: str, value: str) -> bytes:
        key, data = name.encode("utf-8"), value.encode("utf-8")
        return len(key).to_bytes(4, "big") + key + len(data).to_bytes(8, "big") + data

    return (
        (1).to_bytes(8, "big")
        + b"provision\x00"
        + field("provision_no", draft.provision_no)
        + field("level", "article")
        + (1).to_bytes(8, "big")
        + field("structure_path", "第一条")
        + (5).to_bytes(4, "big")
        + b"title\x00"
        + field("full_text", draft.full_text)
    )


def test_v5_preserves_leading_and_trailing_whitespace() -> None:
    text = " \u3000第一条 正文。\t\n"
    assert provision_text_for_parser(text, "corpus-docx-v5") == text


def test_v5_uses_independent_structural_hash_domain() -> None:
    draft = _draft()
    expected = sha256(b"lawyer-agent:legal-provisions:v5\x00" + _hash_payload(draft)).digest()
    assert _content_hash((draft,), parser_version="corpus-docx-v5") == expected
    assert expected != _content_hash((draft,), parser_version="corpus-docx-v4")
    assert expected != _content_hash(
        (replace(draft, full_text=" 甲"),), parser_version="corpus-docx-v5"
    )
    assert expected != _content_hash((replace(draft, title=""),), parser_version="corpus-docx-v5")


def test_v4_structural_hash_encoding_remains_unchanged() -> None:
    draft = _draft()
    expected = sha256(b"lawyer-agent:legal-provisions:v4\x00" + _hash_payload(draft)).digest()
    assert _content_hash((draft,), parser_version="corpus-docx-v4") == expected


@pytest.mark.parametrize(
    "profile", [None, "corpus-docx-v3", "xcorpus-docx-v5", "corpus-docx-v5-extra"]
)
def test_unrecognized_profiles_keep_legacy_semantics(profile: str | None) -> None:
    text = " \t甲\u3000\n"
    assert provision_text_for_parser(text, profile) == "甲"
    assert _content_hash((_draft(text),), parser_version=profile) == sha256("甲".encode()).digest()


def _prepared(tmp_path: Path, profile: str = "corpus-docx-v5"):
    path = tmp_path / "synthetic-profile.docx"
    paragraphs = (
        "合成机关关于《合成条例》",
        "第七十七条适用问题的解释",
        "（2020年1月2日通过）",
        " \u3000第一条 " + "甲。" * 90 + "\t ",
        " 重复段。" + "乙。" * 90 + " ",
        " 重复段。" + "乙。" * 90 + " ",
        " 第二条 末条正文。\u3000 ",
    )
    xml = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(
            f'<w:p><w:r><w:t xml:space="preserve">{line}</w:t></w:r></w:p>' for line in paragraphs
        )
        + "</w:body></w:document>"
    )
    with ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", xml)
    args = build_parser().parse_args(
        [
            "--docx",
            str(path),
            "--instrument-title",
            "合成解释",
            "--issuing-authority",
            "合成机关",
            "--parser-version",
            profile,
            "--published-on",
            "2020-01-02",
            "--effective-on",
            "2020-02-02",
            "--category",
            "law",
            "--status",
            "current",
        ]
    )
    return _prepare(args)


def test_cli_prepare_selects_v5_exact_profile(tmp_path: Path) -> None:
    prepared = _prepared(tmp_path)
    assert prepared.command.parser_version == "corpus-docx-v5"
    assert _source_proof(prepared).parser_version == "corpus-docx-v5"
    assert [article.provision_no for article in prepared.articles] == ["第一条", "第二条"]
    # The established reader trims paragraph edges before the parser boundary.
    # CLI selection must preserve all text that actually enters the parser.
    body = prepared.source.document.paragraphs[3:]
    assert "".join(p.full_text for p in prepared.command.provisions) == "".join(
        p.text for p in body
    )


def test_v5_long_article_rejects_inexact_paragraph_units() -> None:
    from lawyer_agent.domain.common import new_uuid7
    from lawyer_agent.domain.legal_corpus import content_sha256

    text = " 第一条 " + "甲。" * 90 + " "
    provision = Provision(
        new_uuid7(),
        new_uuid7(),
        "第一条",
        ProvisionLevel.ARTICLE,
        ("第一条",),
        None,
        text,
        content_sha256(text),
        0,
        len(text),
    )
    article = ParsedArticle(
        provision_no="第一条",
        structure_path=("第一条",),
        text=text,
        char_start=0,
        char_end=len(text),
        paragraphs=(text[:-1],),
    )
    with pytest.raises(LegalChunkStructureError, match="exact paragraph units"):
        derive_hierarchical_chunks(
            version_id=provision.version_id,
            provisions=(provision,),
            articles=(article,),
            parser_version="corpus-docx-v5/hierarchical-v2",
            max_leaf_chars=120,
            window_chars=80,
            overlap_chars=20,
        )


class _PipelineRepo(_Repo):
    def __init__(self, proof: LegalSourceProof) -> None:
        super().__init__()
        self.proof = proof
        self.chunks: tuple[LegalChunk, ...] = ()
        self.chunk_writes = 0

    async def version_with_instrument(
        self, version_id: UUID
    ) -> tuple[LegalVersion, LegalInstrument]:
        assert self.instrument is not None
        return next(v for v in self.versions if v.id == version_id), self.instrument

    async def provisions_for_version(self, version_id: UUID) -> tuple[Provision, ...]:
        return tuple(p for p in self.provisions if p.version_id == version_id)

    async def chunks_for_version(self, version_id: UUID) -> tuple[LegalChunk, ...]:
        return tuple(c for c in self.chunks if c.version_id == version_id)

    async def replace_chunks_for_version(
        self, version_id: UUID, chunks: tuple[LegalChunk, ...]
    ) -> None:
        self.chunks = chunks
        self.chunk_writes += 1

    async def find_for_version(self, version_id: UUID) -> LegalSourceProof:
        return self.proof


@pytest.mark.parametrize("exact_edge_whitespace", [False, True])
async def test_v5_import_replay_chunks_and_release_quality(
    tmp_path: Path,
    exact_edge_whitespace: bool,
) -> None:
    prepared = _prepared(tmp_path)
    command = prepared.command
    articles = prepared.articles
    if exact_edge_whitespace:
        paragraphs = (
            " \u3000第一条 " + "甲。" * 90 + "\t ",
            " 重复段。" + "乙。" * 90 + " ",
            " 重复段。" + "乙。" * 90 + " \n",
        )
        text = "".join(paragraphs)
        command = replace(command, provisions=(_draft(text),))
        articles = (
            ParsedArticle(
                provision_no="第一条",
                structure_path=("第一条",),
                text=text,
                char_start=0,
                char_end=len(text),
                paragraphs=paragraphs,
            ),
        )
    repo = _PipelineRepo(_source_proof(prepared))
    service = LegalCorpusImportService(repo)
    first = await service.import_version(command)
    before = tuple(repo.provisions)
    assert_provisions_match(command.provisions, before, parser_version="corpus-docx-v5")
    assert tuple(p.full_text for p in before) == tuple(p.full_text for p in command.provisions)
    options = dict(
        version_id=first.version_id,
        provisions=before,
        articles=articles,
        parser_version="corpus-docx-v5/hierarchical-v2",
        max_leaf_chars=120,
        window_chars=80,
        overlap_chars=20,
    )
    chunks = derive_hierarchical_chunks(**options)
    await persist_or_replay_chunk_graph(repo, first.version_id, chunks, replayed=False)
    stored = repo.chunks
    replay = await service.import_version(command)
    assert replay.replayed and replay.version_id == first.version_id
    await persist_or_replay_chunk_graph(
        repo, first.version_id, derive_hierarchical_chunks(**options), replayed=True
    )
    assert repo.chunks == stored and repo.chunk_writes == 1 and tuple(repo.provisions) == before
    for parent in before:
        covered: set[int] = set()
        for leaf in select_index_leaves(repo.chunks):
            if leaf.provision_id != parent.id:
                continue
            start, end = leaf.parent_relative_char_start, leaf.parent_relative_char_end
            assert start is not None and end is not None
            assert leaf.content == parent.full_text[start:end]
            covered.update(range(start, end))
        assert covered == set(range(len(parent.full_text)))
    selection = ReleaseSelection(
        first.version_id,
        repo.proof.source_ref,
        repo.proof.source_sha256.hex(),
        repo.proof.input_sha256.hex(),
        repo.proof.structure_sha256.hex(),
        "review:synthetic-metadata",
        len(before),
        len(chunks),
        first.instrument_id,
    )
    configuration = ReleaseConfiguration(
        "laws", "synthetic-no-model", 3, "corpus-docx-v5/hierarchical-v2", "a" * 64
    )
    quality = ReleaseQualityService(repo, repo, repo)
    report = await quality.check((selection,), configuration)
    assert report.passed, report.versions[0].blockers
    repo.provisions[0] = replace(repo.provisions[0], title="")
    damaged = await quality.check((selection,), configuration)
    assert not damaged.passed
    assert "version_content_hash_mismatch" in damaged.versions[0].blockers


async def test_v5_import_rejects_changed_whitespace_and_v4_identity() -> None:
    repo = _Repo()
    command = _command((_draft(" \t甲\n"),), parser_version="corpus-docx-v5")
    service = LegalCorpusImportService(repo)
    await service.import_version(command)
    assert repo.provisions[0].full_text == " \t甲\n"
    with pytest.raises(LegalCorpusImportConflict, match="different content"):
        await service.import_version(replace(command, provisions=(_draft("甲"),)))
    with pytest.raises(LegalCorpusImportConflict, match="different content"):
        await service.import_version(replace(command, parser_version="corpus-docx-v4"))
