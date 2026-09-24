"""Tenant-scoped MySQL persistence for synchronous contract review runs."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import and_, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lawyer_agent.application.contract_review.contracts import ReviewError
from lawyer_agent.application.contract_review.web import ContractRunPage, ContractRunSummary
from lawyer_agent.application.tenancy import TenantActor
from lawyer_agent.domain.common import new_uuid7, require_uuid7
from lawyer_agent.infrastructure.persistence.history_cursor import decode_cursor, encode_cursor
from lawyer_agent.infrastructure.persistence.models import (
    AuditEventModel,
    AuthSessionModel,
    ContractReviewRunModel,
    MembershipRoleAssignmentModel,
    PermissionModel,
    TenantMembershipModel,
    TenantModel,
    TenantRoleModel,
    TenantRolePermissionModel,
    UserModel,
)

_HEX = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = frozenset({"draft", "no_evidence", "failed", "cancelled"})
_EVIDENCE_KEYS = frozenset(
    {
        "document_id",
        "title",
        "version",
        "content_hash",
        "source_ref",
        "quality_flags",
        "legal_metadata",
    }
)
_MODEL_CALL_KEYS = frozenset({"operation", "model_ref", "status", "latency_ms", "usage"})
_MCP_TOOL_KEYS = frozenset({"name", "capability", "description", "status"})


@dataclass(frozen=True, slots=True)
class FailureLease:
    tenant_id: UUID
    run_id: UUID
    token: SecretStr = field(repr=False)


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: UUID
    tenant_id: UUID
    created_by_user_id: UUID
    created_by_membership_id: UUID
    filename: str
    instruction: str
    as_of: date
    status: str
    pdf_object_key: str | None
    pdf_sha256: str | None
    pdf_size: int | None
    parsed_document: dict[str, Any] | None
    result: dict[str, Any] | None
    evidence_manifest: dict[str, Any] | None
    failure_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    failure_lease: FailureLease | None = field(default=None, repr=False)


class ContractRunRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        now: datetime | None = None,
    ) -> None:
        if not callable(session_factory):
            raise ValueError("session_factory_required")
        self._sessions = session_factory
        self._now = now

    def _clock(self) -> datetime:
        current = self._now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("utc_clock_required")
        return current

    async def create(
        self,
        actor: TenantActor,
        filename: str,
        instruction: str,
        as_of: date,
        idempotency_key: str,
    ) -> RunRecord:
        filename = filename.strip()
        instruction = instruction.strip()
        if (
            not filename
            or len(filename.encode()) > 255
            or not instruction
            or len(instruction.encode()) > 16_384
            or not isinstance(as_of, date)
            or not isinstance(idempotency_key, str)
            or not 1 <= len(idempotency_key.encode()) <= 512
        ):
            raise ValueError("invalid_contract_review_request")
        key_hash = hashlib.sha256(idempotency_key.encode()).digest()
        fingerprint = _digest_json(
            {"filename": filename, "instruction": instruction, "as_of": as_of.isoformat()}
        )
        now = self._clock()
        async with self._sessions() as session, session.begin():
            tenant_id, user_id, membership_id = await self._authorize(
                session, actor, permission="ai_job.create", now=now
            )
            existing = await session.scalar(
                select(ContractReviewRunModel).where(
                    ContractReviewRunModel.tenant_id == tenant_id,
                    ContractReviewRunModel.created_by_membership_id == membership_id,
                    ContractReviewRunModel.idempotency_hash == key_hash,
                )
            )
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise ReviewError("contract_run_conflict")
                return _record(existing)
            model = ContractReviewRunModel(
                id=new_uuid7(),
                tenant_id=tenant_id,
                created_by_user_id=user_id,
                created_by_membership_id=membership_id,
                idempotency_hash=key_hash,
                request_fingerprint=fingerprint,
                filename=filename,
                instruction=instruction,
                as_of=as_of,
                status="awaiting_upload",
                created_at=_naive(now),
                updated_at=_naive(now),
            )
            session.add(model)
            await session.flush()
            await session.refresh(model)
            self._audit(session, actor, model.id, "contract_review.create", now)
            return _record(model)

    async def get(self, actor: TenantActor, run_id: UUID) -> RunRecord:
        return await self.authorize(actor, run_id, write=False)

    async def list_owned(
        self, actor: TenantActor, *, limit: int = 30, cursor: str | None = None,
    ) -> ContractRunPage:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ReviewError("invalid_review_request")
        async with self._sessions() as session, session.begin():
            tenant_id, user_id, membership_id = await self._authorize(
                session, actor, permission="ai_job.read", now=self._clock(),
            )
            try:
                after = decode_cursor(cursor, tenant_id, user_id)
            except ValueError:
                raise ReviewError("invalid_review_request") from None
            # Scalar projection avoids loading PDF parse results/evidence JSON for a sidebar.
            query = select(
                ContractReviewRunModel.id, ContractReviewRunModel.filename,
                ContractReviewRunModel.status, ContractReviewRunModel.created_at,
                ContractReviewRunModel.updated_at,
            ).where(
                ContractReviewRunModel.tenant_id == tenant_id,
                ContractReviewRunModel.created_by_user_id == user_id,
                ContractReviewRunModel.created_by_membership_id == membership_id,
            )
            if after:
                at, ident = after
                query = query.where(or_(
                    ContractReviewRunModel.updated_at < at,
                    and_(
                        ContractReviewRunModel.updated_at == at, ContractReviewRunModel.id < ident,
                    ),
                ))
            query = query.order_by(
                ContractReviewRunModel.updated_at.desc(), ContractReviewRunModel.id.desc(),
            ).limit(limit + 1)
            rows = list((await session.execute(query)).all())
            page = rows[:limit]
            next_cursor = (
                encode_cursor(tenant_id, user_id, page[-1].updated_at, page[-1].id)
                if len(rows) > limit else None
            )
            return ContractRunPage(
                items=tuple(ContractRunSummary(
                    id=r.id, file_name=r.filename, status=r.status,
                    created_at=_aware(r.created_at), updated_at=_aware(r.updated_at),
                ) for r in page), next_cursor=next_cursor,
            )

    async def authorize(
        self, actor: TenantActor, run_id: UUID, *, write: bool = False
    ) -> RunRecord:
        require_uuid7(run_id, field="run_id")
        now = self._clock()
        async with self._sessions() as session, session.begin():
            tenant_id, user_id, membership_id = await self._authorize(
                session,
                actor,
                permission="ai_job.create" if write else "ai_job.read",
                now=now,
            )
            model = await self._owned(session, tenant_id, user_id, membership_id, run_id)
            return _record(model)

    async def attach_pdf(
        self,
        actor: TenantActor,
        run_id: UUID,
        key: str,
        digest: str,
        size: int,
    ) -> RunRecord:
        if (
            not isinstance(key, str)
            or not 1 <= len(key.encode()) <= 1024
            or _HEX.fullmatch(digest) is None
            or type(size) is not int
            or not 1 <= size <= 52_428_800
        ):
            raise ValueError("invalid_pdf_identity")
        return await self._transition(
            actor,
            run_id,
            expected="awaiting_upload",
            target="uploaded",
            values={"pdf_object_key": key, "pdf_sha256": bytes.fromhex(digest), "pdf_size": size},
            replay=lambda row: row.status == "uploaded"
            and row.pdf_object_key == key
            and row.pdf_sha256 == bytes.fromhex(digest)
            and row.pdf_size == size,
            action="contract_review.attach_pdf",
        )

    async def claim(self, actor: TenantActor, run_id: UUID) -> RunRecord:
        token = SecretStr(secrets.token_hex(32))
        lease = FailureLease(actor.context.tenant_id, run_id, token)
        record = await self._transition(
            actor,
            run_id,
            expected="uploaded",
            target="running",
            values={
                "failure_lease_hash": _failure_lease_digest(lease),
                "failure_lease_expires_at": _naive(self._clock() + timedelta(minutes=15)),
            },
            replay=lambda _row: False,
            action="contract_review.claim",
        )
        return replace(record, failure_lease=lease)

    async def finish(
        self,
        actor: TenantActor,
        run_id: UUID,
        *,
        status: Literal["draft", "no_evidence"],
        result: dict[str, Any],
        parsed_document: dict[str, Any],
        evidence_manifest: dict[str, Any],
    ) -> RunRecord:
        if status not in {"draft", "no_evidence"}:
            raise ValueError("invalid_terminal_status")
        _validate_finish_payloads(result, parsed_document, evidence_manifest)
        canonical = {
            "result_json": result,
            "parsed_document": parsed_document,
            "evidence_manifest": evidence_manifest,
            "failure_code": None,
            "failure_lease_hash": None,
            "failure_lease_expires_at": None,
        }
        return await self._transition(
            actor,
            run_id,
            expected="running",
            target=status,
            values=canonical,
            replay=lambda row: row.status == status
            and _digest_json(row.result_json) == _digest_json(result)
            and _digest_json(row.parsed_document) == _digest_json(parsed_document)
            and _digest_json(row.evidence_manifest) == _digest_json(evidence_manifest),
            action="contract_review.finish",
            validate=lambda row: _validate_finish_identity(
                run_id, row, status, result, parsed_document
            ),
        )

    async def fail(
        self, actor: TenantActor, run_id: UUID, code: str, *, cancelled: bool = False
    ) -> RunRecord:
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_.-]{1,64}", code):
            raise ValueError("invalid_failure_code")
        target = "cancelled" if cancelled else "failed"
        return await self._transition(
            actor,
            run_id,
            expected="running",
            target=target,
            values={
                "failure_code": code,
                "failure_lease_hash": None,
                "failure_lease_expires_at": None,
            },
            replay=lambda row: row.status == target and row.failure_code == code,
            action="contract_review.fail",
        )

    async def fail_owned(
        self,
        lease: FailureLease,
        code: str,
        *,
        cancelled: bool = False,
        evidence_manifest: dict[str, Any] | None = None,
    ) -> None:
        if not isinstance(lease, FailureLease):
            raise ReviewError("resource_unavailable")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_.-]{1,64}", code):
            raise ValueError("invalid_failure_code")
        if evidence_manifest is not None:
            _validate_failure_manifest(evidence_manifest)
        now = self._clock()
        target = "cancelled" if cancelled else "failed"
        async with self._sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(
                        ContractReviewRunModel.id,
                        ContractReviewRunModel.tenant_id,
                        ContractReviewRunModel.status,
                        ContractReviewRunModel.failure_lease_hash,
                        ContractReviewRunModel.failure_lease_expires_at,
                        ContractReviewRunModel.version,
                    ).where(
                    ContractReviewRunModel.tenant_id == lease.tenant_id,
                    ContractReviewRunModel.id == lease.run_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ReviewError("resource_unavailable")
            if row.status in {"failed", "cancelled"} and row.failure_lease_hash is None:
                return None
            if row.failure_lease_hash is None or row.failure_lease_expires_at is None:
                raise ReviewError("resource_unavailable")
            _validate_failure_lease(
                lease,
                tenant_id=row.tenant_id,
                run_id=row.id,
                digest=row.failure_lease_hash,
                expires_at=_aware(row.failure_lease_expires_at),
                now=now,
            )
            if row.status != "running":
                raise ReviewError("resource_unavailable")
            digest = _failure_lease_digest(lease)
            changed = cast(
                CursorResult[Any],
                await session.execute(
                    update(ContractReviewRunModel)
                    .where(
                        ContractReviewRunModel.tenant_id == lease.tenant_id,
                        ContractReviewRunModel.id == lease.run_id,
                        ContractReviewRunModel.status == "running",
                        ContractReviewRunModel.version == row.version,
                        ContractReviewRunModel.failure_lease_hash == digest,
                        ContractReviewRunModel.failure_lease_expires_at > _naive(now),
                    )
                    .values(
                        status=target,
                        failure_code=code,
                        evidence_manifest=evidence_manifest,
                        failure_lease_hash=None,
                        failure_lease_expires_at=None,
                        version=ContractReviewRunModel.version + 1,
                        updated_at=_naive(now),
                    )
                ),
            )
            if changed.rowcount != 1:
                raise ReviewError("resource_unavailable")
            self._audit_internal(
                session,
                tenant_id=row.tenant_id,
                run_id=row.id,
                cancelled=cancelled,
                now=now,
            )
            return None

    async def _transition(
        self,
        actor: TenantActor,
        run_id: UUID,
        *,
        expected: str,
        target: str,
        values: dict[str, Any],
        replay: Any,
        action: str,
        validate: Any = None,
    ) -> RunRecord:
        require_uuid7(run_id, field="run_id")
        now = self._clock()
        async with self._sessions() as session, session.begin():
            tenant_id, user_id, membership_id = await self._authorize(
                session, actor, permission="ai_job.create", now=now
            )
            row = await self._owned(session, tenant_id, user_id, membership_id, run_id)
            if validate is not None:
                validate(row)
            if row.status != expected:
                if replay(row):
                    return _record(row)
                raise ReviewError("contract_run_conflict")
            changed = cast(
                CursorResult[Any],
                await session.execute(
                    update(ContractReviewRunModel)
                    .where(
                        ContractReviewRunModel.tenant_id == tenant_id,
                        ContractReviewRunModel.id == run_id,
                        ContractReviewRunModel.version == row.version,
                        ContractReviewRunModel.status == expected,
                    )
                    .values(
                        **values,
                        status=target,
                        version=ContractReviewRunModel.version + 1,
                        updated_at=_naive(now),
                    )
                ),
            )
            if changed.rowcount != 1:
                raise ReviewError("contract_run_conflict")
            updated = await self._owned(session, tenant_id, user_id, membership_id, run_id)
            self._audit(session, actor, run_id, action, now)
            return _record(updated)

    async def _authorize(
        self,
        session: AsyncSession,
        actor: TenantActor,
        *,
        permission: str,
        now: datetime,
    ) -> tuple[UUID, UUID, UUID]:
        try:
            tenant_id = actor.context.tenant_id
            user_id = actor.principal.user_id
            membership_id = actor.context.membership_id
            if membership_id is None:
                raise ValueError
            user = await session.scalar(select(UserModel).where(UserModel.id == user_id))
            tenant = await session.scalar(select(TenantModel).where(TenantModel.id == tenant_id))
            auth_session = await session.scalar(
                select(AuthSessionModel).where(
                    AuthSessionModel.id == actor.principal.session_id,
                    AuthSessionModel.user_id == user_id,
                )
            )
            member = await session.scalar(
                select(TenantMembershipModel).where(
                    TenantMembershipModel.tenant_id == tenant_id,
                    TenantMembershipModel.id == membership_id,
                    TenantMembershipModel.user_id == user_id,
                )
            )
            if (
                user is None
                or tenant is None
                or member is None
                or auth_session is None
                or user.status != "active"
                or tenant.status != "active"
                or member.status != "active"
                or actor.principal.audience.value != "tenant"
                or actor.principal.session_valid is not True
                or auth_session.revoked_at is not None
                or _aware(auth_session.expires_at) <= now
                or _aware(member.valid_from) > now
                or (member.valid_until is not None and _aware(member.valid_until) <= now)
                or actor.principal.auth_version != user.auth_version
                or actor.principal.session_auth_version != user.auth_version
                or auth_session.auth_version_at_issue != user.auth_version
                or (auth_session.tenant_id, auth_session.membership_id,
                    auth_session.authz_version_at_issue) not in {
                        (None, None, None), (tenant_id, membership_id, member.authz_version),
                    }
                or actor.context.authz_version != member.authz_version
                or actor.context.session_authz_version != member.authz_version
                or actor.principal.tenant_id != tenant_id
                or actor.principal.membership_id != membership_id
            ):
                raise ValueError
            permissions = set(
                await session.scalars(
                    select(PermissionModel.code)
                    .join(
                        TenantRolePermissionModel,
                        TenantRolePermissionModel.permission_id == PermissionModel.id,
                    )
                    .join(
                        TenantRoleModel,
                        (TenantRoleModel.tenant_id == TenantRolePermissionModel.tenant_id)
                        & (TenantRoleModel.id == TenantRolePermissionModel.tenant_role_id),
                    )
                    .join(
                        MembershipRoleAssignmentModel,
                        (MembershipRoleAssignmentModel.tenant_id == TenantRoleModel.tenant_id)
                        & (MembershipRoleAssignmentModel.tenant_role_id == TenantRoleModel.id),
                    )
                    .where(
                        MembershipRoleAssignmentModel.tenant_id == tenant_id,
                        MembershipRoleAssignmentModel.membership_id == membership_id,
                        TenantRoleModel.status == "active",
                        PermissionModel.status == "active",
                    )
                )
            )
            if permission not in permissions:
                raise ValueError
            return tenant_id, user_id, membership_id
        except (AttributeError, TypeError, ValueError):
            raise ReviewError("resource_unavailable") from None

    @staticmethod
    async def _owned(
        session: AsyncSession,
        tenant_id: UUID,
        user_id: UUID,
        membership_id: UUID,
        run_id: UUID,
    ) -> ContractReviewRunModel:
        model = await session.scalar(
            select(ContractReviewRunModel).where(
                ContractReviewRunModel.tenant_id == tenant_id,
                ContractReviewRunModel.id == run_id,
                ContractReviewRunModel.created_by_user_id == user_id,
                ContractReviewRunModel.created_by_membership_id == membership_id,
            )
        )
        if model is None:
            raise ReviewError("resource_unavailable")
        return model

    @staticmethod
    def _audit(
        session: AsyncSession,
        actor: TenantActor,
        run_id: UUID,
        action: str,
        now: datetime,
    ) -> None:
        session.add(
            AuditEventModel(
                id=new_uuid7(),
                actor_user_id=actor.principal.user_id,
                tenant_id=actor.context.tenant_id,
                actor_membership_id=actor.context.membership_id,
                actor_kind="tenant_user",
                action=action,
                result="success",
                reason_code="ok",
                target_type="contract_review",
                target_id=run_id,
                trace_id=str(run_id),
                metadata_json=None,
                occurred_at=_naive(now),
            )
        )

    @staticmethod
    def _audit_internal(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        run_id: UUID,
        cancelled: bool,
        now: datetime,
    ) -> None:
        session.add(
            AuditEventModel(
                id=new_uuid7(),
                actor_user_id=None,
                tenant_id=tenant_id,
                actor_membership_id=None,
                actor_kind=None,
                action=(
                    "contract_review.system_cancel"
                    if cancelled
                    else "contract_review.system_fail"
                ),
                result="success",
                reason_code="failure_lease_cleanup",
                target_type="contract_review",
                target_id=run_id,
                trace_id=str(run_id),
                metadata_json=None,
                occurred_at=_naive(now),
            )
        )


def _validate_finish_payloads(
    result: dict[str, Any],
    parsed_document: dict[str, Any],
    evidence_manifest: dict[str, Any],
) -> None:
    if not isinstance(result, dict) or _json_size(result) > 2_097_152:
        raise ValueError("result_too_large")
    if not isinstance(parsed_document, dict) or _json_size(parsed_document) > 2_097_152:
        raise ValueError("parsed_document_too_large")
    blocks = parsed_document.get("blocks")
    dimensions = parsed_document.get("page_dimensions")
    if (
        _HEX.fullmatch(str(parsed_document.get("document_version_id"))) is None
        or not isinstance(blocks, list)
        or not isinstance(dimensions, list)
        or len(blocks) > 20_000
        or len(dimensions) > 10_000
    ):
        raise ValueError("parsed_document_invalid")
    _validate_manifest_keys(evidence_manifest)
    if "verification_report" in evidence_manifest:
        from lawyer_agent.application.contract_review.contracts import DocumentVerificationReport

        try:
            DocumentVerificationReport.model_validate(evidence_manifest["verification_report"])
        except ValueError:
            raise ValueError("evidence_manifest_invalid") from None
    documents = evidence_manifest["documents"]
    model_calls = evidence_manifest["model_calls"]
    if (
        not isinstance(documents, list)
        or len(documents) > 7
        or not isinstance(model_calls, list)
        or len(model_calls) > 100
        or _json_size(evidence_manifest) > 262_144
    ):
        raise ValueError("evidence_manifest_invalid")
    _validate_mcp_tools(evidence_manifest.get("mcp_tools", []))
    seen_documents: set[str] = set()
    for item in documents:
        if (
            not isinstance(item, dict)
            or set(item) != _EVIDENCE_KEYS
            or _HEX.fullmatch(str(item.get("document_id"))) is None
            or _HEX.fullmatch(str(item.get("content_hash"))) is None
            or _json_size(item) > 16_384
        ):
            raise ValueError("evidence_manifest_invalid")
        if item["document_id"] in seen_documents:
            raise ValueError("evidence_manifest_invalid")
        seen_documents.add(item["document_id"])
    for item in model_calls:
        if not isinstance(item, dict) or set(item) != _MODEL_CALL_KEYS or _json_size(item) > 4096:
            raise ValueError("evidence_manifest_invalid")


def _validate_failure_manifest(value: dict[str, Any]) -> None:
    from lawyer_agent.application.contract_review.contracts import DocumentVerificationReport

    _validate_manifest_keys(value)
    if "verification_report" in value:
        try:
            DocumentVerificationReport.model_validate(value["verification_report"])
        except ValueError:
            raise ValueError("evidence_manifest_invalid") from None
    if value["documents"] != [] or not isinstance(value["model_calls"], list):
        raise ValueError("evidence_manifest_invalid")
    if len(value["model_calls"]) > 100 or _json_size(value) > 262_144:
        raise ValueError("evidence_manifest_invalid")
    _validate_mcp_tools(value.get("mcp_tools", []))
    for item in value["model_calls"]:
        if not isinstance(item, dict) or set(item) != _MODEL_CALL_KEYS or _json_size(item) > 4096:
            raise ValueError("evidence_manifest_invalid")


def _validate_manifest_keys(value: dict[str, Any]) -> None:
    from lawyer_agent.application.contract_review.contracts import RetrievalCallReceipt

    required = {"documents", "model_calls"}
    if (not isinstance(value, dict) or not required <= set(value)
        or set(value) - required - {"verification_report", "retrieval_calls", "mcp_tools"}):
        raise ValueError("evidence_manifest_invalid")
    calls = value.get("retrieval_calls", [])
    if not isinstance(calls, list) or len(calls) > 7:
        raise ValueError("evidence_manifest_invalid")
    try:
        for item in calls:
            RetrievalCallReceipt.model_validate(item)
    except ValueError:
        raise ValueError("evidence_manifest_invalid") from None


def _validate_mcp_tools(value: Any) -> None:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("evidence_manifest_invalid")
    for item in value:
        if (
            not isinstance(item, dict)
            or set(item) != _MCP_TOOL_KEYS
            or not isinstance(item["name"], str)
            or not 1 <= len(item["name"]) <= 120
            or not isinstance(item["capability"], str)
            or not 1 <= len(item["capability"]) <= 120
            or not isinstance(item["description"], str)
            or not 1 <= len(item["description"]) <= 500
            or item["status"] not in {"discovered", "authorized"}
            or _json_size(item) > 2048
        ):
            raise ValueError("evidence_manifest_invalid")


def _failure_lease_digest(lease: FailureLease) -> bytes:
    if not isinstance(lease.token, SecretStr):
        raise ValueError("invalid_failure_lease")
    token = lease.token.get_secret_value()
    if _HEX.fullmatch(token) is None:
        raise ValueError("invalid_failure_lease")
    material = lease.tenant_id.bytes + lease.run_id.bytes + bytes.fromhex(token)
    return hashlib.sha256(material).digest()


def _validate_failure_lease(
    lease: FailureLease,
    *,
    tenant_id: UUID,
    run_id: UUID,
    digest: bytes,
    expires_at: datetime,
    now: datetime,
) -> None:
    try:
        candidate = _failure_lease_digest(lease)
        valid = (
            lease.tenant_id == tenant_id
            and lease.run_id == run_id
            and len(digest) == 32
            and secrets.compare_digest(candidate, digest)
            and _aware(expires_at) > _aware(now)
        )
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ReviewError("resource_unavailable")


def _validate_finish_identity(
    run_id: UUID,
    row: ContractReviewRunModel,
    status: str,
    result: dict[str, Any],
    parsed_document: dict[str, Any],
) -> None:
    version = parsed_document.get("document_version_id")
    if row.pdf_sha256 is None or version != row.pdf_sha256.hex():
        raise ReviewError("contract_run_conflict")
    if status == "draft" and (
        str(result.get("run_id")) != str(run_id)
        or result.get("document_version_id") != version
    ):
        raise ReviewError("contract_run_conflict")


def _json_size(value: Any) -> int:
    try:
        payload = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return len(payload.encode())
    except (TypeError, ValueError):
        raise ValueError("json_payload_invalid") from None


def _digest_json(value: Any) -> bytes:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).digest()


def _record(model: ContractReviewRunModel) -> RunRecord:
    return RunRecord(
        id=model.id,
        tenant_id=model.tenant_id,
        created_by_user_id=model.created_by_user_id,
        created_by_membership_id=model.created_by_membership_id,
        filename=model.filename,
        instruction=model.instruction,
        as_of=model.as_of,
        status=model.status,
        pdf_object_key=model.pdf_object_key,
        pdf_sha256=None if model.pdf_sha256 is None else model.pdf_sha256.hex(),
        pdf_size=model.pdf_size,
        parsed_document=model.parsed_document,
        result=model.result_json,
        evidence_manifest=model.evidence_manifest,
        failure_code=model.failure_code,
        version=model.version,
        created_at=_aware(model.created_at),
        updated_at=_aware(model.updated_at),
    )


def _naive(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(tzinfo=None)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
