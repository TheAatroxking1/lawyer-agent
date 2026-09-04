"""Add the phase 2 rule pack schema.

Revision ID: 20260905_09
Revises: 20260905_08
Create Date: 2026-09-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

revision: str = "20260905_09"
down_revision: str | Sequence[str] | None = "20260905_08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
_DATETIME = mysql.DATETIME(fsp=6)
_ID = sa.BINARY(16)


def _id(name: str) -> sa.Column:
    return sa.Column(name, _ID, nullable=False)


def upgrade() -> None:
    op.create_table(
        "tenant_rule_packs",
        _id("id"),
        _id("tenant_id"),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_by_user_id", _ID, nullable=True),
        sa.Column("created_by_membership_id", _ID, nullable=True),
        sa.Column("activated_at", _DATETIME, nullable=True),
        sa.Column("deactivated_at", _DATETIME, nullable=True),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_rule_packs"),
        sa.UniqueConstraint("tenant_id", "id", name="uq_tenant_rule_packs_tenant_id"),
        sa.UniqueConstraint(
            "tenant_id", "name", "version", name="uq_tenant_rule_packs_name_version"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_tenant_rule_packs_tenant_id_tenants"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_tenant_rule_packs_tenant", "tenant_rule_packs", ["tenant_id"])

    op.create_table(
        "tenant_rule_pack_rules",
        _id("id"),
        _id("tenant_id"),
        _id("pack_id"),
        sa.Column("trigger_kind", sa.String(16), nullable=False),
        sa.Column("label", sa.String(256), nullable=False),
        sa.Column("pattern", sa.String(4096), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("suggestion_template", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_rule_pack_rules"),
        sa.UniqueConstraint(
            "tenant_id", "pack_id", "id", name="uq_rules_pack_tenant_id"
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('clause_type','risk_phrase')",
            name="ck_rules_trigger_kind",
        ),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high')",
            name="ck_rules_risk_level",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pack_id"],
            ["tenant_rule_packs.tenant_id", "tenant_rule_packs.id"],
            name="fk_rules_pack",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_rules_tenant_id_tenants"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index("ix_rules_pack_id", "tenant_rule_pack_rules", ["tenant_id", "pack_id"])

    op.create_table(
        "tenant_contract_risk_issues",
        _id("id"),
        _id("tenant_id"),
        _id("document_id"),
        sa.Column("rule_id", _ID, nullable=True),
        _id("pack_id"),
        sa.Column("pack_version", sa.Integer(), nullable=False),
        sa.Column("provision_no", sa.String(128), nullable=False),
        sa.Column("matched_text", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column(
            "evidence_level", sa.String(16), server_default="rule_based", nullable=False
        ),
        sa.Column("disposition_reason", sa.Text(), nullable=True),
        sa.Column("raised_at", _DATETIME, nullable=True),
        sa.Column("disposed_at", _DATETIME, nullable=True),
        sa.Column(
            "created_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.Column(
            "updated_at", _DATETIME, server_default=sa.text("CURRENT_TIMESTAMP(6)"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tenant_contract_risk_issues"),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "id", name="uq_risk_issues_tenant_id"
        ),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high')",
            name="ck_risk_issues_risk_level",
        ),
        sa.CheckConstraint(
            "status IN ('open','accepted','rejected','modified','stale')",
            name="ck_risk_issues_status",
        ),
        sa.CheckConstraint(
            "evidence_level = 'rule_based'",
            name="ck_risk_issues_evidence_level",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "pack_id"],
            ["tenant_rule_packs.tenant_id", "tenant_rule_packs.id"],
            name="fk_risk_issues_pack",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["tenant_documents.tenant_id", "tenant_documents.id"],
            name="fk_risk_issues_document",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], name="fk_risk_issues_tenant_id_tenants"
        ),
        **_TABLE_OPTIONS,
    )
    op.create_index(
        "ix_risk_issues_document_id", "tenant_contract_risk_issues", ["tenant_id", "document_id"]
    )


def downgrade() -> None:
    op.drop_table("tenant_contract_risk_issues")
    op.drop_table("tenant_rule_pack_rules")
    op.drop_table("tenant_rule_packs")
