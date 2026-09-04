"""Managed object-store reference port for tenant documents.

This non-model slice stores upload metadata and managed object keys only. Real
pre-signed multipart uploads to MinIO/S3 are a later slice; this implementation
keeps the tenant-prefix whitelist contract in force from day one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7, require_uuid7

_KEY_PATTERN = re.compile(r"^tenant_[0-9a-f]{32}/[a-z0-9._/-]{1,255}$", re.ASCII)


class ObjectKeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class UploadTarget:
    object_key: str
    upload_id: UUID
    expires_at: datetime
    pre_signed_url: str | None = None


class ObjectStorePort(Protocol):
    """Create/confirm/delete managed object references under tenant keys."""

    def create_upload_target(
        self,
        tenant_id: UUID,
        *,
        original_name: str,
        ttl_seconds: int = 900,
    ) -> UploadTarget: ...

    def confirm_object_key(self, tenant_id: UUID, object_key: str) -> bool: ...

    def delete_object_key(self, tenant_id: UUID, object_key: str) -> None: ...


def tenant_object_key(tenant_id: UUID, original_name: str) -> str:
    require_uuid7(tenant_id, field="object tenant_id")
    if not isinstance(original_name, str) or not original_name.strip():
        raise ObjectKeyError("original file name must be non-empty")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name)[:255]
    return f"tenant_{tenant_id.hex}/doc/{safe_name}"


def assert_object_key_owned(tenant_id: UUID, object_key: str) -> None:
    require_uuid7(tenant_id, field="object tenant_id")
    if not isinstance(object_key, str):
        raise ObjectKeyError("object key must be text")
    if _KEY_PATTERN.fullmatch(object_key) is None:
        raise ObjectKeyError("object key is not a managed tenant key")
    if not object_key.startswith(f"tenant_{tenant_id.hex}/"):
        raise ObjectKeyError("object key does not belong to this tenant")
    segments = object_key.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise ObjectKeyError("object key must not contain traversal segments")


class LocalObjectStorePlaceholder(ObjectStorePort):
    """Placeholder keeping the managed-key contract; real MinIO comes later."""

    def __init__(self) -> None:
        self._live: set[str] = set()

    def create_upload_target(
        self,
        tenant_id: UUID,
        *,
        original_name: str,
        ttl_seconds: int = 900,
    ) -> UploadTarget:
        object_key = tenant_object_key(tenant_id, original_name)
        self._live.add(object_key)
        return UploadTarget(
            object_key=object_key,
            upload_id=new_uuid7(),
            expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
        )

    def confirm_object_key(self, tenant_id: UUID, object_key: str) -> bool:
        try:
            assert_object_key_owned(tenant_id, object_key)
        except ObjectKeyError:
            return False
        return object_key in self._live

    def delete_object_key(self, tenant_id: UUID, object_key: str) -> None:
        assert_object_key_owned(tenant_id, object_key)
        self._live.discard(object_key)
