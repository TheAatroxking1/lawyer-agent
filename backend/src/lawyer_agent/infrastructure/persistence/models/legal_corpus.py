from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BINARY,
    JSON,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class LegalInstrumentModel(TimestampMixin, Base):
    __tablename__ = "legal_instruments"
    __table_args__ = (
        UniqueConstraint("title", "jurisdiction", name="uq_legal_instruments_title_jur"),
        CheckConstraint(
            "CAST(category AS BINARY) IN ("
            "CAST('constitution' AS BINARY),CAST('law' AS BINARY),"
            "CAST('administrative_regulation' AS BINARY),"
            "CAST('judicial_interpretation' AS BINARY),"
            "CAST('local_regulation' AS BINARY),"
            "CAST('supervisory_regulation' AS BINARY),"
            "CAST('unknown' AS BINARY))",
            name="ck_legal_instruments_category",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    issuing_authority: Mapped[str] = mapped_column(String(256), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(64), nullable=False)
    region_code: Mapped[str | None] = mapped_column(String(16))
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="unknown"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class LegalVersionModel(TimestampMixin, Base):
    __tablename__ = "legal_versions"
    __table_args__ = (
        UniqueConstraint("instrument_id", "version_label", name="uq_legal_versions_inst_label"),
        CheckConstraint(
            "status IN ('current','repealed','status_unknown','historical','draft')",
            name="ck_legal_versions_status",
        ),
        ForeignKeyConstraint(
            ["instrument_id"],
            ["legal_instruments.id"],
            name="fk_legal_versions_instrument_id_legal_instruments",
        ),
        Index("ix_legal_versions_instrument_id", "instrument_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    instrument_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    version_label: Mapped[str] = mapped_column(String(128), nullable=False)
    law_number: Mapped[str | None] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="status_unknown")
    published_on: Mapped[date | None] = mapped_column(Date)
    effective_on: Mapped[date | None] = mapped_column(Date)
    repealed_on: Mapped[date | None] = mapped_column(Date)
    content_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    source_ref: Mapped[str | None] = mapped_column(String(512))
    dataset_version: Mapped[str | None] = mapped_column(String(64))
    parser_version: Mapped[str | None] = mapped_column(String(64))


class LegalProvisionModel(TimestampMixin, Base):
    __tablename__ = "legal_provisions"
    __table_args__ = (
        UniqueConstraint("version_id", "provision_no", name="uq_legal_provisions_ver_no"),
        CheckConstraint(
            "level IN ('part','chapter','section','article','paragraph','item','sub_item')",
            name="ck_legal_provisions_level",
        ),
        ForeignKeyConstraint(
            ["version_id"],
            ["legal_versions.id"],
            name="fk_legal_provisions_version_id_legal_versions",
        ),
        Index("ix_legal_provisions_version_id", "version_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provision_no: Mapped[str] = mapped_column(String(64), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    structure_path_json: Mapped[list[str] | None] = mapped_column(JSON)
    title: Mapped[str | None] = mapped_column(String(512))
    full_text: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    char_start: Mapped[int] = mapped_column(Integer, nullable=False)
    char_end: Mapped[int] = mapped_column(Integer, nullable=False)


class LegalChunkModel(TimestampMixin, Base):
    __tablename__ = "legal_chunks"
    __table_args__ = (
        CheckConstraint(
            "(parent_relative_char_start IS NULL AND parent_relative_char_end IS NULL) OR "
            "(parent_relative_char_start IS NOT NULL AND parent_relative_char_end IS NOT NULL "
            "AND parent_relative_char_start >= 0 "
            "AND parent_relative_char_end > parent_relative_char_start)", name="span",
        ),
        CheckConstraint(
            "quality IN ('ok','degraded','failed')",
            name="ck_legal_chunks_quality",
        ),
        ForeignKeyConstraint(
            ["version_id"], ["legal_versions.id"], name="fk_legal_chunks_version_id_legal_versions"
        ),
        ForeignKeyConstraint(
            ["provision_id"],
            ["legal_provisions.id"],
            name="fk_legal_chunks_provision_id_legal_provisions",
        ),
        ForeignKeyConstraint(
            ["parent_chunk_id"], ["legal_chunks.id"], name="fk_legal_chunks_parent_id_legal_chunks"
        ),
        Index("ix_legal_chunks_version_id", "version_id"),
        Index("ix_legal_chunks_provision_id", "provision_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    version_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    provision_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    parent_chunk_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    chunk_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="provision")
    quality: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ok")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    parent_relative_char_start: Mapped[int | None] = mapped_column(Integer)
    parent_relative_char_end: Mapped[int | None] = mapped_column(Integer)


class LegalDatasetSnapshotModel(TimestampMixin, Base):
    __tablename__ = "legal_dataset_snapshots"
    __table_args__ = (
        UniqueConstraint("dataset_name", name="uq_legal_dataset_snapshots_name"),
        CheckConstraint(
            "state IN ('pending','published','superseded','rejected')",
            name="ck_legal_dataset_snapshots_state",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    dataset_name: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    quality_metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class LegalLoadBatchModel(TimestampMixin, Base):
    __tablename__ = "legal_load_batches"
    __table_args__ = (
        UniqueConstraint("file_sha256", name="uq_legal_load_batches_file_sha256"),
        UniqueConstraint("batch_no", name="uq_legal_load_batches_batch_no"),
        CheckConstraint(
            "status IN ('inventoried','parsing','completed','failed')",
            name="ck_legal_load_batches_status",
        ),
        CheckConstraint(
            "item_counts_json IS NOT NULL",
            name="ck_legal_load_batches_counts",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    file_sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="inventoried")
    item_counts_json: Mapped[dict[str, int]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    completed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    error_message: Mapped[str | None] = mapped_column(String(2048))


class LegalQualityIssueModel(TimestampMixin, Base):
    __tablename__ = "legal_quality_issues"
    __table_args__ = (
        ForeignKeyConstraint(
            ["batch_id"], ["legal_load_batches.id"], name="fk_legal_quality_issues_batch_id"
        ),
        Index("ix_legal_quality_issues_batch_id", "batch_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    batch_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    file_sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    issue_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(2048), nullable=False)
