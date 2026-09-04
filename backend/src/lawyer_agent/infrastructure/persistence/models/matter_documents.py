from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BINARY,
    BigInteger,
    CheckConstraint,
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

_TENANT = "tenants.id"


class TenantMatterModel(TimestampMixin, Base):
    __tablename__ = "tenant_matters"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenant_matters_tenant_id"),
        CheckConstraint(
            "kind IN ('contract_review','litigation','legal_advice','compliance','other')",
            name="ck_tenant_matters_kind",
        ),
        CheckConstraint(
            "status IN ('open','active','closed','archived')",
            name="ck_tenant_matters_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], [_TENANT], name="fk_tenant_matters_tenant_id_tenants"
        ),
        Index("ix_tenant_matters_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    description: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    created_by_membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    owner_membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class TenantMatterPartyModel(TimestampMixin, Base):
    __tablename__ = "tenant_matter_parties"
    __table_args__ = (
        UniqueConstraint("tenant_id", "matter_id", "id", name="uq_matter_parties_tenant_id"),
        ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["tenant_matters.tenant_id", "tenant_matters.id"],
            name="fk_matter_parties_matter",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], [_TENANT], name="fk_matter_parties_tenant_id_tenants"
        ),
        Index("ix_matter_parties_matter_id", "tenant_id", "matter_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    matter_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class TenantDocumentModel(TimestampMixin, Base):
    __tablename__ = "tenant_documents"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenant_documents_tenant_id"),
        UniqueConstraint(
            "tenant_id", "matter_id", "id", name="uq_tenant_documents_matter_id"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "matter_id"],
            ["tenant_matters.tenant_id", "tenant_matters.id"],
            name="fk_tenant_documents_matter",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], [_TENANT], name="fk_tenant_documents_tenant_id_tenants"
        ),
        Index("ix_tenant_documents_matter_id", "tenant_id", "matter_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    matter_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    current_version_no: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class TenantDocumentVersionModel(TimestampMixin, Base):
    __tablename__ = "tenant_document_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "document_id", "version_no",
            name="uq_document_versions_doc_no",
        ),
        CheckConstraint(
            "kind IN ('original','derived')",
            name="ck_document_versions_kind",
        ),
        CheckConstraint(
            "upload_status IN ('uploaded','validating','accepted','needs_review',"
            "'parsing','ready','failed')",
            name="ck_document_versions_upload_status",
        ),
        CheckConstraint(
            "review_status IS NULL OR review_status IN "
            "('draft','pending_review','changes_requested','approved','rejected')",
            name="ck_document_versions_review_status",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["tenant_documents.tenant_id", "tenant_documents.id"],
            name="fk_document_versions_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], [_TENANT], name="fk_document_versions_tenant_id_tenants"
        ),
        Index("ix_document_versions_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    document_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    sha256: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    upload_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="uploaded"
    )
    review_status: Mapped[str | None] = mapped_column(String(24))
    review_reason: Mapped[str | None] = mapped_column(Text)
    file_name: Mapped[str | None] = mapped_column(String(256))
    mime_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    parser_version: Mapped[str | None] = mapped_column(String(64))
    parse_error: Mapped[str | None] = mapped_column(Text)
    created_by_user_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    created_by_membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    uploaded_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)

