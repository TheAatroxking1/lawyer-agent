from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

_TEST_DATABASE_PATTERN = re.compile(r"^lawyer_test_[a-f0-9]{32}$")


def _compose_mysql_password() -> str:
    env_path = Path(__file__).parents[3] / "deploy" / ".env"
    if not env_path.is_file():
        pytest.skip("deploy/.env is required unless LAWYER_TEST_MYSQL_ADMIN_URL is set")
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        key, separator, value = raw_line.partition("=")
        if separator and key.strip() == "MYSQL_PASSWORD":
            return f"{value.strip()}-root"
    pytest.skip("MYSQL_PASSWORD is missing from deploy/.env")


def _admin_url() -> URL:
    configured = os.getenv("LAWYER_TEST_MYSQL_ADMIN_URL")
    if configured:
        url = make_url(configured)
    else:
        url = URL.create(
            "mysql+asyncmy",
            username="root",
            password=_compose_mysql_password(),
            host="127.0.0.1",
            port=13306,
            database="mysql",
        )
    if url.drivername != "mysql+asyncmy":
        raise RuntimeError("LAWYER_TEST_MYSQL_ADMIN_URL must use mysql+asyncmy")
    return url


async def _execute_admin(statement: str) -> None:
    engine = create_async_engine(_admin_url(), pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def mysql_url() -> Iterator[URL]:
    """Disposable MySQL database shared by every integration test directory."""
    database_name = f"lawyer_test_{uuid4().hex}"
    if not _TEST_DATABASE_PATTERN.fullmatch(database_name):
        raise RuntimeError("refusing to manage an unexpected database name")

    asyncio.run(
        _execute_admin(
            f"CREATE DATABASE `{database_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    )
    try:
        yield _admin_url().set(database=database_name)
    finally:
        asyncio.run(_execute_admin(f"DROP DATABASE `{database_name}`"))
