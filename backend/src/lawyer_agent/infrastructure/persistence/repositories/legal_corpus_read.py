"""Query composition for complete snapshots with bounded JSON transport."""

from sqlalchemy import select

from lawyer_agent.domain.legal_corpus import DatasetSnapshot
from lawyer_agent.infrastructure.persistence.models.legal_corpus import LegalDatasetSnapshotModel
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus import (
    SqlAlchemyLegalCorpusRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.legal_corpus_inventory import (
    SqlAlchemyLegalCorpusInventoryRepository,
)


class SqlAlchemyLegalCorpusReadRepository(SqlAlchemyLegalCorpusRepository):
    """Keep corpus fact reads shared; route snapshot reads through the JSON reader."""

    async def dataset_snapshot_by_name(self, dataset_name: str) -> DatasetSnapshot | None:
        return await SqlAlchemyLegalCorpusInventoryRepository(self._session).find_dataset(
            dataset_name
        )

    async def dataset_snapshots(self) -> tuple[DatasetSnapshot, ...]:
        # Sort small identities rather than multi-megabyte JSON values in MySQL.
        names = tuple(
            await self._session.scalars(
                select(LegalDatasetSnapshotModel.dataset_name).order_by(
                    LegalDatasetSnapshotModel.released_at.is_not(None).desc(),
                    LegalDatasetSnapshotModel.released_at.desc(),
                    LegalDatasetSnapshotModel.id.desc(),
                )
            )
        )
        snapshots = []
        for name in names:
            snapshot = await self.dataset_snapshot_by_name(name)
            if snapshot is None:
                raise ValueError("dataset_snapshot_changed")
            snapshots.append(snapshot)
        return tuple(snapshots)
