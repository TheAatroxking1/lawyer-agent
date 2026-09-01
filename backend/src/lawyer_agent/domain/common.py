import secrets
import time
from typing import cast
from uuid import RFC_4122, UUID


def is_uuid7(value: object) -> bool:
    """Return whether *value* is a non-zero RFC 9562 UUIDv7."""
    return (
        type(value) is UUID
        and value.version == 7
        and value.variant == RFC_4122
        and value.int != 0
    )


def require_uuid7(value: object, *, field: str = "identifier") -> UUID:
    """Validate an identifier at a domain or persistence boundary."""
    if not is_uuid7(value):
        raise ValueError(f"{field} must be an RFC 9562 UUIDv7")
    return cast(UUID, value)


def new_uuid7(*, now_ms: int | None = None) -> UUID:
    timestamp = int(time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    if not 0 <= timestamp < 1 << 48:
        raise ValueError("UUIDv7 timestamp out of range")
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (timestamp << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return UUID(int=value)
