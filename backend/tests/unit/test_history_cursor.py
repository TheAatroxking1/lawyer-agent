import base64
import json
from datetime import UTC, datetime

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.persistence.history_cursor import decode_cursor, encode_cursor


def cursor(tenant, user, at, ident):
    return base64.urlsafe_b64encode(
        json.dumps([str(tenant), str(user), at, ident]).encode()
    ).decode()


@pytest.mark.parametrize(
    "at,ident",
    [
        ("2026-09-21T00:00:00", 123),
        ("2026-09-21T00:00:00", []),
        ("2026-09-21T00:00:00", True),
        (123, "valid_uuid"),
        ([], "valid_uuid"),
        ("0001-01-01T00:00:00+01:00", "valid_uuid"),
        ("9999-12-31T23:59:59-01:00", "valid_uuid"),
    ],
)
def test_malformed_cursor_has_stable_value_error(at, ident):
    tenant, user = new_uuid7(), new_uuid7()
    value = cursor(tenant, user, at, str(new_uuid7()) if ident == "valid_uuid" else ident)
    with pytest.raises(ValueError, match="^invalid_history_cursor$"):
        decode_cursor(value, tenant, user)


def test_valid_cursor_preserves_utc_and_owner_boundary():
    tenant, user, ident = new_uuid7(), new_uuid7(), new_uuid7()
    at = datetime(2026, 9, 21, tzinfo=UTC)
    value = encode_cursor(tenant, user, at, ident)
    assert decode_cursor(value, tenant, user) == (at.replace(tzinfo=None), ident)
    with pytest.raises(ValueError, match="^invalid_history_cursor$"):
        decode_cursor(value, tenant, new_uuid7())
