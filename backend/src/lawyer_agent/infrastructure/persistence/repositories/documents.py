from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from lawyer_agent.domain.document_review import (
    InvalidReviewTransition,
    ReviewDecision,
    require_review_reason,
    review_target_status,
)
from lawyer_agent.domain.matter_documents import (
    DocumentHeader,
    DocumentKind,
    DocumentUploadStatus,
    DocumentVersion,
    ReviewStatus,
)
from lawyer_agent.infrastructure.persistence.models.matter_documents import (
    TenantDocumentModel,
    TenantDocumentVersionModel,
)

_KIND_MAP = {"original": DocumentKind.ORIGINAL, "derived": DocumentKind.DERIVED}
_UPLOAD_MAP = {
    "uploaded": DocumentUploadStatus.UPLOADED,
    "validating": DocumentUploadStatus.VALIDATING,
    "accepted": DocumentUploadStatus.ACCEPTED,
    "needs_review": DocumentUploadStatus.NEEDS_REVIEW,
    "parsing": DocumentUploadStatus.PARSING,
    "ready": DocumentUploadStatus.READY,
    "failed": DocumentUploadStatus.FAILED,
}
_REVIEW_MAP = {
    "draft": ReviewStatus.DRAFT,
    "pending_review": ReviewStatus.PENDING_REVIEW,
    "changes_requested": ReviewStatus.CHANGES_REQUESTED,
    "approved": ReviewStatus.APPROVED,
    "rejected": ReviewStatus.REJECTED,
}


class ReviewTargetNotFound(ValueError):
    pass


class SqlAlchemyDocumentRepository:
    """Persists tenant document headers and immutable version rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def document_exists(self, tenant_id: UUID, document_id: UUID) -> bool:
        model = await self._session.scalar(
            select(TenantDocumentModel.id).where(
                TenantDocumentModel.tenant_id == tenant_id,
                TenantDocumentModel.id == document_id,
            )
        )
        return model is not None

    async def headers_for_matter(
        self, tenant_id: UUID, matter_id: UUID
    ) -> tuple[DocumentHeader, ...]:
        rows = await self._session.scalars(
            select(TenantDocumentModel)
            .where(
                TenantDocumentModel.tenant_id == tenant_id,
                TenantDocumentModel.matter_id == matter_id,
            )
            .order_by(TenantDocumentModel.created_at)
        )
        return tuple(
            DocumentHeader(
                id=row.id,
                tenant_id=row.tenant_id,
                matter_id=row.matter_id,
                display_name=row.display_name,
                current_version_no=row.current_version_no,
                version=row.version,
            )
            for row in rows
        )

    async def find_version(
        self, tenant_id: UUID, document_id: UUID, version_no: int | None = None
    ) -> DocumentVersion | None:
        statement = select(TenantDocumentVersionModel).where(
            TenantDocumentVersionModel.tenant_id == tenant_id,
            TenantDocumentVersionModel.document_id == document_id,
        )
        if version_no is not None:
            statement = statement.where(
                TenantDocumentVersionModel.version_no == version_no
            )
        model = await self._session.scalar(
            statement.order_by(TenantDocumentVersionModel.version_no.desc())
        )
        return None if model is None else _document_version(model)

    async def review_document_version(
        self,
        *,
        tenant_id: UUID,
        document_id: UUID,
        version_no: int,
        decision: ReviewDecision,
        reason: str | None,
    ) -> DocumentVersion:
        """Apply a human review decision to one version atomically.

        Only versions whose upload has completed (accepted or ready) can enter
        review. The transition is validated by the domain state machine and
        written with a guarded update on the unique version key.
        """
        require_review_reason(decision, reason)
        version = await self.find_version(tenant_id, document_id, version_no)
        if version is None:
            raise ReviewTargetNotFound("document version not found")
        if version.upload_status not in {
            DocumentUploadStatus.ACCEPTED,
            DocumentUploadStatus.READY,
        }:
            raise InvalidReviewTransition(
                f"upload must be accepted or ready before review "
                f"(current={version.upload_status.value})"
            )
        target = review_target_status(version.review_status, decision)
        stored_reason = reason.strip() if reason is not None else None
        if target is ReviewStatus.APPROVED or target is ReviewStatus.PENDING_REVIEW:
            stored_reason = None
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                update(TenantDocumentVersionModel)
                .where(
                    TenantDocumentVersionModel.tenant_id == tenant_id,
                    TenantDocumentVersionModel.document_id == document_id,
                    TenantDocumentVersionModel.version_no == version_no,
                )
                .values(review_status=target.value, review_reason=stored_reason)
            ),
        )
        if result.rowcount != 1:
            raise InvalidReviewTransition("review update affected no row")
        updated = await self.find_version(tenant_id, document_id, version_no)
        if updated is None:
            raise InvalidReviewTransition("review update could not be read back")
        return updated

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
                review_reason=version.review_reason,
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


def _document_version(model: TenantDocumentVersionModel) -> DocumentVersion:
    return DocumentVersion(
        id=model.id,
        tenant_id=model.tenant_id,
        document_id=model.document_id,
        version_no=model.version_no,
        kind=_KIND_MAP[model.kind],
        object_key=model.object_key,
        sha256=bytes(model.sha256),
        upload_status=_UPLOAD_MAP[model.upload_status],
        file_name=model.file_name,
        mime_type=model.mime_type,
        size_bytes=model.size_bytes,
        parser_version=model.parser_version,
        parse_error=model.parse_error,
        review_status=_REVIEW_MAP[model.review_status]
        if model.review_status is not None
        else None,
        review_reason=model.review_reason,
        created_by_user_id=model.created_by_user_id,
        created_by_membership_id=model.created_by_membership_id,
        uploaded_at=_aware_optional(model.uploaded_at),
    )


def _aware_optional(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _naive_optional(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
