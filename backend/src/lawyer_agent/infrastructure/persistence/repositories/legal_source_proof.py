"""Insert-only source proof repository; transaction ownership stays with the caller."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.legal_source_proof import LegalSourceProof
from lawyer_agent.infrastructure.persistence.models.legal_source_proof import (
    LegalVersionSourceProofModel,
)


class SqlAlchemyLegalSourceProofRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_for_version(self, version_id: UUID) -> LegalSourceProof | None:
        row = await self._session.scalar(
            select(LegalVersionSourceProofModel).where(
                LegalVersionSourceProofModel.version_id == version_id,
            )
        )
        if row is None:
            return None
        return LegalSourceProof(
            source_ref=row.source_ref,
            input_ref=row.input_ref,
            source_sha256=row.source_sha256,
            input_sha256=row.input_sha256,
            structure_sha256=row.structure_sha256,
            loader_version=row.loader_version,
            parser_version=row.parser_version,
            converter_version=row.converter_version,
            converter_fingerprint=row.converter_fingerprint,
            recovery_reason=row.recovery_reason,
            quality_flags=tuple(row.quality_flags),
        )

    async def create_for_version(self, version_id: UUID, proof: LegalSourceProof) -> None:
        if not isinstance(proof, LegalSourceProof):
            raise ValueError("source_proof_must_be_strongly_typed")
        self._session.add(
            LegalVersionSourceProofModel(
                version_id=version_id,
                source_ref=proof.source_ref,
                input_ref=proof.input_ref,
                source_sha256=proof.source_sha256,
                input_sha256=proof.input_sha256,
                structure_sha256=proof.structure_sha256,
                loader_version=proof.loader_version,
                parser_version=proof.parser_version,
                converter_version=proof.converter_version,
                converter_fingerprint=proof.converter_fingerprint,
                recovery_reason=proof.recovery_reason,
                quality_flags=list(proof.quality_flags),
            )
        )
        await self._session.flush()
