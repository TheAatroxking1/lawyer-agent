import sys

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL

from lawyer_agent.infrastructure.persistence.engine import create_engine_from_url


@pytest.mark.mysql
@pytest.mark.integration
async def test_large_utf8_payload_roundtrip_and_rollback(mysql_url: URL) -> None:
    engine = create_engine_from_url(mysql_url.render_as_string(hide_password=False))
    if sys.platform == "win32":
        assert engine.dialect.driver == "aiomysql"
    try:
        binary_id = bytes(range(32))
        for size in (300 * 1024, 1024 * 1024, 2 * 1024 * 1024):
            payload = "合同正文🙂" * (size // len("合同正文🙂".encode()) + 1)
            async with engine.connect() as connection:
                timezone = await connection.execute(text("SELECT @@session.time_zone"))
                assert timezone.scalar_one() == "+00:00"
                await connection.execute(text(
                    "CREATE TEMPORARY TABLE driver_payload_test "
                    "(id VARBINARY(32) PRIMARY KEY, body LONGTEXT CHARACTER SET utf8mb4) "
                    "ENGINE=InnoDB"
                ))
                try:
                    await connection.execute(
                        text("INSERT INTO driver_payload_test VALUES (:id, :body)"),
                        {"id": binary_id, "body": payload},
                    )
                    await connection.commit()
                    for _ in range(3):
                        actual = (await connection.execute(
                            text("SELECT body FROM driver_payload_test WHERE id=:id"),
                            {"id": binary_id},
                        )).scalar_one()
                        assert actual == payload
                    await connection.execute(
                        text("DELETE FROM driver_payload_test WHERE id=:id"), {"id": binary_id}
                    )
                    await connection.rollback()
                    assert (await connection.execute(
                        text("SELECT body FROM driver_payload_test WHERE id=:id"),
                        {"id": binary_id},
                    )).scalar_one() == payload
                finally:
                    await connection.execute(text("DROP TEMPORARY TABLE driver_payload_test"))
                    await connection.commit()
            # Exercise fresh transports as well as repeated reads on one transport.
            await engine.dispose()
    finally:
        await engine.dispose()
