from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import pytest

from lawyer_agent.application.report_export import RiskReportService
from lawyer_agent.domain.authorization import AuthorizationScope
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentKind,
    DocumentUploadStatus,
    DocumentVersion,
)
from lawyer_agent.domain.rule_pack import (
    RiskIssue,
    RiskIssueStatus,
    RiskLevel,
)
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
from lawyer_agent.infrastructure.documents.docx_writer import ZipDocxReportWriter

_NOW = datetime(2026, 9, 5, 10, 0, 0, tzinfo=UTC)

_DISCLAIMER_FRAGMENT = "规则候选提示"
_DISCLAIMER_LEGAL = "非法律意见"


class FakeDocumentStore:
    def __init__(self) -> None:
        self.version: DocumentVersion | None = None

    async def find_version(self, tenant_id: UUID, document_id: UUID):
        if (
            self.version is None
            or self.version.tenant_id != tenant_id
            or self.version.document_id != document_id
        ):
            return None
        return self.version


class FakeIssueStore:
    def __init__(self) -> None:
        self.issues: tuple[RiskIssue, ...] = ()
        self.tenant_id: UUID | None = None

    async def issues_for_document(self, context, document_id: UUID):
        del document_id
        if self.tenant_id is None or context.tenant_id != self.tenant_id:
            return ()
        return self.issues


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


def _version(tenant_id: UUID) -> DocumentVersion:
    return DocumentVersion(
        id=new_uuid7(),
        tenant_id=tenant_id,
        document_id=new_uuid7(),
        version_no=1,
        kind=DocumentKind.ORIGINAL,
        object_key=f"tenant_{tenant_id.hex}/doc/lease.docx",
        sha256=bytes(32),
        upload_status=DocumentUploadStatus.ACCEPTED,
        file_name="租赁合同.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1024,
        uploaded_at=_NOW,
    )


def _issue(
    tenant_id: UUID,
    *,
    status: RiskIssueStatus = RiskIssueStatus.OPEN,
    reason: str | None = None,
) -> RiskIssue:
    rule_id = new_uuid7()
    return RiskIssue(
        id=new_uuid7(),
        tenant_id=tenant_id,
        rule_id=rule_id,
        pack_id=new_uuid7(),
        pack_version=1,
        document_id=new_uuid7(),
        provision_no="第十条",
        matched_text="违约金为百分之三十",
        risk_level=RiskLevel.HIGH,
        status=status,
        evidence_level="rule_based",
        disposition_reason=reason,
        raised_at=_NOW,
        disposed_at=_NOW if status is not RiskIssueStatus.OPEN else None,
    )


def _service(documents: FakeDocumentStore, issues: FakeIssueStore) -> RiskReportService:
    return RiskReportService(
        documents=documents,
        issues=issues,
        writer=ZipDocxReportWriter(),
    )


def _read_back(payload: bytes) -> list[str]:
    loader = ZipDocxLoader(source_ref="object://tenant/report.docx")
    document = loader.load("object://tenant/report.docx", payload)
    return [paragraph.text for paragraph in document.paragraphs]


def _export(service, *, tenant_id: UUID, document_id: UUID) -> bytes:
    return asyncio.run(
        service.export(
            context=_context(tenant_id),
            document_id=document_id,
            now=_NOW,
        )
    )


def test_export_contains_disclaimer_and_issue_listing() -> None:
    tenant_id = new_uuid7()
    documents = FakeDocumentStore()
    documents.version = _version(tenant_id)
    issues = FakeIssueStore()
    issues.tenant_id = tenant_id
    issues.issues = (_issue(tenant_id),)
    lines = _read_back(
        _export(
            _service(documents, issues),
            tenant_id=tenant_id,
            document_id=documents.version.document_id,
        )
    )
    joined = "\n".join(lines)
    assert _DISCLAIMER_FRAGMENT in joined
    assert _DISCLAIMER_LEGAL in joined
    # Open issues are labelled as awaiting lawyer review, never as confirmed.
    assert "待人工核验" in joined
    assert "违约金为百分之三十" in joined
    assert "第十条" in joined


def test_export_lists_disposed_issue_with_reason() -> None:
    tenant_id = new_uuid7()
    documents = FakeDocumentStore()
    documents.version = _version(tenant_id)
    issues = FakeIssueStore()
    issues.tenant_id = tenant_id
    issues.issues = (
        _issue(tenant_id, status=RiskIssueStatus.ACCEPTED, reason="律师确认可接受"),
    )
    lines = _read_back(
        _export(
            _service(documents, issues),
            tenant_id=tenant_id,
            document_id=documents.version.document_id,
        )
    )
    joined = "\n".join(lines)
    assert "律师确认可接受" in joined
    assert "已接受" in joined


def test_export_stale_issue_is_labeled() -> None:
    tenant_id = new_uuid7()
    documents = FakeDocumentStore()
    documents.version = _version(tenant_id)
    issues = FakeIssueStore()
    issues.tenant_id = tenant_id
    issues.issues = (_issue(tenant_id, status=RiskIssueStatus.STALE),)
    lines = _read_back(
        _export(
            _service(documents, issues),
            tenant_id=tenant_id,
            document_id=documents.version.document_id,
        )
    )
    assert "已随文本变更失效" in "\n".join(lines)


def test_export_no_hits_says_needs_review_not_no_risk() -> None:
    tenant_id = new_uuid7()
    documents = FakeDocumentStore()
    documents.version = _version(tenant_id)
    issues = FakeIssueStore()
    issues.tenant_id = tenant_id
    issues.issues = ()
    lines = _read_back(
        _export(
            _service(documents, issues),
            tenant_id=tenant_id,
            document_id=documents.version.document_id,
        )
    )
    joined = "\n".join(lines)
    assert "未发现规则命中" in joined
    assert "人工核验" in joined
    assert "无风险" not in joined


def test_export_unknown_document_is_rejected() -> None:
    tenant_id = new_uuid7()
    documents = FakeDocumentStore()
    documents.version = _version(tenant_id)
    issues = FakeIssueStore()
    issues.tenant_id = tenant_id
    with pytest.raises(ValueError, match="document"):
        _export(
            _service(documents, issues),
            tenant_id=tenant_id,
            document_id=new_uuid7(),
        )


def test_export_never_leaks_other_tenant_issues() -> None:
    tenant_a = new_uuid7()
    tenant_b = new_uuid7()
    documents = FakeDocumentStore()
    documents.version = _version(tenant_a)
    issues = FakeIssueStore()
    issues.tenant_id = tenant_b  # issues belong to another tenant
    issues.issues = (_issue(tenant_b),)
    lines = _read_back(
        _export(
            _service(documents, issues),
            tenant_id=tenant_a,
            document_id=documents.version.document_id,
        )
    )
    joined = "\n".join(lines)
    assert "违约金为百分之三十" not in joined
