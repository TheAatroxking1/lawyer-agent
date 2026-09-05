from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest

from lawyer_agent.application.legal_chunk_structure import (
    LegalChunkStructureError,
    derive_hierarchical_chunks,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import ChunkType, LegalChunk

VERSION = new_uuid7()


@dataclass(frozen=True)
class _Prov:
    id: UUID
    full_text: str
    char_start: int


@dataclass(frozen=True)
class _Art:
    provision_no: str
    structure_path: tuple[str, ...]
    paragraphs: tuple[str, ...]


def _article(no: str, paragraphs: list[str]) -> _Art:
    return _Art(
        provision_no=no,
        structure_path=("第一章 总则",),
        paragraphs=tuple(paragraphs),
    )


def _prov(no: str, text: str, at: int) -> _Prov:
    return _Prov(id=new_uuid7(), full_text=text, char_start=at)


def _collect(chunks: tuple[LegalChunk, ...]) -> dict[str, list[str]]:
    by_type: dict[str, list[str]] = {}
    for chunk in chunks:
        by_type.setdefault(chunk.chunk_type.value, []).append(chunk.content)
    return by_type


def test_every_article_gets_a_parent_and_short_article_keeps_one_child() -> None:
    article = _article("第一条", ["第一条 出租人应交付租赁物。"])
    provision = _prov("第一条", "第一条 出租人应交付租赁物。", 0)
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(provision,),
        articles=(article,),
        parser_version="docx-v2",
    )
    assert len(chunks) == 2
    parent, child = chunks
    assert parent.chunk_type is ChunkType.PROVISION
    assert parent.parent_chunk_id is None
    assert parent.content == "第一条 出租人应交付租赁物。"
    assert child.chunk_type is ChunkType.PARAGRAPH
    assert child.parent_chunk_id == parent.id
    assert child.provision_id == parent.provision_id
    assert child.content == "出租人应交付租赁物。"


def test_long_article_splits_paragraphs_items_and_sub_items() -> None:
    article = _article(
        "第一条",
        [
            "第一条 出租人应保持租赁物符合约定用途。",
            "承租人应当按照约定的方法使用租赁物。",
            "（一）用于约定用途。",
            "（二）不得擅自转租。",
            "1. 转租需出租人同意。",
        ],
    )
    provision = _prov(
        "第一条",
        "第一条 出租人应保持租赁物符合约定用途。承租人应当按照约定的方法使用"
        "租赁物。（一）用于约定用途。（二）不得擅自转租。1. 转租需出租人同意。",
        0,
    )
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(provision,),
        articles=(article,),
        parser_version="docx-v2",
    )
    parent = chunks[0]
    children = chunks[1:]
    assert parent.chunk_type is ChunkType.PROVISION
    kinds = [child.chunk_type for child in children]
    assert kinds == [
        ChunkType.PARAGRAPH,
        ChunkType.PARAGRAPH,
        ChunkType.ITEM,
        ChunkType.ITEM,
        ChunkType.SUB_ITEM,
    ]
    assert children[0].content == "出租人应保持租赁物符合约定用途。"
    assert children[2].content == "（一）用于约定用途。"
    assert all(child.parent_chunk_id == parent.id for child in children)


def test_articles_are_never_merged_and_short_articles_not_joined() -> None:
    provisions = (
        _prov("第一条", "第一条 甲。", 0),
        _prov("第二条", "第二条 乙。", 6),
    )
    articles = (
        _article("第一条", ["第一条 甲。"]),
        _article("第二条", ["第二条 乙。"]),
    )
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=provisions,
        articles=articles,
        parser_version="docx-v2",
    )
    assert [chunk.chunk_type for chunk in chunks] == [
        ChunkType.PROVISION,
        ChunkType.PARAGRAPH,
        ChunkType.PROVISION,
        ChunkType.PARAGRAPH,
    ]
    # Each 条 parent maps to its own provision id and owns its own children.
    first_parent, first_child, second_parent, second_child = chunks
    assert first_parent.provision_id != second_parent.provision_id
    assert first_child.parent_chunk_id == first_parent.id
    assert second_child.parent_chunk_id == second_parent.id


def test_overlong_leaf_falls_back_to_sliding_windows_never_crossing_unit() -> None:
    body = "承租人逾期支付租金，出租人可以请求支付欠付租金。" * 40
    paragraph = "第一条 " + body
    article = _article("第一条", [paragraph])
    provision = _prov("第一条", paragraph, 0)
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(provision,),
        articles=(article,),
        parser_version="docx-v2",
        max_leaf_chars=300,
        window_chars=200,
        overlap_chars=40,
    )
    parent = chunks[0]
    children = chunks[1:]
    assert len(children) > 1
    assert all(child.chunk_type is ChunkType.PARAGRAPH for child in children)
    assert all(child.parent_chunk_id == parent.id for child in children)
    assert all(len(child.content) <= 200 for child in children)
    # Overlapping windows keep the leaf covered end to end without losing text.
    assert children[0].content.startswith(body[:8])
    assert children[-1].content.endswith(body[-8:])


def test_without_paragraph_data_falls_back_to_stripped_whole_article() -> None:
    article = _Art(
        provision_no="第一条",
        structure_path=(),
        paragraphs=(),
    )
    provision = _prov("第一条", "第一条 仅有全文，无段落明细。", 0)
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(provision,),
        articles=(article,),
        parser_version="docx-v2",
    )
    assert len(chunks) == 2
    assert chunks[1].content == "仅有全文，无段落明细。"


def test_invalid_inputs_are_rejected_stably() -> None:
    article = _article("第一条", ["第一条 甲。"])
    provision = _prov("第一条", "第一条 甲。", 0)
    with pytest.raises(LegalChunkStructureError, match="non-empty"):
        derive_hierarchical_chunks(
            version_id=VERSION,
            provisions=(),
            articles=(article,),
            parser_version="docx-v2",
        )
    with pytest.raises(LegalChunkStructureError, match="correspond"):
        derive_hierarchical_chunks(
            version_id=VERSION,
            provisions=(provision, provision),
            articles=(article,),
            parser_version="docx-v2",
        )
    with pytest.raises(LegalChunkStructureError, match="overlap"):
        derive_hierarchical_chunks(
            version_id=VERSION,
            provisions=(provision,),
            articles=(article,),
            parser_version="docx-v2",
            window_chars=100,
            overlap_chars=200,
        )
