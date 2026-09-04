from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.matter_documents import DocumentVersion
from lawyer_agent.infrastructure.persistence.models.matter_documents import (
    TenantDocumentModel,
    TenantDocumentVersionModel,
)


class SqlAlchemyDocumentRepository:
    """Persists tenant document headers and immutable version rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def save_document_version(
        self, version: DocumentVersion, *, matter_id: UUID
    ) -> None:
        self._session.add(
            TenantDocumentModel(
                id=version.document_id,
                tenant_id=version.tenant_id,
                matter_id=matter_id,
                display_name=version.file_name or version.object_key,
                current_version_no=version.version_no,
                version=1,
            )
        )
        self._session.add(
            TenantDocumentVersionModel(
                id=version.id,
                tenant_id=version.tenant_id,
                document_id=version.document_id,
                version_no=version.version_no,
                kind=version.kind.value,
                object_key=version.object_key,
                sha256=version.sha256,
                upload_status=version.upload_status.value,
                review_status=version.review_status.value
                if version.review_status is not None
                else None,
                file_name=version.file_name,
                mime_type=version.mime_type,
                size_bytes=version.size_bytes,
                parser_version=version.parser_version,
                parse_error=version.parse_error,
                created_by_user_id=version.created_by_user_id,
                created_by_membership_id=version.created_by_membership_id,
                uploaded_at=_naive_optional(version.uploaded_at),
            )
        )
        await self._session.flush()


def _naive_optional(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
