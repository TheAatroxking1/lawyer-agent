from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import pytest

from lawyer_agent.application.legal_chunk_structure import (
    LegalChunkStructureError,
    derive_hierarchical_chunks,
    derive_hierarchical_chunks_with_locations,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import ChunkType, LegalChunk, content_sha256

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


def test_short_article_keeps_only_the_authoritative_parent() -> None:
    article = _article("第一条", ["第一条 出租人应交付租赁物。"])
    provision = _prov("第一条", "第一条 出租人应交付租赁物。", 0)
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(provision,),
        articles=(article,),
        parser_version="docx-v2",
    )
    assert len(chunks) == 1
    parent = chunks[0]
    assert parent.chunk_type is ChunkType.PROVISION
    assert parent.parent_chunk_id is None
    assert parent.content == "第一条 出租人应交付租赁物。"
    assert parent.content_hash == content_sha256(provision.full_text)


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
        max_leaf_chars=50,
        window_chars=40,
        overlap_chars=10,
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
        ChunkType.PROVISION,
    ]
    first_parent, second_parent = chunks
    assert first_parent.provision_id != second_parent.provision_id


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


def test_sliding_windows_cover_every_character_in_one_trusted_unit() -> None:
    body = "".join(chr(0x4E00 + index) for index in range(100))
    paragraph = "第一条 " + body
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(_prov("第一条", paragraph, 0),),
        articles=(_article("第一条", [paragraph]),),
        parser_version="docx-v2",
        max_leaf_chars=50,
        window_chars=30,
        overlap_chars=5,
    )
    pieces = [chunk.content for chunk in chunks[1:]]
    assert len(pieces) > 1
    assert all(any(character in piece for piece in pieces) for character in body)


def test_article_at_threshold_keeps_only_parent() -> None:
    full_text = "第一条 " + ("甲" * 13)
    assert len(full_text) == 17
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(_prov("第一条", full_text, 0),),
        articles=(_article("第一条", [full_text]),),
        parser_version="docx-v2",
        max_leaf_chars=17,
        window_chars=10,
        overlap_chars=2,
    )
    assert len(chunks) == 1


@pytest.mark.parametrize(
    "paragraphs",
    [(), ("",), ("第一条 权威全文被错误截断。",)],
)
def test_untrusted_paragraph_data_falls_back_to_whole_parent_only(
    paragraphs: tuple[str, ...],
) -> None:
    article = _Art(
        provision_no="第一条",
        structure_path=(),
        paragraphs=paragraphs,
    )
    provision = _prov("第一条", "第一条 仅有全文，无段落明细。", 0)
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(provision,),
        articles=(article,),
        parser_version="docx-v2",
        max_leaf_chars=10,
        window_chars=8,
        overlap_chars=2,
    )
    assert len(chunks) == 1
    assert chunks[0].content == "第一条 仅有全文，无段落明细。"


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


@pytest.mark.parametrize("number", ["第一条之一", "第12条之2", "第１２条之２"])
def test_long_supplement_strips_full_marker_only_from_first_child(number: str) -> None:
    body = "承租人应当按照约定使用租赁物。"
    reference = "第一条之一规定的情形，适用本款。"
    paragraphs = [f"{number}\u3000{body}", reference]
    full_text = "".join(paragraphs)
    chunks = derive_hierarchical_chunks(
        version_id=VERSION,
        provisions=(_prov(number, full_text, 7),),
        articles=(_article(number, paragraphs),),
        parser_version="docx-supplement-test",
        max_leaf_chars=30,
        window_chars=25,
        overlap_chars=5,
    )
    parent, first, second = chunks
    assert parent.content == full_text
    assert parent.content_hash == content_sha256(full_text)
    assert first.content == body
    assert second.content == reference
    assert first.parent_chunk_id == second.parent_chunk_id == parent.id


def test_located_windows_report_actual_offsets_for_repeated_text() -> None:
    paragraph = "第一条 " + "甲" * 1200
    located = derive_hierarchical_chunks_with_locations(
        version_id=VERSION,
        provisions=(_prov("第一条", paragraph, 0),),
        articles=(_article("第一条", [paragraph]),),
        parser_version="location-test",
        max_leaf_chars=600,
        window_chars=400,
        overlap_chars=60,
    )

    assert [item.parent_relative_char_span for item in located] == [
        (0, 1204), (4, 404), (344, 744), (684, 1084), (1024, 1204)
    ]
    assert [item.chunk.content for item in located[1:]] == [
        paragraph[start:end] for start, end in [
            (4, 404), (344, 744), (684, 1084), (1024, 1204)
        ]
    ]


def test_located_identical_paragraphs_keep_distinct_parent_offsets() -> None:
    lines = ["第一条 " + "甲" * 40, "甲" * 40]
    located = derive_hierarchical_chunks_with_locations(
        version_id=VERSION,
        provisions=(_prov("第一条", "".join(lines), 0),),
        articles=(_article("第一条", lines),),
        parser_version="location-test",
        max_leaf_chars=30,
        window_chars=20,
        overlap_chars=5,
    )
    child_spans = [item.parent_relative_char_span for item in located[1:]]
    assert child_spans[:3] == [(4, 24), (19, 39), (34, 44)]
    assert child_spans[3:] == [(44, 64), (59, 79), (74, 84)]


def test_located_first_unit_starts_after_heading_when_body_repeats_heading() -> None:
    lines = ("第一条 第一条", "甲" * 601)
    located = derive_hierarchical_chunks_with_locations(
        version_id=VERSION,
        provisions=(_prov("第一条", "".join(lines), 0),),
        articles=(_article("第一条", list(lines)),),
        parser_version="location-test",
        max_leaf_chars=600,
        window_chars=400,
        overlap_chars=60,
    )

    first_child = located[1]
    assert first_child.chunk.content == "第一条"
    assert first_child.parent_relative_char_span == (4, 7)
