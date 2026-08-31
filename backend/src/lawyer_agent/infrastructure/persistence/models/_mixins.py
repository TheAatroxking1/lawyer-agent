from datetime import datetime

from sqlalchemy import Integer, text
from sqlalchemy.orm import Mapped, mapped_column

from lawyer_agent.infrastructure.persistence.types import UTC_DATETIME


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTC_DATETIME, nullable=False, server_default=text("CURRENT_TIMESTAMP(6)")
    )


class VersionMixin:
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
