from __future__ import annotations

from dataclasses import replace

import pytest

from lawyer_agent.application.legal_index_chunks import (
    LegalIndexChunkError,
    select_index_leaves,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    content_sha256,
)


def _chunk(*, text: str, parent: LegalChunk | None = None) -> LegalChunk:
    return LegalChunk(
        id=new_uuid7(),
        version_id=parent.version_id if parent else new_uuid7(),
        provision_id=parent.provision_id if parent else new_uuid7(),
        chunk_type=ChunkType.PROVISION,
        quality=ChunkQuality.OK,
        content=text,
        content_hash=content_sha256(text),
        parent_chunk_id=parent.id if parent else None,
        parser_version="docx-v1",
    )


def test_select_index_leaves_preserves_legacy_whole_provisions_and_empty_input() -> None:
    first = _chunk(text="第一条")
    second = _chunk(text="第二条")
    assert select_index_leaves(()) == ()
    assert select_index_leaves((first, second)) == (first, second)


def test_select_index_leaves_returns_only_leaf_children_in_input_order() -> None:
    parent = _chunk(text="第一条全文")
    child_a = _chunk(text="第一款", parent=parent)
    child_b = _chunk(text="第二款", parent=parent)
    assert select_index_leaves((child_b, parent, child_a)) == (child_b, child_a)


def test_select_index_leaves_handles_nested_graph_without_recursion() -> None:
    root = _chunk(text="第一条全文")
    middle = _chunk(text="第一款", parent=root)
    leaf = _chunk(text="第一项", parent=middle)
    assert select_index_leaves((root, middle, leaf)) == (leaf,)


@pytest.mark.parametrize(
    "case",
    ["duplicate", "missing", "version", "provision", "self", "cycle", "failed"],
)
def test_select_index_leaves_rejects_invalid_graph(case: str) -> None:
    parent = _chunk(text="第一条全文")
    child = _chunk(text="第一款", parent=parent)
    if case == "duplicate":
        chunks = (parent, parent)
    elif case == "missing":
        chunks = (replace(child, parent_chunk_id=new_uuid7()),)
    elif case == "version":
        chunks = (parent, replace(child, version_id=new_uuid7()))
    elif case == "provision":
        chunks = (parent, replace(child, provision_id=new_uuid7()))
    elif case == "self":
        chunks = (replace(parent, parent_chunk_id=parent.id),)
    elif case == "cycle":
        chunks = (
            replace(parent, parent_chunk_id=child.id),
            child,
        )
    else:
        chunks = (replace(parent, quality=ChunkQuality.FAILED),)
    with pytest.raises(LegalIndexChunkError):
        select_index_leaves(chunks)
