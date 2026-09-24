from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest

from lawyer_agent.application.legal_chunk_structure import (
    LegalChunkStructureError,
    derive_hierarchical_chunks,
)
from lawyer_agent.application.legal_corpus_import import (
    LegalCorpusImportConflict,
    LegalCorpusImportPort,
    LegalCorpusImportService,
    LegalImportCommand,
    LegalProvisionDraft,
    _content_hash,
)
from lawyer_agent.application.legal_index_chunks import select_index_leaves
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)
from lawyer_agent.infrastructure.documents.parsers import ParsedArticle


class _Repo(LegalCorpusImportPort):
    def __init__(self) -> None:
        self.instrument: LegalInstrument | None = None
        self.versions: list[LegalVersion] = []
        self.provisions: list[Provision] = []

    async def find_instrument_by_identity(
        self, title: str, jurisdiction: str
    ) -> LegalInstrument | None:
        return self.instrument

    async def create_instrument(self, instrument: LegalInstrument) -> None:
        self.instrument = instrument

    async def find_version(
        self, instrument_id: UUID, version_label: str
    ) -> LegalVersion | None:
        return next(
            (
                version
                for version in self.versions
                if version.instrument_id == instrument_id
                and version.version_label == version_label
            ),
            None,
        )

    async def create_version(self, version: LegalVersion) -> None:
        self.versions.append(version)

    async def create_provisions(self, provisions: tuple[Provision, ...]) -> None:
        self.provisions.extend(provisions)


def _draft(
    number: str,
    text: str,
    *,
    path: tuple[str, ...] | None = None,
) -> LegalProvisionDraft:
    return LegalProvisionDraft(
        provision_no=number,
        level=ProvisionLevel.ARTICLE,
        structure_path=path or (number,),
        title=None,
        full_text=text,
    )


def _command(
    provisions: tuple[LegalProvisionDraft, ...],
    *,
    parser_version: str = "corpus-docx-v4",
) -> LegalImportCommand:
    return LegalImportCommand(
        title="合成法规",
        issuing_authority="合成机关",
        jurisdiction="national",
        region_code=None,
        version_label="合成版本",
        status=LegalVersionStatus.CURRENT,
        published_on=None,
        effective_on=None,
        repealed_on=None,
        law_number=None,
        source_ref="synthetic://v4-exact-text",
        dataset_version="synthetic-v1",
        parser_version=parser_version,
        provisions=provisions,
    )


def _encoded_field(name: str, value: str) -> bytes:
    encoded_name = name.encode("utf-8")
    encoded_value = value.encode("utf-8")
    return (
        len(encoded_name).to_bytes(4, "big")
        + encoded_name
        + len(encoded_value).to_bytes(8, "big")
        + encoded_value
    )


def _encoded_nullable_field(name: str, value: str | None) -> bytes:
    encoded_name = name.encode("utf-8")
    encoded = len(encoded_name).to_bytes(4, "big") + encoded_name
    if value is None:
        return encoded + b"\x00"
    encoded_value = value.encode("utf-8")
    return encoded + b"\x01" + len(encoded_value).to_bytes(8, "big") + encoded_value


async def test_v4_import_preserves_exact_text_offsets_hash_and_replay() -> None:
    texts = (" \u3000第一条\ue004 甲。 \n", "\t第二条  乙。\u3000")
    repo = _Repo()
    command = _command((_draft("第一条", texts[0]), _draft("第二条", texts[1])))

    first = await LegalCorpusImportService(repo).import_version(command)
    replay = await LegalCorpusImportService(repo).import_version(command)

    assert replay.replayed and replay.version_id == first.version_id
    assert [item.full_text for item in repo.provisions] == list(texts)
    assert [item.content_hash for item in repo.provisions] == [
        content_sha256(text) for text in texts
    ]
    assert [(item.char_start, item.char_end) for item in repo.provisions] == [
        (0, len(texts[0])),
        (len(texts[0]), len(texts[0]) + len(texts[1])),
    ]


async def test_v4_hash_is_structural_utf8_and_rejects_boundary_or_space_change() -> None:
    first = _draft("第一条", "甲", path=("第一章", "第一条"))
    second = _draft("第二条", "乙", path=("第一章", "第二条"))
    expected = sha256(
        b"lawyer-agent:legal-provisions:v4\x00"
        + (2).to_bytes(8, "big")
        + b"provision\x00"
        + _encoded_field("provision_no", "第一条")
        + _encoded_field("level", "article")
        + (2).to_bytes(8, "big")
        + _encoded_field("structure_path", "第一章")
        + _encoded_field("structure_path", "第一条")
        + _encoded_nullable_field("title", None)
        + _encoded_field("full_text", "甲")
        + b"provision\x00"
        + _encoded_field("provision_no", "第二条")
        + _encoded_field("level", "article")
        + (2).to_bytes(8, "big")
        + _encoded_field("structure_path", "第一章")
        + _encoded_field("structure_path", "第二条")
        + _encoded_nullable_field("title", None)
        + _encoded_field("full_text", "乙")
    ).digest()
    assert _content_hash((first, second), parser_version="corpus-docx-v4") == expected
    assert _content_hash(
        (_draft("第一条", "甲乙"),), parser_version="corpus-docx-v4"
    ) != _content_hash((first, second), parser_version="corpus-docx-v4")

    repo = _Repo()
    service = LegalCorpusImportService(repo)
    await service.import_version(_command((first, second)))
    with pytest.raises(LegalCorpusImportConflict, match="different content"):
        await service.import_version(_command((replace(first, full_text=" 甲"), second)))
    with pytest.raises(LegalCorpusImportConflict, match="different content"):
        await service.import_version(_command((_draft("第一条", "甲乙"),)))


async def test_v4_hash_and_replay_distinguish_none_from_empty_title() -> None:
    without_title = _draft("第一条", "甲")
    empty_title = replace(without_title, title="")

    assert _content_hash(
        (without_title,), parser_version="corpus-docx-v4"
    ) != _content_hash((empty_title,), parser_version="corpus-docx-v4")

    repo = _Repo()
    service = LegalCorpusImportService(repo)
    await service.import_version(_command((without_title,)))
    assert repo.provisions[0].title is None
    with pytest.raises(LegalCorpusImportConflict, match="different content"):
        await service.import_version(_command((empty_title,)))


@pytest.mark.parametrize("parser_version", ["docx-v1", "corpus-docx-v3", "xcorpus-docx-v4"])
async def test_non_exact_v4_labels_keep_legacy_strip_and_hash(
    parser_version: str,
) -> None:
    drafts = (_draft("第一条", " 甲 "), _draft("第二条", "\t乙\n"))
    repo = _Repo()
    await LegalCorpusImportService(repo).import_version(
        _command(drafts, parser_version=parser_version)
    )
    assert [item.full_text for item in repo.provisions] == ["甲", "乙"]
    assert repo.versions[0].content_hash == content_sha256("甲乙")


async def test_v4_hierarchical_leaves_cover_exact_imported_parent_text() -> None:
    repeated_paragraph = "重复段。 \t" + "乙；" * 90
    paragraphs = (
        " \u3000第一条\ue004 " + "甲。" * 90,
        repeated_paragraph,
        repeated_paragraph,
        "末段。 " + "丙。" * 90 + "\n",
    )
    repo = _Repo()
    await LegalCorpusImportService(repo).import_version(
        _command((_draft("第一条", "".join(paragraphs)),))
    )
    provision = repo.provisions[0]
    article = ParsedArticle(
        provision_no="第一条",
        text=provision.full_text,
        structure_path=("第一条",),
        char_start=0,
        char_end=len(provision.full_text),
        paragraphs=paragraphs,
    )
    chunks = derive_hierarchical_chunks(
        version_id=repo.versions[0].id,
        provisions=(provision,),
        articles=(article,),
        parser_version="corpus-docx-v4/hierarchical-v2",
        max_leaf_chars=120,
        window_chars=80,
        overlap_chars=20,
    )
    parent = chunks[0]
    assert parent.content == "".join(paragraphs) == provision.full_text
    first_repeated_start = len(paragraphs[0])
    second_repeated_start = first_repeated_start + len(repeated_paragraph)
    assert parent.content[
        first_repeated_start : first_repeated_start + len(repeated_paragraph)
    ] == repeated_paragraph
    assert parent.content[
        second_repeated_start : second_repeated_start + len(repeated_paragraph)
    ] == repeated_paragraph
    assert first_repeated_start != second_repeated_start
    covered: set[int] = set()
    for leaf in select_index_leaves(chunks):
        start = leaf.parent_relative_char_start
        end = leaf.parent_relative_char_end
        assert start is not None and end is not None
        assert leaf.content == parent.content[start:end]
        covered.update(range(start, end))
    assert covered == set(range(len(parent.content)))


def test_v4_long_article_rejects_when_exact_units_cannot_be_built() -> None:
    text = " 第一条 " + "甲。" * 80
    provision = Provision(
        id=new_uuid7(),
        version_id=new_uuid7(),
        provision_no="第一条",
        level=ProvisionLevel.ARTICLE,
        structure_path=("第一条",),
        title=None,
        full_text=text,
        content_hash=content_sha256(text),
        char_start=0,
        char_end=len(text),
    )
    article = ParsedArticle(
        provision_no="第一条",
        text=text,
        structure_path=("第一条",),
        char_start=0,
        char_end=len(text),
        paragraphs=(text[:-1],),
    )
    with pytest.raises(LegalChunkStructureError, match="exact paragraph units"):
        derive_hierarchical_chunks(
            version_id=provision.version_id,
            provisions=(provision,),
            articles=(article,),
            parser_version="corpus-docx-v4/hierarchical-v2",
            max_leaf_chars=100,
            window_chars=80,
            overlap_chars=20,
        )
