from __future__ import annotations

import asyncio
import io
import zipfile
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from test_ai_job_migration import _alembic_config

from alembic import command
from lawyer_agent.application.rules import ProvisionInput, RuleCheckService, RuleEngine
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.rule_pack import RiskIssueStatus
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
from lawyer_agent.infrastructure.documents.parsers import LegalStructureParser
from lawyer_agent.infrastructure.persistence.models.rule_pack import (
    TenantContractRiskIssueModel,
)
from lawyer_agent.infrastructure.persistence.repositories.rule_pack import (
    SqlAlchemyRulePackRepository,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_XML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx_bytes(paragraphs: list[str]) -> bytes:
    runs = "".join(
        f"<w:p><w:r><w:t>{text_para}</w:t></w:r></w:p>" for text_para in paragraphs
    )
    xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f'<w:document xmlns:w="{_XML_NS}"><w:body>{runs}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


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


async def _seed_document_and_pack(
    mysql_url: URL,
) -> tuple[UUID, UUID]:
    """Seed tenant + matter + document + active pack with one risk rule."""
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    tenant_id = new_uuid7()
    pack_id = new_uuid7()
    try:
        async with factory() as session:
            user_id = new_uuid7()
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
            matter_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO tenant_matters "
                    "(id,tenant_id,title,kind,status,created_by_user_id,"
                    "created_by_membership_id,version) "
                    "VALUES (:id,:tenant,'m','contract_review','open',:u,:m,1)"
                ),
                {
                    "id": matter_id.bytes,
                    "tenant": tenant_id.bytes,
                    "u": new_uuid7().bytes,
                    "m": new_uuid7().bytes,
                },
            )
            document_id = new_uuid7()
            await session.execute(
                text(
                    "INSERT INTO tenant_documents "
                    "(id,tenant_id,matter_id,display_name,current_version_no,version) "
                    "VALUES (:id,:tenant,:matter,'lease.docx',1,1)"
                ),
                {"id": document_id.bytes, "tenant": tenant_id.bytes, "matter": matter_id.bytes},
            )
            await session.execute(
                text(
                    "INSERT INTO tenant_rule_packs "
                    "(id,tenant_id,name,version,active) VALUES (:id,:tenant,'p',1,1)"
                ),
                {"id": pack_id.bytes, "tenant": tenant_id.bytes},
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
    finally:
        await engine.dispose()
    return tenant_id, document_id


async def _cleanup(mysql_url: URL, tenant_id: UUID) -> None:
    engine = create_async_engine(mysql_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM tenant_contract_risk_issues"))
            await connection.execute(text("DELETE FROM tenant_rule_pack_rules"))
            await connection.execute(text("DELETE FROM tenant_rule_packs"))
            await connection.execute(text("DELETE FROM tenant_document_versions"))
            await connection.execute(text("DELETE FROM tenant_documents"))
            await connection.execute(text("DELETE FROM tenant_matter_parties"))
            await connection.execute(
                text("DELETE FROM tenant_matters WHERE tenant_id=:id"),
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


async def _provisions_from_docx(payload: bytes) -> tuple[ProvisionInput, ...]:
    loader = ZipDocxLoader(source_ref="object://tenant/lease.docx")
    document = loader.load("object://tenant/lease.docx", payload)
    instrument = LegalStructureParser().parse(document)
    return tuple(
        ProvisionInput(provision_no=article.provision_no, text=article.text)
        for article in instrument.articles
    )


async def _run_flow(mysql_url: URL) -> None:
    tenant_id, document_id = await _seed_document_and_pack(mysql_url)
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        v1 = _docx_bytes(
            [
                "租赁合同",
                "第十条 违约责任：甲方逾期交付租赁物的，应按日租金的百分之三支付违约金。",
                "第十二条 本合同自双方签字之日起生效。",
            ]
        )
        v2 = _docx_bytes(
            [
                "租赁合同",
                "第十条 违约责任：甲方逾期交付租赁物的，应按日租金的百分之十五支付违约金。",
                "第十二条 本合同自双方签字之日起生效。",
            ]
        )
        provisions = await _provisions_from_docx(v1)
        assert len(provisions) == 2
        async with factory() as session:
            repo = SqlAlchemyRulePackRepository(session)
            service = RuleCheckService(rule_store=repo, engine=RuleEngine())
            created = await service.run_checks(
                context=_context(tenant_id),
                document_id=document_id,
                provisions=provisions,
            )
            await session.commit()
            assert len(created) == 1
            first_id = created[0].id
            assert created[0].status is RiskIssueStatus.OPEN
            assert "百分之三" in created[0].matched_text

            disposed = await service.dispose(
                context=_context(tenant_id),
                issue_id=first_id,
                status=RiskIssueStatus.ACCEPTED,
                reason="律师确认百分之三违约金可接受",
                now=datetime.now(UTC),
            )
            assert disposed is True
            await session.commit()
            stored = await session.scalar(
                select(TenantContractRiskIssueModel).where(
                    TenantContractRiskIssueModel.id == first_id
                )
            )
            assert stored is not None and stored.status == "accepted"

        # Re-run on an amended document: the old open finding is gone from the
        # store (already accepted); the changed clause opens a fresh issue.
        provisions2 = await _provisions_from_docx(v2)
        async with factory() as session:
            repo = SqlAlchemyRulePackRepository(session)
            service = RuleCheckService(rule_store=repo, engine=RuleEngine())
            created2 = await service.run_checks(
                context=_context(tenant_id),
                document_id=document_id,
                provisions=provisions2,
            )
            await session.commit()
            assert len(created2) == 1
            assert "百分之十五" in created2[0].matched_text
    finally:
        await engine.dispose()
        await _cleanup(mysql_url, tenant_id)


def test_rule_engine_docx_disposition_and_rerun_flow(mysql_url: URL) -> None:
    config = _alembic_config(mysql_url)
    command.upgrade(config, "head")
    asyncio.run(_run_flow(mysql_url))
