from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BINARY,
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin, VersionMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class ContractReviewRunModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "tenant_contract_reviews"
    __table_args__ = (
        CheckConstraint(
            "status IN ('awaiting_upload','uploaded','running','draft','no_evidence',"
            "'failed','cancelled')",
            name="contract_review_status",
        ),
        CheckConstraint("pdf_size IS NULL OR pdf_size BETWEEN 1 AND 52428800", name="pdf_size"),
        CheckConstraint(
            "(pdf_object_key IS NULL AND pdf_sha256 IS NULL AND pdf_size IS NULL) OR "
            "(pdf_object_key IS NOT NULL AND pdf_sha256 IS NOT NULL AND pdf_size IS NOT NULL)",
            name="pdf_identity",
        ),
        CheckConstraint(
            "(failure_lease_hash IS NULL AND failure_lease_expires_at IS NULL) OR "
            "(failure_lease_hash IS NOT NULL AND failure_lease_expires_at IS NOT NULL)",
            name="failure_lease_identity",
        ),
        UniqueConstraint("tenant_id", "id"),
        UniqueConstraint(
            "tenant_id",
            "created_by_membership_id",
            "idempotency_hash",
            name="uq_contract_review_tenant_member_idempotency",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by_membership_id", "created_by_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_contract_review_owner_membership",
        ),
        Index("ix_contract_review_tenant_created", "tenant_id", "created_at"),
        Index("ix_contract_review_tenant_status", "tenant_id", "status"),
        Index(
            "ix_contract_review_owner_updated",
            "tenant_id", "created_by_user_id", "updated_at", "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    created_by_user_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("users.id"), nullable=False
    )
    created_by_membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    idempotency_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    request_fingerprint: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    instruction: Mapped[str] = mapped_column(Text, nullable=False)
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="awaiting_upload"
    )
    pdf_object_key: Mapped[str | None] = mapped_column(String(1024))
    pdf_sha256: Mapped[bytes | None] = mapped_column(BINARY(32))
    pdf_size: Mapped[int | None] = mapped_column(BigInteger)
    parsed_document: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    evidence_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_lease_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    failure_lease_expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
