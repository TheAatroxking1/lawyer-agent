from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

MATTER_TABLES = {
    "tenant_matters",
    "tenant_matter_parties",
    "tenant_documents",
    "tenant_document_versions",
}


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


def test_phase2_matter_documents_migration_round_trip(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    tables = asyncio.run(_table_names(mysql_url))
    assert MATTER_TABLES <= tables

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(mysql_url)) == {"alembic_version"}
    command.upgrade(config, "head")
    assert MATTER_TABLES <= asyncio.run(_table_names(mysql_url))
    command.check(config)
