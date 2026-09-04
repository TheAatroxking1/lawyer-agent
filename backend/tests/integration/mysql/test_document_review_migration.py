from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        mysql_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


async def _table_names(mysql_url: URL) -> set[str]:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
    finally:
        await engine.dispose()


async def _has_review_reason(mysql_url: URL) -> bool:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync_connection: "review_reason"
                in set(
                    column["name"]
                    for column in inspect(sync_connection).get_columns(
                        "tenant_document_versions"
                    )
                )
            )
    finally:
        await engine.dispose()


def test_document_review_reason_migration_round_trip(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    assert asyncio.run(_has_review_reason(mysql_url)) is True

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(mysql_url)) == {"alembic_version"}
    command.upgrade(config, "head")
    assert asyncio.run(_has_review_reason(mysql_url)) is True
    command.check(config)
