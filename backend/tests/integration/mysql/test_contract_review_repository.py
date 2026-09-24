from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import select, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.application.contract_review.contracts import (
    ReviewError,
    ReviewScope,
    VersionInput,
)
from lawyer_agent.application.tenancy import TenantActor
from lawyer_agent.domain.authorization import AuthorizationScope, Principal, PrincipalAudience
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.model_gateway import ModelCallRecord, ModelOperation, TokenUsage
from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus
from lawyer_agent.infrastructure.contract_review.pdf import PdfDocumentService
from lawyer_agent.infrastructure.contract_review.runtime import (
    _model_call_payloads,
    _parsed_document,
    run_view,
)
from lawyer_agent.infrastructure.contract_review.storage import ContractRunRepository
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
from tests.unit.test_contract_review_runtime import _pdf

pytestmark = [pytest.mark.integration, pytest.mark.mysql]
NOW = datetime(2026, 9, 16, 8, tzinfo=UTC)


class _AllowDocument:
    async def require(self, scope: ReviewScope, action: str, resource_id: str) -> None:
        del scope, action, resource_id


@dataclass(frozen=True)
class SeededActor:
    actor: TenantActor
    session_id: UUID


def _config(mysql_url: URL) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    config.set_main_option(
        "sqlalchemy.url", mysql_url.render_as_string(hide_password=False).replace("%", "%%")
    )
    return config


def _actor(user_id: UUID, tenant_id: UUID, membership_id: UUID, session_id: UUID) -> TenantActor:
    return TenantActor(
        principal=Principal(
            user_id=user_id,
            session_id=session_id,
            audience=PrincipalAudience.TENANT,
            tenant_id=tenant_id,
            membership_id=membership_id,
            user_status="active",
            session_valid=True,
            auth_version=1,
            session_auth_version=1,
            permissions=frozenset(),
            role_codes=frozenset(),
            authenticated_at=NOW - timedelta(minutes=1),
        ),
        context=TenantContext(
            tenant_id=tenant_id,
            membership_id=membership_id,
            membership_user_id=user_id,
            department_id=None,
            tenant_status=TenantStatus.ACTIVE,
            membership_status=MembershipStatus.ACTIVE,
            valid_from=NOW - timedelta(days=1),
            valid_until=None,
            authz_version=1,
            session_authz_version=1,
            scope=AuthorizationScope(allow_tenant_wide=True),
        ),
    )


async def _seed_actor(
    session: AsyncSession, *, tenant_id: UUID | None = None, label: str, device: bool = False,
) -> SeededActor:
    user_id, membership_id, session_id = new_uuid7(), new_uuid7(), new_uuid7()
    actual_tenant_id = tenant_id or new_uuid7()
    session.add(UserModel(id=user_id, status="active", display_name=label, auth_version=1))
    await session.flush()
    if tenant_id is None:
        session.add(
            TenantModel(
                id=actual_tenant_id,
                name=label,
                normalized_name=f"{label}-{actual_tenant_id}",
                tenant_type="enterprise",
                status="active",
                created_by_user_id=user_id,
                review_status="approved",
            )
        )
        await session.flush()
    session.add(
        TenantMembershipModel(
            id=membership_id,
            tenant_id=actual_tenant_id,
            user_id=user_id,
            member_type="owner",
            status="active",
            valid_from=NOW - timedelta(days=1),
            authz_version=1,
        )
    )
    await session.flush()
    role_id = new_uuid7()
    session.add(
        TenantRoleModel(
            id=role_id,
            tenant_id=actual_tenant_id,
            code=f"reviewer-{membership_id}",
            name="Contract reviewer",
            is_custom=True,
            status="active",
        )
    )
    await session.flush()
    permission_ids = list(
        await session.scalars(
            select(PermissionModel.id).where(
                PermissionModel.code.in_(("ai_job.create", "ai_job.read"))
            )
        )
    )
    assert len(permission_ids) == 2
    for permission_id in permission_ids:
        session.add(
            TenantRolePermissionModel(
                tenant_id=actual_tenant_id,
                tenant_role_id=role_id,
                permission_id=permission_id,
            )
        )
    session.add(
        MembershipRoleAssignmentModel(
            tenant_id=actual_tenant_id,
            membership_id=membership_id,
            tenant_role_id=role_id,
            assigned_by_membership_id=membership_id,
        )
    )
    session.add(
        AuthSessionModel(
            id=session_id,
            user_id=user_id,
            tenant_id=None if device else actual_tenant_id,
            membership_id=None if device else membership_id,
            current_family_id=new_uuid7(),
            auth_version_at_issue=1,
            authz_version_at_issue=None if device else 1,
            last_seen_at=NOW,
            expires_at=NOW + timedelta(days=1),
        )
    )
    return SeededActor(
        _actor(user_id, actual_tenant_id, membership_id, session_id), session_id
    )


async def _exercise(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions.begin() as session:
            owner = await _seed_actor(session, label="owner")
            same_tenant = await _seed_actor(
                session, tenant_id=owner.actor.context.tenant_id, label="other-owner"
            )
            other_tenant = await _seed_actor(session, label="other-tenant")

        repository = ContractRunRepository(sessions, now=NOW)
        created = await repository.create(
            owner.actor, "contract.pdf", "请审查", date(2026, 9, 16), "idem-1"
        )
        assert created.status == "awaiting_upload"
        assert (await repository.get(owner.actor, created.id)).id == created.id
        for stranger in (same_tenant.actor, other_tenant.actor):
            with pytest.raises(ReviewError, match="resource_unavailable"):
                await repository.get(stranger, created.id)

        uploaded = await repository.attach_pdf(
            owner.actor,
            created.id,
            f"tenants/{created.tenant_id}/contract-reviews/{created.id}/{'a' * 64}.pdf",
            "a" * 64,
            128,
        )
        assert uploaded.status == "uploaded"
        running = await repository.claim(owner.actor, created.id)
        assert running.status == "running" and running.failure_lease is not None
        lease = running.failure_lease

        persisted = await repository.create(
            owner.actor, "runtime.pdf", "请审查", date(2026, 9, 16), "idem-runtime"
        )
        pdf_bytes = _pdf()
        document_version = "b" * 64
        persisted = await repository.attach_pdf(
            owner.actor,
            persisted.id,
            f"tenants/{persisted.tenant_id}/contract-reviews/{persisted.id}/{document_version}.pdf",
            document_version,
            len(pdf_bytes),
        )
        persisted = await repository.claim(owner.actor, persisted.id)
        scope = ReviewScope(
            tenant_id=persisted.tenant_id,
            actor_id=persisted.created_by_user_id,
            run_id=persisted.id,
            document_version_id=document_version,
        )
        pdf = PdfDocumentService(scope, pdf_bytes, _AllowDocument())
        await pdf.parse(scope, VersionInput(document_version_id=document_version))
        parsed_document = await _parsed_document(pdf, scope)
        model_calls = _model_call_payloads((ModelCallRecord(
            operation=ModelOperation.CHAT,
            model_ref="configured-model",
            status="success",
            latency_ms=3,
            usage=TokenUsage(prompt_tokens=2, completion_tokens=1, total_tokens=3),
            vector_count=None,
        ),))
        finished = await repository.finish(
            owner.actor,
            persisted.id,
            status="no_evidence",
            result={"status": "no_evidence", "message": "证据不足"},
            parsed_document=parsed_document,
            evidence_manifest={"documents": [], "model_calls": model_calls,
                               "verification_report": {
                                   "pages": [{"page": 1, "reasons": ["missing_text"]}],
                               }},
        )
        assert finished.status == "no_evidence"
        assert finished.parsed_document is not None
        assert finished.parsed_document["page_dimensions"] == [[200.0, 200.0]]
        assert run_view(await repository.get(owner.actor, finished.id)).verification_report.pages
        for stranger in (same_tenant.actor, other_tenant.actor):
            with pytest.raises(ReviewError, match="resource_unavailable"):
                await repository.get(stranger, finished.id)
        assert set(finished.evidence_manifest["model_calls"][0]) == {
            "operation", "model_ref", "status", "latency_ms", "usage"
        }
        await pdf.aclose()

        incomplete = await repository.create(
            owner.actor, "coverage.pdf", "请审查", date(2026, 9, 16), "idem-coverage",
        )
        await repository.attach_pdf(
            owner.actor, incomplete.id,
            f"tenants/{incomplete.tenant_id}/contract-reviews/{incomplete.id}/{'c' * 64}.pdf",
            "c" * 64, 128,
        )
        incomplete = await repository.claim(owner.actor, incomplete.id)
        assert incomplete.failure_lease is not None
        report = {"pages": [{"page": 23, "reasons": ["missing_text"]}]}
        await repository.fail_owned(
            incomplete.failure_lease, "document_incomplete",
            evidence_manifest={"documents": [], "model_calls": [], "verification_report": report},
        )
        restored = run_view(await repository.get(owner.actor, incomplete.id))
        assert restored.verification_report.model_dump(mode="json") == report
        assert restored.status == "failed" and not restored.issues
        for stranger in (same_tenant.actor, other_tenant.actor):
            with pytest.raises(ReviewError, match="resource_unavailable"):
                await repository.get(stranger, incomplete.id)

        async with sessions.begin() as session:
            await session.execute(
                update(AuthSessionModel)
                .where(AuthSessionModel.id == owner.session_id)
                .values(revoked_at=NOW, revocation_reason="logout")
            )
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await repository.get(owner.actor, created.id)
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await repository.finish(
                owner.actor,
                created.id,
                status="no_evidence",
                result={"status": "no_evidence", "message": "证据不足"},
                parsed_document={
                    "document_version_id": "a" * 64,
                    "page_dimensions": [[1, 1]],
                    "blocks": [],
                },
                evidence_manifest={"documents": [], "model_calls": []},
            )
        await repository.fail_owned(
            lease,
            "provider_unavailable",
            evidence_manifest={
                "documents": [],
                "model_calls": _model_call_payloads((ModelCallRecord(
                    operation=ModelOperation.CHAT,
                    model_ref="configured-model",
                    status="error",
                    latency_ms=10,
                    error_code="provider_unavailable",
                    usage=TokenUsage(
                        prompt_tokens=1, completion_tokens=0, total_tokens=1
                    ),
                    vector_count=None,
                ),)),
            },
        )
        async with sessions() as session:
            row = await session.get(ContractReviewRunModel, created.id)
            assert row is not None
            assert row.status == "failed"
            assert row.failure_lease_hash is None and row.failure_lease_expires_at is None
            audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.target_id == created.id,
                    AuditEventModel.action == "contract_review.system_fail",
                )
            )
            assert audit is not None
            assert audit.tenant_id == created.tenant_id
            assert audit.actor_kind is None
            assert audit.actor_user_id is None
            assert audit.actor_membership_id is None
            assert audit.reason_code == "failure_lease_cleanup"
            user_audit = await session.scalar(
                select(AuditEventModel).where(
                    AuditEventModel.target_id == created.id,
                    AuditEventModel.action == "contract_review.create",
                )
            )
            assert user_audit is not None
            assert user_audit.actor_kind == "tenant_user"
            assert user_audit.actor_user_id == created.created_by_user_id
            assert user_audit.actor_membership_id == created.created_by_membership_id
    finally:
        await engine.dispose()


def test_repository_owner_revocation_and_failure_capability(mysql_url: URL) -> None:
    command.upgrade(_config(mysql_url), "head")
    asyncio.run(_exercise(mysql_url))


async def _exercise_device_session(mysql_url: URL) -> None:
    from lawyer_agent.infrastructure.persistence.engine import create_engine_from_url

    engine = create_engine_from_url(mysql_url.render_as_string(hide_password=False))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions.begin() as session:
            owner = await _seed_actor(session, label="device-owner", device=True)
        repo = ContractRunRepository(sessions, now=NOW)
        created = await repo.create(owner.actor, "device.pdf", "请审查", NOW.date(), "device-idem")
        assert (await repo.get(owner.actor, created.id)).id == created.id
        for stale in (
            replace(owner.actor, context=replace(owner.actor.context, authz_version=2)),
            replace(owner.actor, context=replace(owner.actor.context, session_authz_version=2)),
            replace(owner.actor, principal=replace(owner.actor.principal, auth_version=2)),
            replace(owner.actor, principal=replace(
                owner.actor.principal, membership_id=new_uuid7(),
            )),
        ):
            with pytest.raises(ReviewError, match="resource_unavailable"):
                await repo.get(stale, created.id)
        async with sessions.begin() as session:
            await session.execute(update(AuthSessionModel).where(
                AuthSessionModel.id == owner.session_id,
            ).values(revoked_at=NOW.replace(tzinfo=None)))
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await repo.get(owner.actor, created.id)
    finally:
        await engine.dispose()


def test_repository_derived_device_token_preserves_authorization(mysql_url: URL) -> None:
    command.upgrade(_config(mysql_url), "head")
    asyncio.run(_exercise_device_session(mysql_url))
