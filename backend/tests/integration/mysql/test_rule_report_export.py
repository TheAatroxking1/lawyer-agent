from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.documents import DocumentCreateCommand, DocumentUploadService
from lawyer_agent.application.report_export import RiskReportService
from lawyer_agent.application.rules import RuleCheckService, RuleEngine
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.matter_documents import MatterKind
from lawyer_agent.domain.rule_pack import RiskIssueStatus
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
from lawyer_agent.infrastructure.documents.docx_writer import ZipDocxReportWriter
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser
from lawyer_agent.infrastructure.objects.object_store import LocalObjectStorePlaceholder
from lawyer_agent.infrastructure.persistence.repositories.documents import (
    SqlAlchemyDocumentRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.matters import (
    SqlAlchemyMatterRepository,
)
from lawyer_agent.infrastructure.persistence.repositories.rule_pack import (
    SqlAlchemyRulePackRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _context(tenant_id: UUID) -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        membership_id=new_uuid7(),
        membership_user_id=new_uuid7(),
        department_id=None,
        tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE,
        valid_from=datetime.now(UTC),
        valid_until=None,
        authz_version=1,
        session_authz_version=1,
        scope=AuthorizationScope(allow_tenant_wide=False),
    )


async def _seed_tenant_with_document(
    mysql_url: URL, factory, *, active_pack: bool
) -> tuple[UUID, UUID]:
    """Seed one tenant with an accepted original document and rule pack."""
    tenant_id = new_uuid7()
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
        matter = await SqlAlchemyMatterRepository(session).create_matter(
            tenant_id=tenant_id,
            title="租赁合同审查",
            kind=MatterKind.CONTRACT_REVIEW,
            created_by_user_id=user_id,
            created_by_membership_id=membership_id,
        )
        version = await DocumentUploadService(
            LocalObjectStorePlaceholder(), SqlAlchemyDocumentRepository(session)
        ).complete(
            DocumentCreateCommand(
                tenant_id=tenant_id,
                matter_id=matter.id,
                file_name="租赁合同.docx",
                mime_type=_DOCX_MIME,
                payload=b"fake-original",
                created_by_user_id=user_id,
                created_by_membership_id=membership_id,
            )
        )
        pack_id = new_uuid7()
        await session.execute(
            text(
                "INSERT INTO tenant_rule_packs "
                "(id,tenant_id,name,version,active) VALUES (:id,:tenant,'p',1,:active)"
            ),
            {"id": pack_id.bytes, "tenant": tenant_id.bytes, "active": int(active_pack)},
        )
        await session.execute(
            text(
                "INSERT INTO tenant_rule_pack_rules "
                "(id,tenant_id,pack_id,trigger_kind,label,pattern,risk_level,"
                "suggestion_template,enabled) "
                "VALUES (:id,:tenant,:pack,:kind,:label,:pattern,:level,:suggestion,1)"
            ),
            {
                "id": new_uuid7().bytes,
                "tenant": tenant_id.bytes,
                "pack": pack_id.bytes,
                "kind": "risk_phrase",
                "label": "高额违约金",
                "pattern": r"(?:百分之[一二三四五六七八九十百0-9]+|[0-9]+\s*%)",
                "level": "high",
                "suggestion": "违约金比例较高，请人工核验。",
            },
        )
        await session.commit()
    return tenant_id, version.document_id


async def _cleanup(mysql_url: URL, tenant_ids: list[UUID]) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenant_contract_risk_issues"))
            await connection.execute(text("DELETE FROM tenant_rule_pack_rules"))
            await connection.execute(text("DELETE FROM tenant_rule_packs"))
            await connection.execute(text("DELETE FROM tenant_document_versions"))
            await connection.execute(text("DELETE FROM tenant_documents"))
            await connection.execute(text("DELETE FROM tenant_matter_parties"))
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenant_matters WHERE tenant_id=:id"),
                    {"id": tenant_id.bytes},
                )
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenant_memberships WHERE tenant_id=:id"),
                    {"id": tenant_id.bytes},
                )
            for tenant_id in tenant_ids:
                await connection.execute(
                    text("DELETE FROM tenants WHERE id=:id"),
                    {"id": tenant_id.bytes},
                )
            await connection.execute(
                text(
                    "DELETE FROM users WHERE id NOT IN "
                    "(SELECT created_by_user_id FROM tenants)"
                )
            )
    finally:
        await engine.dispose()


def _docx_bytes(paragraphs: list[str]) -> bytes:
    import io
    import zipfile

    xml_ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    runs = "".join(
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>" for paragraph in paragraphs
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f'<w:document xmlns:w="{xml_ns}"><w:body>{runs}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


async def _run_export_flow(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant_a, document_a = await _seed_tenant_with_document(
        mysql_url, factory, active_pack=True
    )
    tenant_b, _document_b = await _seed_tenant_with_document(
        mysql_url, factory, active_pack=True
    )
    try:
        # Run a check against tenant A's document and dispose one issue.
        async with factory() as session:
            repo = SqlAlchemyRulePackRepository(session)
            loader = ZipDocxLoader(source_ref="object://tenant/lease.docx")
            parsed = loader.load(
                "object://tenant/lease.docx",
                _docx_bytes(
                    [
                        "租赁合同",
                        "第十条 违约责任：逾期交付应按日租金的百分之三支付违约金。",
                    ]
                ),
            )
            instrument = LegalStructureParser().parse(parsed)
            from lawyer_agent.application.rules import ProvisionInput

            provisions = tuple(
                ProvisionInput(provision_no=article.provision_no, text=article.text)
                for article in instrument.articles
            )
            created = await RuleCheckService(
                rule_store=repo, engine=RuleEngine()
            ).run_checks(
                context=_context(tenant_a),
                document_id=document_a,
                provisions=provisions,
            )
            await session.commit()
            assert len(created) == 1
            issue_id = created[0].id
            disposed = await RuleCheckService(rule_store=repo, engine=RuleEngine()).dispose(
                context=_context(tenant_a),
                issue_id=issue_id,
                status=RiskIssueStatus.ACCEPTED,
                reason="律师确认百分之三违约金可接受",
                now=datetime.now(UTC),
            )
            assert disposed is True
            await session.commit()

        # Export a report for tenant A and read it back with the docx loader.
        async with factory() as session:
            documents = SqlAlchemyDocumentRepository(session)
            issues = SqlAlchemyRulePackRepository(session)
            payload = await RiskReportService(
                documents=documents, issues=issues, writer=ZipDocxReportWriter()
            ).export(
                context=_context(tenant_a),
                document_id=document_a,
                now=datetime(2026, 9, 5, 11, 0, 0, tzinfo=UTC),
            )
            assert payload[:2] == b"PK"
            back = ZipDocxLoader(source_ref="object://tenant/report.docx").load(
                "object://tenant/report.docx", payload
            )
            text_joined = "\n".join(p.text for p in back.paragraphs)
            assert "合同规则检查工作报告（草稿）" in text_joined
            assert "规则候选提示" in text_joined
            assert "非法律意见" in text_joined
            assert "百分之三" in text_joined
            assert "已接受" in text_joined
            assert "律师确认百分之三违约金可接受" in text_joined
            assert "待人工核验" not in text_joined

        # Cross-tenant reverse: tenant B cannot export tenant A's document.
        async with factory() as session:
            documents = SqlAlchemyDocumentRepository(session)
            issues = SqlAlchemyRulePackRepository(session)
            try:
                await RiskReportService(
                    documents=documents, issues=issues, writer=ZipDocxReportWriter()
                ).export(
                    context=_context(tenant_b),
                    document_id=document_a,
                    now=datetime.now(UTC),
                )
                raise AssertionError("tenant B unexpectedly exported tenant A document")
            except ValueError as exc:
                assert "tenant" in str(exc)
    finally:
        await engine.dispose()
        await _cleanup(mysql_url, [tenant_a, tenant_b])


def test_rule_check_docx_export_over_mysql(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_export_flow(mysql_url))
