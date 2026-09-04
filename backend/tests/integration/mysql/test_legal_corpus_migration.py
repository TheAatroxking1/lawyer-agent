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

LEGAL_CORPUS_TABLES = {
    "legal_instruments",
    "legal_versions",
    "legal_provisions",
    "legal_chunks",
    "legal_dataset_snapshots",
    "legal_load_batches",
    "legal_quality_issues",
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


async def _insert_corpus_row(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            from uuid import uuid4

            instrument_id = uuid4().bytes
            version_id = uuid4().bytes
            provision_id = uuid4().bytes
            await connection.execute(
                text(
                    "INSERT INTO legal_instruments "
                    "(id, title, issuing_authority, jurisdiction, version) "
                    "VALUES (:id, '中华人民共和国民法典', '全国人民代表大会', 'national', 1)"
                ),
                {"id": instrument_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_versions "
                    "(id, instrument_id, version_label, status, published_on, "
                    " effective_on, content_hash) "
                    "VALUES (:id, :instrument, '2020-05-28 公布版', 'current', "
                    " '2020-05-28', '2021-01-01', :hash)"
                ),
                {"id": version_id, "instrument": instrument_id, "hash": bytes(32)},
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_provisions "
                    "(id, version_id, provision_no, level, full_text, content_hash, "
                    " char_start, char_end) "
                    "VALUES (:id, :version, '第一条', 'article', "
                    " '第一条 为了保护民事主体的合法权益，制定本法。', :hash, 0, 32)"
                ),
                {"id": provision_id, "version": version_id, "hash": bytes(32)},
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_dataset_snapshots "
                    "(id, dataset_name, parser_version, state, manifest_json, "
                    " quality_metrics_json) "
                    "VALUES (:id, 'dataset_v1', 'docx-v1', 'published', '{}', '{}')"
                ),
                {"id": uuid4().bytes},
            )
            await connection.execute(
                text(
                    "INSERT INTO legal_load_batches "
                    "(id, batch_no, source_ref, file_sha256, parser_version, status, "
                    " item_counts_json) "
                    "VALUES (:id, 'B0001', 'object://corpus/0001.docx', :hash, "
                    " 'docx-v1', 'completed', '{\"provisions\": 1}')"
                ),
                {"id": uuid4().bytes, "hash": bytes(32)},
            )
    finally:
        await engine.dispose()


def test_legal_corpus_migration_round_trip_and_row_insert(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    tables = asyncio.run(_table_names(mysql_url))
    assert LEGAL_CORPUS_TABLES <= tables
    asyncio.run(_insert_corpus_row(mysql_url))

    command.downgrade(config, "base")
    assert asyncio.run(_table_names(mysql_url)) == {"alembic_version"}
    command.upgrade(config, "head")
    assert LEGAL_CORPUS_TABLES <= asyncio.run(_table_names(mysql_url))
    command.check(config)
