from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest
from sqlalchemy import JSON, cast, column, func, select, table, text
from sqlalchemy.dialects.mysql import BINARY
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from lawyer_agent.infrastructure.persistence.json_documents import (
    _parts_statement,
    read_json_document,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


@pytest.mark.parametrize("size", [1024 * 1024, 20 * 1024 * 1024])
@pytest.mark.parametrize("loop_kind", ["default", "selector"])
def test_real_mysql_large_json_both_loops(mysql_url: URL, size: int, loop_kind: str) -> None:
    if loop_kind == "selector" and sys.platform != "win32":
        pytest.skip("Windows selector comparison")

    async def probe() -> None:
        engine = create_async_engine(mysql_url, connect_args={"read_timeout": 20})
        try:
            async with AsyncSession(engine) as session:
                await session.execute(text(
                    "CREATE TEMPORARY TABLE json_read_probe (payload JSON NOT NULL)"
                ))
                await session.execute(text(
                    "INSERT INTO json_read_probe VALUES (JSON_OBJECT('body', 'plan'))"
                ))
                source = table("json_read_probe", column("payload", JSON))
                statement = select(source.c.payload)
                compiled = _parts_statement(statement).compile(
                    dialect=engine.dialect, compile_kwargs={"literal_binds": True},
                )
                # EXPLAIN warnings can embed the complete JSON, so inspect the plan
                # on small content before the large-payload driver regression check.
                plan = await session.scalar(text("EXPLAIN FORMAT=JSON " + str(compiled)))
                assert 'materialized_from_subquery' in plan
                await session.execute(text(
                    "UPDATE json_read_probe SET payload = JSON_OBJECT('body', REPEAT('x', :size))"
                ), {"size": size})
                digest = await session.scalar(
                    select(func.sha2(cast(source.c.payload, BINARY), 256))
                )
                started = time.perf_counter()
                document = await read_json_document(
                    session, statement, expected_sha256=digest,
                )
                assert document == {"body": "x" * size}
                assert await session.scalar(select(1)) == 1
                print(json.dumps({"loop": type(asyncio.get_running_loop()).__name__,
                                  "body_bytes": size,
                                  "seconds": round(time.perf_counter() - started, 3)}))
        finally:
            await engine.dispose()

    factory = asyncio.SelectorEventLoop if loop_kind == "selector" else None
    with asyncio.Runner(loop_factory=factory) as runner:
        runner.run(asyncio.wait_for(probe(), timeout=45))
