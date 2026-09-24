from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalCategory
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_OLD_ID = UUID("01a06ae2-6000-7000-8000-0000000000d1")
_LAW_ID = UUID("01a06ae2-6000-7000-8000-0000000000d2")


async def _insert_pre_migration_row(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,version) "
                    "VALUES (:id,'旧分类示例法','示例机关','national',1)"
                ),
                {"id": _OLD_ID.bytes},
            )
    finally:
        await engine.dispose()


async def _assert_category_storage(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            repository = SqlAlchemyLegalCorpusRepository(session)
            old = await repository.instrument_by_id(_OLD_ID)
            assert old is not None
            assert old.category is LegalCategory.UNKNOWN

            await session.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id,title,issuing_authority,jurisdiction,category,version) "
                    "VALUES (:id,'法律分类示例','示例机关','national','law',1)"
                ),
                {"id": _LAW_ID.bytes},
            )
            await session.commit()
            law = await repository.instrument_by_id(_LAW_ID)
            assert law is not None
            assert law.category is LegalCategory.LAW

        for index, category in enumerate(LegalCategory):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO legal_instruments "
                        "(id,title,issuing_authority,jurisdiction,category,version) "
                        "VALUES (:id,:title,'示例机关','national',:category,1)"
                    ),
                    {
                        "id": new_uuid7().bytes,
                        "title": f"合法分类示例-{index}",
                        "category": category.value,
                    },
                )

        for index, category in enumerate(("foreign_law", "LAW", "Law", "law ")):
            async with engine.begin() as connection:
                with pytest.raises(OperationalError) as captured:
                    await connection.execute(
                        text(
                            "INSERT INTO legal_instruments "
                            "(id,title,issuing_authority,jurisdiction,category,version) "
                            "VALUES (:id,:title,'示例机关','national',:category,1)"
                        ),
                        {
                            "id": new_uuid7().bytes,
                            "title": f"非法分类示例-{index}",
                            "category": category,
                        },
                    )
                assert captured.value.orig.args[0] == 3819
    finally:
        await engine.dispose()


def test_legal_category_migration_preserves_old_rows_and_enforces_values(
    mysql_url: URL,
) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "20260905_10")
    asyncio.run(_insert_pre_migration_row(mysql_url))
    command.upgrade(config, "head")
    asyncio.run(_assert_category_storage(mysql_url))
    command.check(config)

    command.downgrade(config, "20260905_10")
    command.upgrade(config, "head")
