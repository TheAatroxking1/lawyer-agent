from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.infrastructure.persistence.models import MessageSecurityRejectionModel
from lawyer_agent.infrastructure.persistence.repositories.message_security import (
    MessageSecurityRejectionRepository,
    RejectionCapacity,
    RejectionWriteResult,
    SecurityRejection,
    observability_ref,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_KEY = b"k" * 32


def test_observability_ref_is_deterministic_and_key_bound() -> None:
    first = observability_ref(_KEY, b"raw-message")
    second = observability_ref(_KEY, b"raw-message")
    assert first == second
    assert first != observability_ref(b"x" * 32, b"raw-message")
    assert len(first) == 32
    with pytest.raises(ValueError, match="32 bytes"):
        observability_ref(b"short", b"raw-message")


async def _run_rejection_checks(mysql_url: str) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    now = datetime.now(UTC)
    try:
        async with factory() as session:
            repo = MessageSecurityRejectionRepository(session)
            capacity = RejectionCapacity(max_rows=1000, max_rows_per_day=1000, sample_rate=100)
            ref = observability_ref(_KEY, b"bad-message")
            rejection = SecurityRejection(
                observability_ref=ref,
                rejection_code="invalid_signature",
                source_channel="main",
                received_at=now,
                schema_version_hint=1,
                kid_hint="k2026",
            )
            assert (
                await repo.record(rejection, capacity=capacity)
                is RejectionWriteResult.PERSISTED
            )
            assert (
                await repo.record(rejection, capacity=capacity)
                is RejectionWriteResult.DEDUPLICATED
            )
            await session.commit()

        async with factory() as session:
            row = await session.scalar(
                select(MessageSecurityRejectionModel).where(
                    MessageSecurityRejectionModel.observability_ref == ref
                )
            )
            assert row is not None
            assert row.schema_version_hint == 1
            assert row.kid_hint == "k2026"

        # Invalid hints are sanitized to NULL.
        async with factory() as session:
            repo = MessageSecurityRejectionRepository(session)
            capacity = RejectionCapacity(max_rows=1000, max_rows_per_day=1000, sample_rate=100)
            bad_ref = observability_ref(_KEY, b"bad-hints")
            await repo.record(
                SecurityRejection(
                    observability_ref=bad_ref,
                    rejection_code="invalid_schema",
                    source_channel="main",
                    received_at=now,
                    schema_version_hint=-1,
                    kid_hint="!!!bad!!!",
                ),
                capacity=capacity,
            )
            await session.commit()
        async with factory() as session:
            row = await session.scalar(
                select(MessageSecurityRejectionModel).where(
                    MessageSecurityRejectionModel.observability_ref == bad_ref
                )
            )
            assert row is not None
            assert row.schema_version_hint is None
            assert row.kid_hint is None

        # Capacity ceiling drops writes once the global row cap is reached.
        async with factory() as session:
            repo = MessageSecurityRejectionRepository(session)
            current = await session.scalar(
                select(func.count()).select_from(MessageSecurityRejectionModel)
            )
            assert current is not None
            full = RejectionCapacity(
                max_rows=current,
                max_rows_per_day=current + 1,
                sample_rate=100,
            )
            assert (
                await repo.record(
                    SecurityRejection(
                        observability_ref=observability_ref(_KEY, b"cap-drop"),
                        rejection_code="invalid_signature",
                        source_channel="main",
                        received_at=now,
                    ),
                    capacity=full,
                )
                is RejectionWriteResult.DROPPED_CAPACITY
            )

        # Deterministic sampling returns SAMPLED for a ref outside the sample window.
        async with factory() as session:
            repo = MessageSecurityRejectionRepository(session)
            capacity = RejectionCapacity(max_rows=1000, max_rows_per_day=1000, sample_rate=1)
            assert (
                await repo.record(
                    SecurityRejection(
                        observability_ref=b"\x02" + b"\x00" * 31,
                        rejection_code="invalid_signature",
                        source_channel="main",
                        received_at=now,
                    ),
                    capacity=capacity,
                )
                is RejectionWriteResult.SAMPLED
            )

        # Expiry cleanup removes old rows within a bounded batch.
        async with factory() as session:
            repo = MessageSecurityRejectionRepository(session)
            old_ref = observability_ref(_KEY, b"old-message")
            await repo.record(
                SecurityRejection(
                    observability_ref=old_ref,
                    rejection_code="invalid_signature",
                    source_channel="main",
                    received_at=now - timedelta(days=30),
                ),
                capacity=RejectionCapacity(max_rows=1000, max_rows_per_day=1000, sample_rate=100),
            )
            await session.commit()
            deleted = await repo.delete_expired(
                before=now - timedelta(days=7),
                limit=1000,
            )
            assert deleted >= 1
            await session.commit()
    finally:
        await engine.dispose()


def test_security_rejection_telemetry_bounds_and_sanitizes(mysql_url) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_rejection_checks(mysql_url.render_as_string(hide_password=False)))
