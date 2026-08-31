from uuid import RFC_4122, UUID

import pytest

from lawyer_agent.domain.common import new_uuid7


def test_uuid7_uses_standard_network_bytes() -> None:
    value = new_uuid7()

    assert value.version == 7
    assert value.variant == RFC_4122
    assert UUID(bytes=value.bytes) == value


@pytest.mark.parametrize("timestamp", [-1, 1 << 48])
def test_uuid7_rejects_timestamps_outside_the_rfc_range(timestamp: int) -> None:
    with pytest.raises(ValueError, match="UUIDv7 timestamp out of range"):
        new_uuid7(now_ms=timestamp)
