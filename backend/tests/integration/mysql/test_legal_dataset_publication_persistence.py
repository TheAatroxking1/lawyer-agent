from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config
from test_legal_corpus_chunks_repositories import _create_database, _drop_database

from alembic import command
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import DatasetSnapshot, DatasetState
from lawyer_agent.domain.legal_dataset_publication import PublicationError, PublicationState
from lawyer_agent.domain.legal_navigation import navigation_index_name
from lawyer_agent.infrastructure.persistence.models.legal_dataset_publication import (
    LegalDatasetPublicationModel,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_dataset_publication import (
    SqlAlchemyDatasetPublicationStore,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def candidate(index="laws-new", alias="laws"):
    return DatasetSnapshot(
        id=new_uuid7(), dataset_name=alias, parser_version="parser-v1",
        state=DatasetState.PENDING,
        manifest={"alias": alias, "index_name": index,
                  "version_ids": [str(new_uuid7())], "model_ref": "synthetic",
                  "dimension": 3, "indexed_documents": 2,
                  "navigation_index": navigation_index_name(index),
                  "navigation_schema_version": 1, "navigation_documents": 2},
        quality_metrics={"indexed_documents": 2},
    )


async def acknowledge(store, row):
    await store.transition(row.id, PublicationState.READY, PublicationState.SWITCHING)
    return await store.transition(
        row.id, PublicationState.SWITCHING, PublicationState.ACKNOWLEDGED,
    )


@pytest.fixture
def publication_mysql_url(mysql_url: URL) -> Iterator[URL]:
    name = f"lawyer_test_{uuid4().hex}"
    asyncio.run(_create_database(mysql_url, name))
    url = mysql_url.set(database=name)
    try:
        command.upgrade(_alembic_config(url), "head")
        yield url
    finally:
        asyncio.run(_drop_database(mysql_url, name))


def test_missing_publication(publication_mysql_url: URL) -> None:
    async def run():
        engine = create_async_engine(publication_mysql_url)
        try:
            store = SqlAlchemyDatasetPublicationStore(async_sessionmaker(engine))
            assert await store.get(new_uuid7()) is None
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_committed_journal_uniqueness_and_cas_race(publication_mysql_url: URL) -> None:
    async def run():
        engine = create_async_engine(publication_mysql_url)
        factory = async_sessionmaker(engine)
        store = SqlAlchemyDatasetPublicationStore(factory)
        try:
            snapshot = candidate()
            row = await store.create(snapshot, "laws-old")
            # A separate session sees the write immediately; default expire_on_commit is safe.
            assert await SqlAlchemyDatasetPublicationStore(factory).get(row.id) == row
            for conflict in (candidate("laws-other"), candidate(alias="other")):
                with pytest.raises(PublicationError, match="publication_conflict"):
                    await store.create(conflict, None)
            outcomes = await asyncio.gather(
                store.transition(row.id, PublicationState.READY, PublicationState.SWITCHING),
                store.transition(row.id, PublicationState.READY, PublicationState.SWITCHING),
                return_exceptions=True,
            )
            assert sum(isinstance(value, PublicationError) for value in outcomes) == 1
            assert (await store.get(row.id)).state == PublicationState.SWITCHING
            with pytest.raises(PublicationError, match="publication_conflict"):
                await store.complete(row.id)
            with pytest.raises(PublicationError, match="publication_invalid_transition"):
                await store.transition(row.id, PublicationState.SWITCHING, PublicationState.READY)
            assert (await store.get(row.id)).candidate == snapshot
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_atomic_complete_retry_and_historical_no_overwrite(
    publication_mysql_url: URL, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run():
        engine = create_async_engine(publication_mysql_url)
        factory = async_sessionmaker(engine)
        store = SqlAlchemyDatasetPublicationStore(factory)
        try:
            row = await store.create(candidate(), "laws-old")
            await acknowledge(store, row)
            original = SqlAlchemyLegalCorpusInventoryRepository.upsert_dataset

            async def fail_after_write(self, snapshot):
                await original(self, snapshot)
                raise RuntimeError("synthetic_snapshot_failure")

            with monkeypatch.context() as patch:
                patch.setattr(SqlAlchemyLegalCorpusInventoryRepository,
                              "upsert_dataset", fail_after_write)
                with pytest.raises(RuntimeError, match="synthetic_snapshot_failure"):
                    await store.complete(row.id)
            assert (await store.get(row.id)).state == PublicationState.ACKNOWLEDGED
            async with factory() as session:
                assert await SqlAlchemyLegalCorpusInventoryRepository(session).find_dataset(
                    "laws",
                ) is None
            with pytest.raises(PublicationError, match="publication_conflict"):
                await store.create(candidate("laws-future"), "laws-new")
            completed = await store.complete(row.id)
            assert completed.state == PublicationState.COMPLETED
            assert completed.candidate.state == DatasetState.PENDING
            async with factory() as session:
                published = await SqlAlchemyLegalCorpusInventoryRepository(session).find_dataset(
                    "laws",
                )
                assert published.state == DatasetState.PUBLISHED
                assert published.released_at.utcoffset().total_seconds() == 0
                snapshot_id = published.id
                model = await session.get(LegalDatasetPublicationModel, row.id)
                assert model.active_alias is None and model.completed_at is not None
            next_row = await store.create(candidate("laws-future"), "laws-new")
            await acknowledge(store, next_row)
            await store.complete(next_row.id)
            assert await store.complete(row.id) == completed
            async with factory() as session:
                latest = await SqlAlchemyLegalCorpusInventoryRepository(session).find_dataset(
                    "laws",
                )
                assert latest.manifest["index_name"] == "laws-future"
                assert latest.id == snapshot_id
                assert len(list(await session.scalars(select(LegalDatasetPublicationModel)))) == 2
            # Completed history permanently reserves physical index names.
            with pytest.raises(PublicationError, match="publication_conflict"):
                await store.create(candidate(), "laws-future")
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_migration_checks_and_history_downgrade_guard(publication_mysql_url: URL) -> None:
    config = _alembic_config(publication_mysql_url)
    command.downgrade(config, "20260906_13")
    command.upgrade(config, "head")
    command.check(config)

    async def run():
        engine = create_async_engine(publication_mysql_url)
        factory = async_sessionmaker(engine)
        store = SqlAlchemyDatasetPublicationStore(factory)
        try:
            row = await store.create(candidate(), None)
            async with factory() as session:
                for changes in (
                    "state='READY'", "state='ready '", "active_alias=NULL",
                    "active_alias='LAWS'", "completed_at=CURRENT_TIMESTAMP(6)",
                    "state='completed', active_alias=NULL",
                    "candidate_json=JSON_ARRAY()",
                ):
                    with pytest.raises(OperationalError) as captured:
                        await session.execute(text(
                            f"UPDATE legal_dataset_publications SET {changes}",  # noqa: S608
                        ))
                    assert captured.value.orig.args[0] == 3819
                    await session.rollback()
            await acknowledge(store, row)
            await store.complete(row.id)
        finally:
            await engine.dispose()
    asyncio.run(run())
    with pytest.raises(RuntimeError, match="publication history"):
        command.downgrade(config, "20260906_13")

    async def preserved():
        engine = create_async_engine(publication_mysql_url)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                    "20260906_14"
                )
                assert await connection.scalar(text(
                    "SELECT COUNT(*) FROM legal_dataset_publications",
                )) == 1
        finally:
            await engine.dispose()
    asyncio.run(preserved())


def test_corrupt_persisted_candidate_has_stable_error(publication_mysql_url: URL) -> None:
    async def run():
        engine = create_async_engine(publication_mysql_url)
        factory = async_sessionmaker(engine)
        store = SqlAlchemyDatasetPublicationStore(factory)
        try:
            row = await store.create(candidate(), None)
            async with factory() as session, session.begin():
                model = await session.get(LegalDatasetPublicationModel, row.id)
                payload = dict(model.candidate_json)
                payload["id"] = ["synthetic_private_payload"]
                await session.execute(update(LegalDatasetPublicationModel).where(
                    LegalDatasetPublicationModel.id == row.id,
                ).values(candidate_json=payload))
            with pytest.raises(PublicationError, match="publication_record_invalid"):
                await store.get(row.id)
            with pytest.raises(PublicationError, match="publication_record_invalid"):
                await store.transition(row.id, PublicationState.READY, PublicationState.SWITCHING)
            async with factory() as session:
                model = await session.get(LegalDatasetPublicationModel, row.id)
                assert model.state == "ready" and model.active_alias == "laws"
        finally:
            await engine.dispose()
    asyncio.run(run())


def test_active_lookup_recovers_committed_id_and_excludes_history(
    publication_mysql_url: URL,
) -> None:
    async def run():
        engine = create_async_engine(publication_mysql_url)
        factory = async_sessionmaker(engine)
        store = SqlAlchemyDatasetPublicationStore(factory)
        try:
            assert await store.find_active("laws") is None
            first = await store.create(candidate(), None)
            # Newly constructed store discovers the id after the creating process has exited.
            assert await SqlAlchemyDatasetPublicationStore(factory).find_active("laws") == first
            assert await store.find_active("other") is None
            for invalid in ("", "LAWS", "laws*", "a" * 65, "laws\n"):
                with pytest.raises(PublicationError, match="publication_invalid_alias"):
                    await store.find_active(invalid)
            await acknowledge(store, first)
            await store.complete(first.id)
            assert await store.find_active("laws") is None
            second = await store.create(candidate("laws-next"), "laws-new")
            assert await store.find_active("laws") == second
            assert (await store.get(first.id)).state == PublicationState.COMPLETED
        finally:
            await engine.dispose()
    asyncio.run(run())
