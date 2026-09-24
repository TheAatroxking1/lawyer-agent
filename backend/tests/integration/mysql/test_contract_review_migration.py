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


def _config(mysql_url: URL) -> Config:
    backend = Path(__file__).parents[3]
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("script_location", str(backend / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        mysql_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


async def _has_table(mysql_url: URL) -> bool:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(
                lambda sync: "tenant_contract_reviews" in inspect(sync).get_table_names()
            )
    finally:
        await engine.dispose()


def test_contract_review_migration_round_trip(mysql_url: URL) -> None:
    config = _config(mysql_url)
    command.upgrade(config, "20260906_15")
    assert not asyncio.run(_has_table(mysql_url))
    command.upgrade(config, "20260916_16")
    assert asyncio.run(_has_table(mysql_url))
    command.downgrade(config, "20260906_15")
    assert not asyncio.run(_has_table(mysql_url))
    command.upgrade(config, "head")
