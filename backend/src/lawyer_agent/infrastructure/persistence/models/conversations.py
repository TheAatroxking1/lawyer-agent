from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BINARY,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class RequestUuidBinary(UuidBinary):
    """Client idempotency UUIDs may be UUID4; resource identifiers remain UUID7."""

    cache_ok = True

    def process_bind_param(self, value: UUID | None, dialect: Dialect) -> bytes | None:
        if value is not None and not isinstance(value, UUID):
            raise ValueError("request_id must be UUID")
        return None if value is None else value.bytes


class ConversationModel(TimestampMixin, Base):
    __tablename__ = "tenant_conversations"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", "creator_user_id", name="uq_conversation_owner"),
        ForeignKeyConstraint(
            ["tenant_id", "contract_review_id"],
            ["tenant_contract_reviews.tenant_id", "tenant_contract_reviews.id"],
            name="fk_conversation_contract_review",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "creator_membership_id", "creator_user_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id", "tenant_memberships.user_id"],
            name="fk_conversation_owner_membership",
        ),
        CheckConstraint(
            "(active_request_id IS NULL AND active_token_hash IS NULL "
            "AND active_expires_at IS NULL) OR (active_request_id IS NOT NULL "
            "AND active_token_hash IS NOT NULL AND active_expires_at IS NOT NULL)",
            name="conversation_lease",
        ),
        Index("ix_conversation_owner_updated", "tenant_id", "creator_user_id", "updated_at", "id"),
        Index("ix_conversation_contract_review", "tenant_id", "contract_review_id"),
    )
    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("tenants.id"), nullable=False)
    creator_user_id: Mapped[UUID] = mapped_column(
        UuidBinary(), ForeignKey("users.id"), nullable=False
    )
    creator_membership_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    contract_review_id: Mapped[UUID | None] = mapped_column(UuidBinary())
    active_request_id: Mapped[UUID | None] = mapped_column(RequestUuidBinary())
    active_token_hash: Mapped[bytes | None] = mapped_column(BINARY(32))
    active_expires_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class ConversationMessageModel(TimestampMixin, Base):
    __tablename__ = "tenant_conversation_messages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id", "creator_user_id"],
            [
                "tenant_conversations.tenant_id",
                "tenant_conversations.id",
                "tenant_conversations.creator_user_id",
            ],
            name="fk_conversation_message_owner",
        ),
        UniqueConstraint(
            "tenant_id",
            "conversation_id",
            "request_id",
            "role",
            name="uq_conversation_request_role",
        ),
        UniqueConstraint(
            "tenant_id", "conversation_id", "sequence", name="uq_conversation_message_sequence"
        ),
        CheckConstraint("role IN ('user','assistant')", name="conversation_message_role"),
        CheckConstraint(
            "status IN ('streaming','completed','incomplete')", name="conversation_message_status"
        ),
        Index("ix_conversation_message_order", "tenant_id", "conversation_id", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    conversation_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    creator_user_id: Mapped[UUID] = mapped_column(UuidBinary(), nullable=False)
    request_id: Mapped[UUID] = mapped_column(RequestUuidBinary(), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(
        Text().with_variant(mysql.MEDIUMTEXT(), "mysql"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
