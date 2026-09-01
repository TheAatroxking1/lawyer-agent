from uuid import RFC_4122, UUID, uuid1, uuid4

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.types import UuidBinary


def test_uuid7_uses_standard_network_bytes() -> None:
    value = new_uuid7()

    assert value.version == 7
    assert value.variant == RFC_4122
    assert UUID(bytes=value.bytes) == value


@pytest.mark.parametrize("timestamp", [-1, 1 << 48])
def test_uuid7_rejects_timestamps_outside_the_rfc_range(timestamp: int) -> None:
    with pytest.raises(ValueError, match="UUIDv7 timestamp out of range"):
        new_uuid7(now_ms=timestamp)


@pytest.mark.parametrize(
    "value",
    [uuid1(), uuid4(), UUID(int=0), True, "01990f00-0000-7000-8000-000000000501", object()],
)
def test_uuid_binary_rejects_non_uuid7_values_before_driver_binding(value: object) -> None:
    with pytest.raises(ValueError, match="UUIDv7"):
        UuidBinary().process_bind_param(value, None)  # type: ignore[arg-type]
