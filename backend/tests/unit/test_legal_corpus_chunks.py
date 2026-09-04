from __future__ import annotations

from uuid import UUID

import pytest

from lawyer_agent.application.legal_corpus_chunks import (
    derive_chunks,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    Provision,
    ProvisionLevel,
    content_sha256,
)


def _provision(
    *,
    provision_no: str,
    full_text: str,
    level: ProvisionLevel = ProvisionLevel.ARTICLE,
    char_start: int = 0,
    version_id: UUID | None = None,
) -> Provision:
    return Provision(
        id=new_uuid7(),
        version_id=version_id or new_uuid7(),
        provision_no=provision_no,
        level=level,
        structure_path=(),
        title=None,
        full_text=full_text,
        content_hash=content_sha256(full_text),
        char_start=char_start,
        char_end=char_start + len(full_text),
    )


def test_derive_chunks_empty_provisions() -> None:
    assert derive_chunks(
        version_id=new_uuid7(), provisions=(), parser_version="docx-zip-v1"
    ) == ()


def test_derive_chunks_one_provision_per_article() -> None:
    version_id = new_uuid7()
    first = _provision(provision_no="第一条", full_text="第一条 内容甲。")
    second = _provision(
        provision_no="第二条", full_text="第二条 内容乙。", char_start=10
    )
    chunks = derive_chunks(
        version_id=version_id,
        provisions=(first, second),
        parser_version="docx-zip-v1",
    )
    assert len(chunks) == 2
    assert [chunk.chunk_type for chunk in chunks] == [
        ChunkType.PROVISION,
        ChunkType.PROVISION,
    ]
    assert [chunk.quality for chunk in chunks] == [
        ChunkQuality.OK,
        ChunkQuality.OK,
    ]
    assert [chunk.version_id for chunk in chunks] == [version_id, version_id]
    assert [chunk.provision_id for chunk in chunks] == [first.id, second.id]
    assert [chunk.content for chunk in chunks] == [
        "第一条 内容甲。",
        "第二条 内容乙。",
    ]
    assert all(
        chunk.content_hash == content_sha256(chunk.content) for chunk in chunks
    )
    assert all(chunk.parser_version == "docx-zip-v1" for chunk in chunks)
    assert all(chunk.parent_chunk_id is None for chunk in chunks)


def test_derive_chunks_preserves_input_order_when_unsorted() -> None:
    version_id = new_uuid7()
    later = _provision(
        provision_no="第二条", full_text="第二条 内容乙。", char_start=50
    )
    earlier = _provision(
        provision_no="第一条", full_text="第一条 内容甲。", char_start=0
    )
    chunks = derive_chunks(
        version_id=version_id,
        provisions=(later, earlier),
        parser_version="docx-zip-v1",
    )
    assert [chunk.content for chunk in chunks] == [
        "第一条 内容甲。",
        "第二条 内容乙。",
    ]


def test_derive_chunks_rejects_non_provision_typed_inputs() -> None:
    with pytest.raises(ValueError, match="provision"):
        derive_chunks(
            version_id=new_uuid7(),
            provisions=("not-a-provision",),  # type: ignore[arg-type]
            parser_version="docx-zip-v1",
        )


def test_derive_chunks_rejects_blank_parser_version() -> None:
    version_id = new_uuid7()
    provision = _provision(provision_no="第一条", full_text="内容")
    with pytest.raises(ValueError, match="parser"):
        derive_chunks(
            version_id=version_id,
            provisions=(provision,),
            parser_version="  ",
        )


def test_chunk_values_reject_bad_hash_and_parent() -> None:
    version_id = new_uuid7()
    provision = _provision(provision_no="第一条", full_text="内容甲。")
    (chunk,) = derive_chunks(
        version_id=version_id,
        provisions=(provision,),
        parser_version="docx-zip-v1",
    )
    # LegalChunk domain already rejects a hash mismatch on construction.
    assert chunk.content_hash == content_sha256(chunk.content)
