from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BINARY,
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.models._mixins import TimestampMixin, VersionMixin
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class UserModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','locked','disabled','pending_deletion')",
            name="user_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    deleted_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)


class AuthIdentityModel(TimestampMixin, Base):
    __tablename__ = "auth_identities"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('username','phone','email','wechat_unionid','wechat_openid')",
            name="auth_identity_kind",
        ),
        CheckConstraint("status IN ('active','revoked')", name="auth_identity_status"),
        UniqueConstraint(
            "kind",
            "issuer",
            "blind_index_key_version",
            "subject_blind_index",
            name="uq_auth_identities_subject",
        ),
        Index("ix_auth_identities_user_id", "user_id"),
        Index("ix_auth_identities_blind_key_version", "blind_index_key_version"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    display_value: Mapped[str | None] = mapped_column(String(255))
    subject_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    subject_blind_index: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    blind_index_key_version: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    verified_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")


class IdentityVerificationChallengeModel(TimestampMixin, Base):
    __tablename__ = "identity_verification_challenges"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('phone','email','wechat_unionid','wechat_openid')",
            name="identity_challenge_kind",
        ),
        CheckConstraint(
            "status IN ('pending','consumed','expired','locked')",
            name="identity_challenge_status",
        ),
        Index("ix_identity_verification_challenges_target", "kind", "target_blind_index"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    user_id: Mapped[UUID | None] = mapped_column(UuidBinary(), ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    issuer: Mapped[str] = mapped_column(String(255), nullable=False)
    target_blind_index: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    challenge_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    rate_limit_context_hash: Mapped[bytes] = mapped_column(BINARY(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")


class PasswordCredentialModel(VersionMixin, TimestampMixin, Base):
    __tablename__ = "password_credentials"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active','locked','revoked')", name="password_credential_status"
        ),
    )

    user_id: Mapped[UUID] = mapped_column(UuidBinary(), ForeignKey("users.id"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False, server_default="argon2id")
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    password_changed_at: Mapped[datetime] = mapped_column(UTC_DATETIME, nullable=False)
    failed_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    locked_until: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
