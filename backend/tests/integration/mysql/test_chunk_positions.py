import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config
from test_legal_corpus_chunks_repositories import (
    _VERSION_NEW,
    _create_database,
    _drop_database,
    _seed,
)

from alembic import command
from lawyer_agent.application.legal_chunk_structure import derive_hierarchical_chunks
from lawyer_agent.infrastructure.documents.parsers import ParsedArticle
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusChunkRepository,
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def test_chunk_positions_upgrade_roundtrip_check_and_guard(mysql_url):
    name = "lawyer_test_" + uuid4().hex
    asyncio.run(_create_database(mysql_url, name))
    url = mysql_url.set(database=name)
    config = _alembic_config(url)
    try:
        command.upgrade(config, "20260906_14")
        asyncio.run(_seed(url))
        command.upgrade(config, "head")
        command.check(config)

        async def verify():
            engine = create_async_engine(url)
            try:
                async with async_sessionmaker(engine)() as session, session.begin():
                    corpus = SqlAlchemyLegalCorpusRepository(session)
                    provisions = await corpus.provisions_for_version(_VERSION_NEW)
                    p = provisions[0]
                    parsed = ParsedArticle(p.provision_no, (), p.full_text, 0, len(p.full_text),
                                           (p.full_text,))
                    chunks = derive_hierarchical_chunks(
                        version_id=_VERSION_NEW, provisions=provisions, articles=(parsed,),
                        parser_version="synthetic/hierarchical-v2",
                    )
                    await SqlAlchemyLegalCorpusChunkRepository(session).replace_chunks_for_version(
                        _VERSION_NEW, chunks,
                    )
                async with async_sessionmaker(engine)() as session:
                    loaded = await SqlAlchemyLegalCorpusChunkRepository(session).chunks_for_version(
                        _VERSION_NEW,
                    )
                    assert loaded == chunks
                    assert loaded[0].parent_relative_char_start == 0
                    assert loaded[0].parent_relative_char_end == len(p.full_text)
                with pytest.raises(DBAPIError) as caught:
                    async with engine.begin() as conn:
                        await conn.execute(text(
                            "UPDATE legal_chunks SET parent_relative_char_end=0"
                        ))
                assert caught.value.orig.args[0] == 3819
            finally:
                await engine.dispose()

        asyncio.run(verify())
        with pytest.raises(RuntimeError, match="archival review"):
            command.downgrade(config, "20260906_14")

        async def clear_test_spans():
            engine = create_async_engine(url)
            try:
                async with engine.begin() as conn:
                    await conn.execute(text("UPDATE legal_chunks SET "
                        "parent_relative_char_start=NULL,parent_relative_char_end=NULL"))
            finally:
                await engine.dispose()

        asyncio.run(clear_test_spans())
        command.downgrade(config, "20260906_14")
        command.upgrade(config, "head")
        command.check(config)
    finally:
        asyncio.run(_drop_database(mysql_url, name))
