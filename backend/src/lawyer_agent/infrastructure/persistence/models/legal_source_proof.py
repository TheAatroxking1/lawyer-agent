"""Immutable provenance for a public legal corpus version."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BINARY, JSON, CheckConstraint, ForeignKeyConstraint, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class LegalVersionSourceProofModel(Base):
    __tablename__ = "legal_version_source_proofs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["version_id"], ["legal_versions.id"], name="fk_legal_source_proofs_version",
        ),
        CheckConstraint(
            "(converter_version IS NULL AND converter_fingerprint IS NULL) OR "
            "(converter_version IS NOT NULL AND converter_fingerprint IS NOT NULL)",
            name="converter_pair",
        ),
        CheckConstraint(
            "recovery_reason IS NULL OR (converter_version IS NOT NULL AND "
            "CAST(converter_version AS BINARY) = CAST('binary-word-static-text-v1' AS BINARY) AND "
            "CAST(recovery_reason AS BINARY) = CAST('office_validation_failed' AS BINARY))",
            name="recovery",
        ),
        CheckConstraint(
            "JSON_TYPE(quality_flags) = 'ARRAY' AND JSON_LENGTH(quality_flags) <= 64",
            name="flags",
        ),
    )

    version_id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    input_ref: Mapped[str] = mapped_column(Text, nullable=False)
    source_sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    input_sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    structure_sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    loader_version: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    converter_version: Mapped[str | None] = mapped_column(String(128))
    converter_fingerprint: Mapped[str | None] = mapped_column(String(64))
    recovery_reason: Mapped[str | None] = mapped_column(String(64))
    quality_flags: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
    )
