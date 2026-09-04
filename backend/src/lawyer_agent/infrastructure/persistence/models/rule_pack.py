from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
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


class TenantRulePackModel(TimestampMixin, Base):
    __tablename__ = "tenant_rule_packs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_tenant_rule_packs_tenant_id"),
        UniqueConstraint(
            "tenant_id", "name", "version", name="uq_tenant_rule_packs_name_version"
        ),
        ForeignKeyConstraint(
            ["tenant_id"], [_TENANT], name="fk_tenant_rule_packs_tenant_id_tenants"
        ),
        Index("ix_tenant_rule_packs_tenant", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default=text("0")
    )
    created_by_user_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    created_by_membership_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    activated_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    deactivated_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class TenantRulePackRuleModel(TimestampMixin, Base):
    __tablename__ = "tenant_rule_pack_rules"
    __table_args__ = (
        UniqueConstraint("tenant_id", "pack_id", "id", name="uq_rules_pack_tenant_id"),
        CheckConstraint(
            "trigger_kind IN ('clause_type','risk_phrase')",
            name="ck_rules_trigger_kind",
        ),
        CheckConstraint(
            "risk_level IN ('low','medium','high')",
            name="ck_rules_risk_level",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "pack_id"],
            ["tenant_rule_packs.tenant_id", "tenant_rule_packs.id"],
            name="fk_rules_pack",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], [_TENANT], name="fk_rules_tenant_id_tenants"
        ),
        Index("ix_rules_pack_id", "tenant_id", "pack_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    pack_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    trigger_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    label: Mapped[str] = mapped_column(String(256), nullable=False)
    pattern: Mapped[str] = mapped_column(String(4096), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    suggestion_template: Mapped[str] = mapped_column(Text, nullable=False)
    enabled: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, server_default=text("1")
    )


class TenantContractRiskIssueModel(TimestampMixin, Base):
    __tablename__ = "tenant_contract_risk_issues"
    __table_args__ = (
        UniqueConstraint("tenant_id", "document_id", "id", name="uq_risk_issues_tenant_id"),
        CheckConstraint(
            "risk_level IN ('low','medium','high')",
            name="ck_risk_issues_risk_level",
        ),
        CheckConstraint(
            "status IN ('open','accepted','rejected','modified','stale')",
            name="ck_risk_issues_status",
        ),
        CheckConstraint(
            "evidence_level = 'rule_based'",
            name="ck_risk_issues_evidence_level",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "pack_id"],
            ["tenant_rule_packs.tenant_id", "tenant_rule_packs.id"],
            name="fk_risk_issues_pack",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["tenant_documents.tenant_id", "tenant_documents.id"],
            name="fk_risk_issues_document",
        ),
        ForeignKeyConstraint(
            ["tenant_id"], [_TENANT], name="fk_risk_issues_tenant_id_tenants"
        ),
        Index("ix_risk_issues_document_id", "tenant_id", "document_id"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    document_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    rule_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    pack_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    pack_version: Mapped[int] = mapped_column(Integer, nullable=False)
    provision_no: Mapped[str] = mapped_column(String(128), nullable=False)
    matched_text: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    evidence_level: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="rule_based"
    )
    disposition_reason: Mapped[str | None] = mapped_column(Text)
    raised_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    disposed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
