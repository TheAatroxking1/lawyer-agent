"""Assemble a tenant-scoped DOCX working report of rule check results.

The report consumes only stored data (document metadata plus RiskIssue rows and
their lawyer dispositions). It is a working draft: it states that findings are
rule-based candidates, never claims to be legal advice, never auto-concludes a
limitation or validity question, and labels every un-disposed finding as
awaiting human review.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.matter_documents import DocumentVersion
from lawyer_agent.domain.rule_pack import RiskIssue, RiskIssueStatus
from lawyer_agent.infrastructure.documents.docx_writer import (
    ReportSegment,
    ReportSegmentKind,
    ZipDocxReportWriter,
)

_DISCLAIMER = (
    "本报告为规则候选提示（evidence_level=rule_based），非法律意见；"
    "所列风险与建议均需律师/法务人工核验后方可使用。"
)
_NO_HITS = "未发现规则命中（仍需人工核验，不代表检查已经完成或不存在其他风险）。"


class TenantScoped(Protocol):
    tenant_id: UUID


class ReportDocumentPort(Protocol):
    async def find_version(
        self, tenant_id: UUID, document_id: UUID
    ) -> DocumentVersion | None: ...


class ReportIssuePort(Protocol):
    async def issues_for_document(
        self, context: TenantScoped, document_id: UUID
    ) -> tuple[RiskIssue, ...]: ...


class RiskReportService:
    """Builds a DOCX working report for one tenant document."""

    def __init__(
        self,
        *,
        documents: ReportDocumentPort,
        issues: ReportIssuePort,
        writer: ZipDocxReportWriter | None = None,
    ) -> None:
        self._documents = documents
        self._issues = issues
        self._writer = writer or ZipDocxReportWriter()

    async def export(
        self,
        *,
        context: TenantScoped,
        document_id: UUID,
        now: datetime,
    ) -> bytes:
        version = await self._documents.find_version(context.tenant_id, document_id)
        if version is None:
            raise ValueError("document is not available for this tenant")
        risk_issues = await self._issues.issues_for_document(context, document_id)
        segments = self._segments(version, risk_issues, now)
        return self._writer.write(segments)

    def _segments(
        self,
        version: DocumentVersion,
        issues: tuple[RiskIssue, ...],
        now: datetime,
    ) -> tuple[ReportSegment, ...]:
        display_name = version.file_name or version.object_key
        segments: list[ReportSegment] = [
            ReportSegment(ReportSegmentKind.TITLE, "合同规则检查工作报告（草稿）"),
            ReportSegment(
                ReportSegmentKind.PARAGRAPH,
                f"文档：{display_name}    生成时间：{now.isoformat()}",
            ),
            ReportSegment(ReportSegmentKind.NOTE, _DISCLAIMER),
        ]
        if not issues:
            segments.append(ReportSegment(ReportSegmentKind.PARAGRAPH, _NO_HITS))
            return tuple(segments)
        segments.append(ReportSegment(ReportSegmentKind.HEADING, "风险清单"))
        for issue in _sorted_issues(issues):
            segments.append(
                ReportSegment(
                    ReportSegmentKind.LIST_ITEM,
                    _issue_line(issue),
                )
            )
        segments.append(
            ReportSegment(
                ReportSegmentKind.NOTE,
                "高风险建议未经律师/法务确认不得作为对外结论；"
                "未处置或被驳回的条目不视为已确认。",
            )
        )
        return tuple(segments)


def _sorted_issues(issues: tuple[RiskIssue, ...]) -> tuple[RiskIssue, ...]:
    return tuple(
        sorted(
            issues,
            key=lambda issue: (issue.provision_no, issue.rule_id.hex),
        )
    )


def _issue_line(issue: RiskIssue) -> str:
    status_label, reason = _status_text(issue)
    parts = [
        f"{issue.provision_no}｜命中：{issue.matched_text}",
        f"风险级别：{issue.risk_level.value}｜{status_label}",
    ]
    if reason:
        parts.append(f"处置理由：{reason}")
    return "；".join(parts)


def _status_text(issue: RiskIssue) -> tuple[str, str | None]:
    status = issue.status
    if status is RiskIssueStatus.OPEN:
        return "待人工核验", None
    if status is RiskIssueStatus.ACCEPTED:
        return "已接受", issue.disposition_reason
    if status is RiskIssueStatus.REJECTED:
        return "已驳回", issue.disposition_reason
    if status is RiskIssueStatus.MODIFIED:
        return "已修改（需重跑核验）", issue.disposition_reason
    if status is RiskIssueStatus.STALE:
        return "已随文本变更失效", None
    return "未知状态", None
