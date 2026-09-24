"""Durable public-corpus publication journal; unresolved aliases stay occupied."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.base import Base
from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME, UuidBinary


class LegalDatasetPublicationModel(Base):
    __tablename__ = "legal_dataset_publications"
    __table_args__ = (
        UniqueConstraint("active_alias"),
        UniqueConstraint("index_name"),
        CheckConstraint(
            "CAST(state AS BINARY) IN ('ready','switching','acknowledged','completed')",
            name="state",
        ),
        CheckConstraint(
            "(CAST(state AS BINARY) = 'completed' AND active_alias IS NULL "
            "AND completed_at IS NOT NULL) OR "
            "(CAST(state AS BINARY) <> 'completed' AND active_alias IS NOT NULL "
            "AND CAST(active_alias AS BINARY) = CAST(alias AS BINARY) "
            "AND completed_at IS NULL)",
            name="active",
        ),
        CheckConstraint("JSON_TYPE(candidate_json) = 'OBJECT'", name="candidate"),
    )

    id: Mapped[UUID] = mapped_column(UuidBinary(), primary_key=True)
    active_alias: Mapped[str | None] = mapped_column(String(64))
    alias: Mapped[str] = mapped_column(String(64), nullable=False)
    index_name: Mapped[str] = mapped_column(String(255), nullable=False)
    previous_target: Mapped[str | None] = mapped_column(String(255))
    candidate_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)"),
    )
    completed_at: Mapped[datetime | None] = mapped_column(UTC_DATETIME)
