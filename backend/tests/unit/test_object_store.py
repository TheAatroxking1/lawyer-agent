from __future__ import annotations

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.objects.object_store import (
    LocalObjectStorePlaceholder,
    ObjectKeyError,
    assert_object_key_owned,
    tenant_object_key,
)


def test_tenant_object_key_prefix_and_sanitization() -> None:
    tenant_id = new_uuid7()
    key = tenant_object_key(tenant_id, "租赁合同 (final).docx")
    assert key.startswith(f"tenant_{tenant_id.hex}/doc/")
    assert " " not in key
    assert "(" not in key


def test_key_ownership_assertion() -> None:
    tenant_a = new_uuid7()
    tenant_b = new_uuid7()
    key = tenant_object_key(tenant_a, "a.docx")
    assert_object_key_owned(tenant_a, key)
    with pytest.raises(ObjectKeyError, match="does not belong"):
        assert_object_key_owned(tenant_b, key)


def test_key_rejects_traversal_and_junk() -> None:
    tenant_id = new_uuid7()
    for bad in ("../etc/passwd", "", f"tenant_{tenant_id.hex}/../../x"):
        with pytest.raises(ObjectKeyError):
            assert_object_key_owned(tenant_id, bad)


def test_placeholder_confirm_requires_created_key() -> None:
    tenant_id = new_uuid7()
    store = LocalObjectStorePlaceholder()
    target = store.create_upload_target(tenant_id, original_name="x.docx")
    assert store.confirm_object_key(tenant_id, target.object_key) is True
    foreign = new_uuid7()
    assert store.confirm_object_key(foreign, target.object_key) is False
    store.delete_object_key(tenant_id, target.object_key)
    assert store.confirm_object_key(tenant_id, target.object_key) is False
