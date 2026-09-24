from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalChunk,
    content_sha256,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_INSTRUMENT = UUID("01a06ae2-7000-7000-8000-000000000001")
_VERSION = UUID("01a06ae2-7000-7000-8000-000000000002")
_OTHER_VERSION = UUID("01a06ae2-7000-7000-8000-000000000003")
_PROVISION = UUID("01a06ae2-7000-7000-8000-000000000004")
_OTHER_PROVISION = UUID("01a06ae2-7000-7000-8000-000000000005")
_OLD_CHUNK = UUID("01a06ae2-7000-7000-8000-000000000006")
_OTHER_CHUNK = UUID("01a06ae2-7000-7000-8000-000000000007")
_PARENT = UUID("01a06ae2-7000-7000-8000-000000000008")
_PARAGRAPH_A = UUID("01a06ae2-7000-7000-8000-000000000009")
_PARAGRAPH_B = UUID("01a06ae2-7000-7000-8000-00000000000a")
_ITEM = UUID("01a06ae2-7000-7000-8000-00000000000b")
_FAILED = UUID("01a06ae2-7000-7000-8000-00000000000c")
_SIBLING_PROVISION = UUID("01a06ae2-7000-7000-8000-00000000000d")


def _chunk(
    chunk_id: UUID,
    chunk_type: ChunkType,
    content: str,
    *,
    version_id: UUID = _VERSION,
    provision_id: UUID = _PROVISION,
    parent_id: UUID | None = None,
    quality: ChunkQuality = ChunkQuality.OK,
) -> LegalChunk:
    return LegalChunk(
        id=chunk_id,
        version_id=version_id,
        provision_id=provision_id,
        parent_chunk_id=parent_id,
        chunk_type=chunk_type,
        quality=quality,
        content=content,
        content_hash=content_sha256(content),
        parser_version="hierarchical-v1",
    )


async def _seed_compatible_rows(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,category,version) "
                    "VALUES (:id,'层级分块示例法','示例机关','national','law',1)"
                ),
                {"id": _INSTRUMENT.bytes},
            )
            for version_id, label in (
                (_VERSION, "2024版"),
                (_OTHER_VERSION, "2020版"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO legal_versions "
                        "(id,instrument_id,version_label,status,content_hash) "
                        "VALUES (:id,:instrument,:label,'current',:hash)"
                    ),
                    {
                        "id": version_id.bytes,
                        "instrument": _INSTRUMENT.bytes,
                        "label": label,
                        "hash": bytes(32),
                    },
                )
            for provision_id, version_id, number in (
                (_PROVISION, _VERSION, "第一条"),
                (_SIBLING_PROVISION, _VERSION, "第三条"),
                (_OTHER_PROVISION, _OTHER_VERSION, "第二条"),
            ):
                body = f"{number} 示例内容。"
                await connection.execute(
                    text(
                        "INSERT INTO legal_provisions "
                        "(id,version_id,provision_no,level,full_text,content_hash,"
                        "char_start,char_end) VALUES "
                        "(:id,:version,:number,'article',:body,:hash,0,:end)"
                    ),
                    {
                        "id": provision_id.bytes,
                        "version": version_id.bytes,
                        "number": number,
                        "body": body,
                        "hash": content_sha256(body),
                        "end": len(body),
                    },
                )
            for chunk_id, version_id, provision_id, body in (
                (_OLD_CHUNK, _VERSION, _PROVISION, "旧目标块"),
                (_OTHER_CHUNK, _OTHER_VERSION, _OTHER_PROVISION, "旧版本块"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO legal_chunks "
                        "(id,version_id,provision_id,chunk_type,quality,content,"
                        "content_hash,parser_version) VALUES "
                        "(:id,:version,:provision,'provision','ok',:body,:hash,'legacy-v1')"
                    ),
                    {
                        "id": chunk_id.bytes,
                        "version": version_id.bytes,
                        "provision": provision_id.bytes,
                        "body": body,
                        "hash": content_sha256(body),
                    },
                )
    finally:
        await engine.dispose()


def _hierarchy() -> tuple[LegalChunk, ...]:
    parent = _chunk(_PARENT, ChunkType.PROVISION, "第一条 完整条文。")
    paragraph_a = _chunk(
        _PARAGRAPH_A, ChunkType.PARAGRAPH, "第一款。", parent_id=_PARENT
    )
    paragraph_b = _chunk(
        _PARAGRAPH_B, ChunkType.PARAGRAPH, "第二款。", parent_id=_PARENT
    )
    item = _chunk(_ITEM, ChunkType.ITEM, "（一）事项。", parent_id=_PARAGRAPH_B)
    failed = _chunk(
        _FAILED,
        ChunkType.SUB_ITEM,
        "失败质量留档。",
        parent_id=_ITEM,
        quality=ChunkQuality.FAILED,
    )
    return failed, item, paragraph_b, paragraph_a, parent


async def _exercise_repository(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            repository = SqlAlchemyLegalCorpusChunkRepository(session)
            hierarchy = _hierarchy()
            await repository.replace_chunks_for_version(_VERSION, hierarchy)
            await session.commit()

            rows = await repository.chunks_for_version(_VERSION)
            by_id = {row.id: row for row in rows}
            assert set(by_id) == {row.id for row in hierarchy}
            assert by_id[_PARAGRAPH_A].chunk_type is ChunkType.PARAGRAPH
            assert by_id[_ITEM].chunk_type is ChunkType.ITEM
            assert by_id[_ITEM].parent_chunk_id == _PARAGRAPH_B
            assert by_id[_FAILED].quality is ChunkQuality.FAILED
            assert {row.id for row in await repository.chunks_for_version(_OTHER_VERSION)} == {
                _OTHER_CHUNK
            }

            # An identical replay is accepted, including reverse parent-first input.
            await repository.replace_chunks_for_version(_VERSION, hierarchy)
            await session.commit()
            assert len(await repository.chunks_for_version(_VERSION)) == 5

            old_ids = {row.id for row in await repository.chunks_for_version(_VERSION)}
            invalid_graphs = (
                (
                    _chunk(
                        UUID("01a06ae2-7000-7000-8000-000000000011"),
                        ChunkType.PARAGRAPH,
                        "父节点缺失",
                        parent_id=UUID("01a06ae2-7000-7000-8000-000000000012"),
                    ),
                ),
                (
                    _chunk(
                        UUID("01a06ae2-7000-7000-8000-000000000013"),
                        ChunkType.PARAGRAPH,
                        "循环甲",
                        parent_id=UUID("01a06ae2-7000-7000-8000-000000000014"),
                    ),
                    _chunk(
                        UUID("01a06ae2-7000-7000-8000-000000000014"),
                        ChunkType.ITEM,
                        "循环乙",
                        parent_id=UUID("01a06ae2-7000-7000-8000-000000000013"),
                    ),
                ),
                (
                    _chunk(
                        UUID("01a06ae2-7000-7000-8000-000000000015"),
                        ChunkType.PROVISION,
                        "错误条文归属",
                        provision_id=_OTHER_PROVISION,
                    ),
                ),
                (
                    _chunk(
                        UUID("01a06ae2-7000-7000-8000-000000000016"),
                        ChunkType.PROVISION,
                        "同版本第一条父块",
                    ),
                    _chunk(
                        UUID("01a06ae2-7000-7000-8000-000000000017"),
                        ChunkType.PARAGRAPH,
                        "错误跨条父链",
                        provision_id=_SIBLING_PROVISION,
                        parent_id=UUID(
                            "01a06ae2-7000-7000-8000-000000000016"
                        ),
                    ),
                ),
                (
                    _chunk(
                        UUID("01a06ae2-7000-7000-8000-000000000018"),
                        ChunkType.PROVISION,
                        "错误版本",
                        version_id=_OTHER_VERSION,
                        provision_id=_OTHER_PROVISION,
                    ),
                ),
                (hierarchy[-1], hierarchy[-1]),
            )
            for invalid in invalid_graphs:
                with pytest.raises(ValueError):
                    await repository.replace_chunks_for_version(_VERSION, invalid)
                assert {
                    row.id for row in await repository.chunks_for_version(_VERSION)
                } == old_ids

            with pytest.raises(ValueError, match="target version does not exist"):
                await repository.replace_chunks_for_version(
                    UUID("01a06ae2-7000-7000-8000-000000000019"), ()
                )
            assert {
                row.id for row in await repository.chunks_for_version(_VERSION)
            } == old_ids

            # A database insertion failure rolls the savepoint back to old chunks.
            colliding = _chunk(_OTHER_CHUNK, ChunkType.PROVISION, "主键碰撞")
            with pytest.raises(IntegrityError):
                await repository.replace_chunks_for_version(_VERSION, (colliding,))
            assert {
                row.id for row in await repository.chunks_for_version(_VERSION)
            } == old_ids

            # Empty replacement clears only the target version.
            await repository.replace_chunks_for_version(_VERSION, ())
            await session.commit()
            assert await repository.chunks_for_version(_VERSION) == ()
            assert {row.id for row in await repository.chunks_for_version(_OTHER_VERSION)} == {
                _OTHER_CHUNK
            }
    finally:
        await engine.dispose()


async def _restore_duplicate_hierarchy(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            repository = SqlAlchemyLegalCorpusChunkRepository(session)
            await repository.replace_chunks_for_version(_VERSION, _hierarchy())
            await session.commit()
    finally:
        await engine.dispose()


async def _make_downgrade_compatible(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            repository = SqlAlchemyLegalCorpusChunkRepository(session)
            compatible = (_chunk(_PARENT, ChunkType.PROVISION, "兼容整条块"),)
            await repository.replace_chunks_for_version(_VERSION, compatible)
            await session.commit()
    finally:
        await engine.dispose()


async def _chunk_ids(mysql_url: URL, version_id: UUID) -> set[UUID]:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            repository = SqlAlchemyLegalCorpusChunkRepository(session)
            return {row.id for row in await repository.chunks_for_version(version_id)}
    finally:
        await engine.dispose()


def test_hierarchical_chunks_replace_safely_and_migration_round_trips(
    mysql_url: URL,
) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "20260905_11")
    asyncio.run(_seed_compatible_rows(mysql_url))
    command.upgrade(config, "head")

    asyncio.run(_exercise_repository(mysql_url))
    asyncio.run(_restore_duplicate_hierarchy(mysql_url))
    with pytest.raises(RuntimeError, match="duplicate hierarchical chunks"):
        command.downgrade(config, "20260905_11")
    assert asyncio.run(_chunk_ids(mysql_url, _VERSION)) == {
        row.id for row in _hierarchy()
    }

    asyncio.run(_make_downgrade_compatible(mysql_url))
    command.downgrade(config, "20260905_11")
    command.upgrade(config, "head")
    command.check(config)
    assert asyncio.run(_chunk_ids(mysql_url, _VERSION)) == {_PARENT}
    assert asyncio.run(_chunk_ids(mysql_url, _OTHER_VERSION)) == {_OTHER_CHUNK}
