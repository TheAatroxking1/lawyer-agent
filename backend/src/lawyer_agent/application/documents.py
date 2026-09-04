"""Tenant document upload session and completion (metadata only, non-model)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentKind,
    DocumentUploadStatus,
    DocumentVersion,
)
from lawyer_agent.infrastructure.objects.object_store import (
    ObjectStorePort,
    UploadTarget,
)

_MAX_UPLOAD_BYTES = 64 * 1024 * 1024
_MIME_PATTERN = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$", re.ASCII)


class DocumentStorePort(Protocol):
    async def save_document_version(
        self, version: DocumentVersion, *, matter_id: UUID
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DocumentCreateCommand:
    tenant_id: UUID
    matter_id: UUID
    file_name: str
    mime_type: str
    payload: bytes
    created_by_user_id: UUID
    created_by_membership_id: UUID


@dataclass(frozen=True, slots=True)
class UploadSessionResult:
    upload_id: UUID
    target: UploadTarget
    expected_sha256: bytes


class DocumentUploadService:
    """Creates upload sessions and completes them with validation (no AV)."""

    def __init__(
        self,
        objects: ObjectStorePort,
        store: DocumentStorePort,
        *,
        allowed_mime_prefixes: tuple[str, ...] = (
            "application/vnd.openxmlformats-officedocument.wordprocessingml",
        ),
    ) -> None:
        self._objects = objects
        self._store = store
        self._allowed_mime_prefixes = allowed_mime_prefixes

    def begin(self, command: DocumentCreateCommand) -> UploadSessionResult:
        self._validate_command(command)
        target = self._objects.create_upload_target(
            command.tenant_id,
            original_name=command.file_name,
        )
        return UploadSessionResult(
            upload_id=target.upload_id,
            target=target,
            expected_sha256=hashlib.sha256(command.payload).digest(),
        )

    async def complete(self, command: DocumentCreateCommand) -> DocumentVersion:
        self._validate_command(command)
        if len(command.payload) > _MAX_UPLOAD_BYTES:
            raise ValueError("upload exceeds the size limit")
        status = DocumentUploadStatus.ACCEPTED
        if not _MIME_PATTERN.fullmatch(command.mime_type) or not any(
            command.mime_type.startswith(prefix)
            for prefix in self._allowed_mime_prefixes
        ):
            status = DocumentUploadStatus.NEEDS_REVIEW
        version = DocumentVersion(
            id=new_uuid7(),
            tenant_id=command.tenant_id,
            document_id=new_uuid7(),
            version_no=1,
            kind=DocumentKind.ORIGINAL,
            object_key=self._objects.create_upload_target(
                command.tenant_id, original_name=command.file_name
            ).object_key,
            sha256=hashlib.sha256(command.payload).digest(),
            upload_status=status,
            file_name=command.file_name,
            mime_type=command.mime_type,
            size_bytes=len(command.payload),
            created_by_user_id=command.created_by_user_id,
            created_by_membership_id=command.created_by_membership_id,
            uploaded_at=datetime.now(UTC),
        )
        await self._store.save_document_version(version, matter_id=command.matter_id)
        return version

    def _validate_command(self, command: DocumentCreateCommand) -> None:
        for value, name in (
            (command.tenant_id, "tenant_id"),
            (command.matter_id, "matter_id"),
            (command.created_by_user_id, "created_by_user_id"),
            (command.created_by_membership_id, "created_by_membership_id"),
        ):
            require_uuid7(value, field=name)
        if not isinstance(command.file_name, str) or not command.file_name.strip():
            raise ValueError("file name must be non-empty text")
        if not isinstance(command.payload, bytes) or not command.payload:
            raise ValueError("upload payload must be non-empty bytes")
