from uuid import UUID

from sqlalchemy import BINARY
from sqlalchemy.dialects import mysql
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import TypeDecorator

from lawyer_agent.domain.common import require_uuid7

UTC_DATETIME = mysql.DATETIME(fsp=6)


class UuidBinary(TypeDecorator[UUID]):
    impl = BINARY(16)
    cache_ok = True

    def process_bind_param(self, value: UUID | None, dialect: Dialect) -> bytes | None:
        return None if value is None else require_uuid7(value).bytes

    def process_result_value(self, value: bytes | None, dialect: Dialect) -> UUID | None:
        return None if value is None else UUID(bytes=value)
