from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.documents import DocumentCreateCommand, DocumentUploadService
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentUploadStatus,
    MatterKind,
)
from lawyer_agent.infrastructure.objects.object_store import LocalObjectStorePlaceholder
from lawyer_agent.infrastructure.persistence.models.matter_documents import (
    TenantDocumentModel,
    TenantDocumentVersionModel,
)
from lawyer_agent.infrastructure.persistence.repositories.documents import (
    SqlAlchemyDocumentRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.matters import (
    SqlAlchemyMatterRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


async def _seed_tenant_and_matter(mysql_url: URL) -> tuple[UUID, UUID]:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant_id = new_uuid7()
    matter_id = new_uuid7()
    try:
        async with factory() as session:
            user_id = new_uuid7()
            membership_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO users (id,status,display_name,auth_version,version) "
                    "VALUES (:id,'active','u',1,1)"
                ),
                {"id": user_id.bytes},
            )
            await session.execute(
                text(
                    "INSERT INTO tenants "
                    "(id,name,normalized_name,tenant_type,status,review_status,"
                    "created_by_user_id,version) "
                    "VALUES (:id,:name,:norm,'enterprise','active','approved',:user,1)"
                ),
                {
                    "id": tenant_id.bytes,
                    "name": f"t-{tenant_id}",
                    "norm": f"t-{tenant_id}".lower(),
                    "user": user_id.bytes,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_memberships "
                    "(id,tenant_id,user_id,member_type,status,valid_from,authz_version,version) "
                    "VALUES (:id,:tenant,:user,'owner','active',UTC_TIMESTAMP(6),1,1)"
                ),
                {"id": membership_id.bytes, "tenant": tenant_id.bytes, "user": user_id.bytes},
            )
            repo = SqlAlchemyMatterRepository(session)
            matter = await repo.create_matter(
                tenant_id=tenant_id,
                title="合同审查",
                kind=MatterKind.CONTRACT_REVIEW,
                created_by_user_id=user_id,
                created_by_membership_id=membership_id,
            )
            matter_id = matter.id
            await session.commit()
    finally:
        await engine.dispose()
    return tenant_id, matter_id


async def _cleanup(mysql_url: URL, tenant_id: UUID) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenant_document_versions"))
            await connection.execute(text("DELETE FROM tenant_documents"))
            await connection.execute(text("DELETE FROM tenant_matter_parties"))
            await connection.execute(
                text("DELETE FROM tenant_matters WHERE tenant_id=:id"), {"id": tenant_id.bytes}
            )
            await connection.execute(
                text("DELETE FROM tenant_memberships WHERE tenant_id=:id"),
                {"id": tenant_id.bytes},
            )
            await connection.execute(
                text("DELETE FROM tenants WHERE id=:id"), {"id": tenant_id.bytes}
            )
            await connection.execute(
                text(
                    "DELETE FROM users WHERE id NOT IN "
                    "(SELECT created_by_user_id FROM tenants)"
                )
            )
    finally:
        await engine.dispose()


async def _run_upload_flow(mysql_url: URL) -> None:
    tenant_id, matter_id = await _seed_tenant_and_matter(mysql_url)
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            objects = LocalObjectStorePlaceholder()
            repo = SqlAlchemyDocumentRepository(session)
            service = DocumentUploadService(objects, repo)
            version = await service.complete(
                DocumentCreateCommand(
                    tenant_id=tenant_id,
                    matter_id=matter_id,
                    file_name="lease.docx",
                    mime_type=_DOCX_MIME,
                    payload=b"fake-docx-bytes",
                    created_by_user_id=new_uuid7(),
                    created_by_membership_id=new_uuid7(),
                )
            )
            await session.commit()
            assert version.upload_status is DocumentUploadStatus.ACCEPTED

            stored_header = await session.scalar(
                select(TenantDocumentModel).where(
                    TenantDocumentModel.tenant_id == tenant_id,
                    TenantDocumentModel.matter_id == matter_id,
                    TenantDocumentModel.id == version.document_id,
                )
            )
            assert stored_header is not None
            stored_version = await session.scalar(
                select(TenantDocumentVersionModel).where(
                    TenantDocumentVersionModel.id == version.id
                )
            )
            assert stored_version is not None
            assert stored_version.upload_status == "accepted"
            assert stored_version.object_key.startswith(f"tenant_{tenant_id.hex}/")
    finally:
        await engine.dispose()
        await _cleanup(mysql_url, tenant_id)


def test_document_upload_flow_persists_version(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_upload_flow(mysql_url))
