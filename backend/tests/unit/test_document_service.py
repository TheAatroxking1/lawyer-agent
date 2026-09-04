from __future__ import annotations

import asyncio

import pytest

from lawyer_agent.application.documents import (
    DocumentCreateCommand,
    DocumentUploadService,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentKind,
    DocumentUploadStatus,
)
from lawyer_agent.infrastructure.objects.object_store import LocalObjectStorePlaceholder


class _FakeStore:
    def __init__(self) -> None:
        self.saved = []

    async def save_document_version(self, version) -> None:
        self.saved.append(version)


def _command(
    *,
    mime_type: str = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    payload: bytes = b"fake docx bytes",
) -> DocumentCreateCommand:
    return DocumentCreateCommand(
        tenant_id=new_uuid7(),
        matter_id=new_uuid7(),
        file_name="contract.docx",
        mime_type=mime_type,
        payload=payload,
        created_by_user_id=new_uuid7(),
        created_by_membership_id=new_uuid7(),
    )


def test_upload_accepts_docx_mime() -> None:
    store = _FakeStore()
    service = DocumentUploadService(LocalObjectStorePlaceholder(), store)
    version = asyncio.run(service.complete(_command()))
    assert version.upload_status is DocumentUploadStatus.ACCEPTED
    assert version.kind is DocumentKind.ORIGINAL
    assert version.version_no == 1
    assert store.saved == [version]


def test_upload_unknown_mime_goes_needs_review() -> None:
    store = _FakeStore()
    service = DocumentUploadService(LocalObjectStorePlaceholder(), store)
    version = asyncio.run(
        service.complete(_command(mime_type="application/octet-stream"))
    )
    assert version.upload_status is DocumentUploadStatus.NEEDS_REVIEW


def test_upload_rejects_oversize_payload() -> None:
    service = DocumentUploadService(LocalObjectStorePlaceholder(), _FakeStore())
    with pytest.raises(ValueError, match="size limit"):
        asyncio.run(service.complete(_command(payload=b"x" * (64 * 1024 * 1024 + 1))))


def test_begin_creates_tenant_scoped_target() -> None:
    command = _command()
    objects = LocalObjectStorePlaceholder()
    service = DocumentUploadService(objects, _FakeStore())
    result = service.begin(command)
    assert result.target.object_key.startswith(f"tenant_{command.tenant_id.hex}/")
    assert len(result.expected_sha256) == 32
