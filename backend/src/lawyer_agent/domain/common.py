import secrets
import time
from uuid import UUID


def new_uuid7(*, now_ms: int | None = None) -> UUID:
    timestamp = int(time.time_ns() // 1_000_000 if now_ms is None else now_ms)
    if not 0 <= timestamp < 1 << 48:
        raise ValueError("UUIDv7 timestamp out of range")
    random_a = secrets.randbits(12)
    random_b = secrets.randbits(62)
    value = (timestamp << 80) | (0x7 << 76) | (random_a << 64) | (0b10 << 62) | random_b
    return UUID(int=value)
