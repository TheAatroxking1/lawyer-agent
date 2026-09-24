from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lawyer_agent.application.identity import AuditContext
from lawyer_agent.application.sessions import (
    InvalidSession,
    LockedRefreshToken,
    RefreshLockLocator,
    SessionLockLocator,
    SessionService,
    SessionValidationState,
    SwitchTenantCommand,
    TenantSessionState,
    UserSessionState,
)
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.sessions import AccessTokenClaims, Audience
from lawyer_agent.infrastructure.security.jwt_tokens import TokenService

NOW = datetime(2026, 9, 21, tzinfo=UTC)
AUDIT = AuditContext("remembered-session", None, None)


@pytest.fixture
def setup():
    user, session, family, tenant, member = (new_uuid7() for _ in range(5))
    state = SessionValidationState(
        session, user, None, None, family, 3, None, None,
        NOW + timedelta(days=365), "active", 3, None, None, None, None, None, None,
    )
    membership = TenantSessionState(
        tenant, "active", member, user, "active", NOW - timedelta(days=1), None, 7,
    )
    repo = SimpleNamespace(
        locate_session=AsyncMock(return_value=SessionLockLocator(session, family, user, None)),
        lock_session=AsyncMock(return_value=state),
        get_validation_state=AsyncMock(return_value=state),
        lock_tenant_context=AsyncMock(return_value=membership),
        get_tenant_context=AsyncMock(return_value=membership),
        flush=AsyncMock(),
    )
    uow = SimpleNamespace(sessions=repo, audit=SimpleNamespace(append=AsyncMock()),
                          security_locks=SimpleNamespace(
                              acquire_session_family=AsyncMock(return_value=True)))

    @asynccontextmanager
    async def factory():
        yield uow

    key = Ed25519PrivateKey.generate()
    tokens = TokenService(issuer="remembered-test", active_kid="test",
                          signing_keys={"test": key}, verification_keys={"test": key.public_key()})
    service = SessionService(uow_factory=factory, token_service=tokens,
                             refresh_hash_key=b"r" * 32, clock=lambda: NOW)
    return service, repo, state, membership, tokens


def tenant_token(state, member, tokens):
    return tokens.issue_tenant(AccessTokenClaims(
        user_id=state.user_id, session_id=state.session_id, token_id=new_uuid7(),
        audience=Audience.TENANT, issued_at=NOW, not_before=NOW,
        expires_at=NOW + timedelta(minutes=10), auth_version=3,
        tenant_id=member.tenant_id, membership_id=member.membership_id, authz_version=7,
    ))


async def test_derived_tenant_access_retains_account_session_and_short_expiry(setup):
    service, repo, state, member, tokens = setup
    encoded = await service.tenant_access(
        SwitchTenantCommand(state.session_id, member.tenant_id, member.membership_id),
        audit_context=AUDIT,
    )
    claims = tokens.verify(encoded, audience=Audience.TENANT, now=NOW)
    assert claims.session_id == state.session_id and claims.authz_version == 7
    assert claims.expires_at == NOW + timedelta(minutes=10)
    account = service._issue_access_from_state(state, NOW)
    assert (await service.validate_access(account, audience=Audience.ACCOUNT)).tenant_id is None
    validated = await service.validate_access(encoded, audience=Audience.TENANT)
    assert (validated.tenant_id, validated.membership_id) == (
        member.tenant_id, member.membership_id,
    )
    repo.get_tenant_context.assert_awaited_with(
        user_id=state.user_id, tenant_id=member.tenant_id, membership_id=member.membership_id,
    )


@pytest.mark.parametrize("change", [
    {"authz_version": 8}, {"membership_status": "revoked"},
    {"tenant_status": "suspended"}, {"valid_until": NOW},
    {"membership_user_id": new_uuid7()}, {"membership_id": new_uuid7()},
    {"tenant_id": new_uuid7()},
])
async def test_derived_access_rechecks_authoritative_membership(setup, change):
    service, repo, state, member, tokens = setup
    encoded = tenant_token(state, member, tokens)
    assert (await service.validate_access(encoded, audience=Audience.TENANT)).tenant_id
    repo.get_tenant_context.return_value = replace(member, **change)
    with pytest.raises(InvalidSession):
        await service.validate_access(encoded, audience=Audience.TENANT)


async def test_account_revocation_immediately_invalidates_derived_access(setup):
    service, repo, state, member, tokens = setup
    encoded = tenant_token(state, member, tokens)
    assert (await service.validate_access(encoded, audience=Audience.TENANT)).tenant_id
    repo.get_validation_state.return_value = replace(state, revoked_at=NOW)
    with pytest.raises(InvalidSession):
        await service.validate_access(encoded, audience=Audience.TENANT)


async def test_tenant_access_rejects_missing_or_mismatched_membership(setup):
    service, repo, state, member, _ = setup
    repo.lock_tenant_context.return_value = replace(member, membership_user_id=new_uuid7())
    with pytest.raises(InvalidSession):
        await service.tenant_access(
            SwitchTenantCommand(state.session_id, member.tenant_id, member.membership_id),
            audit_context=AUDIT,
        )


def test_new_device_session_has_365_day_refresh_and_idle_lifetime(setup):
    service, _, state, _, _ = setup
    material = service._new_session_material(
        user=UserSessionState(state.user_id, "active", 3), tenant=None, now=NOW,
    )
    expected = NOW + timedelta(days=365)
    assert material.session.expires_at == expected
    assert material.refresh_record.expires_at == expected
    assert material.refresh_record.idle_expires_at == expected


@pytest.mark.parametrize("consumer", ["tenant", "invitation"])
async def test_authoritative_tenant_consumers_allow_device_session_but_reject_stale_claims(
    setup, consumer,
):
    from lawyer_agent.application.invitations import (
        InvitationAuthorizationDenied,
        InvitationService,
    )
    from lawyer_agent.application.tenancy import (
        ActorSessionState,
        TenantActor,
        TenantAuthorizationDenied,
        TenantService,
    )
    from lawyer_agent.domain.authorization import AuthorizationScope, Principal, PrincipalAudience
    from lawyer_agent.domain.tenancy import MembershipStatus, TenantContext, TenantStatus

    _, _, state, member, _ = setup
    principal = Principal(
        user_id=state.user_id, session_id=state.session_id, audience=PrincipalAudience.TENANT,
        tenant_id=member.tenant_id, membership_id=member.membership_id, user_status="active",
        session_valid=True, auth_version=3, session_auth_version=3,
        permissions=frozenset(), role_codes=frozenset(), authenticated_at=NOW,
    )
    context = TenantContext(
        tenant_id=member.tenant_id, membership_id=member.membership_id,
        membership_user_id=state.user_id, department_id=None, tenant_status=TenantStatus.ACTIVE,
        membership_status=MembershipStatus.ACTIVE, valid_from=member.valid_from, valid_until=None,
        authz_version=7, session_authz_version=7, scope=AuthorizationScope(allow_tenant_wide=True),
    )
    actor = TenantActor(principal, context)
    snapshot = SimpleNamespace(
        actor_session=ActorSessionState(state.session_id, state.user_id, None, None, 3, None,
                                        None, state.expires_at),
        context=context, actor_membership=SimpleNamespace(authz_version=7), user_auth_version=3,
    )
    if consumer == "tenant":
        service = object.__new__(TenantService)
        service._clock = lambda: NOW

        async def check():
            service._validate_authoritative_actor(actor, snapshot)
    else:
        service = object.__new__(InvitationService)
        service._clock = lambda: NOW
        uow = SimpleNamespace(authorization=SimpleNamespace(
            load_snapshot=AsyncMock(return_value=snapshot),
        ))

        async def check():
            await service._require_tenant_snapshot(uow, actor, for_update=False)

    await check()
    snapshot.actor_membership.authz_version = 8
    with pytest.raises((TenantAuthorizationDenied, InvitationAuthorizationDenied)):
        await check()
    snapshot.actor_membership.authz_version = 7
    snapshot.actor_session = replace(snapshot.actor_session, revoked_at=NOW)
    with pytest.raises((TenantAuthorizationDenied, InvitationAuthorizationDenied)):
        await check()


async def test_successful_rotation_invalidates_cached_old_device_expiry(setup):
    service, repo, state, _, _ = setup
    now = [state.expires_at - timedelta(seconds=30)]
    service._clock = lambda: now[0]
    raw = "R" * 43
    token_id = new_uuid7()
    repo.locate_refresh = AsyncMock(return_value=RefreshLockLocator(
        token_id, state.current_family_id, state.session_id, None,
    ))
    repo.lock_refresh = AsyncMock(return_value=LockedRefreshToken(
        token_id, service._refresh_hash(raw), state.current_family_id, state.session_id,
        state.user_id, None, None, state.expires_at, state.expires_at, None, None,
    ))
    for method in ("mark_refresh_used", "add_refresh", "link_refresh_replacement"):
        setattr(repo, method, AsyncMock())

    async def renew(session_id, touched_at, *, expires_at):
        assert session_id == state.session_id and touched_at == now[0]
        repo.get_validation_state.return_value = replace(state, expires_at=expires_at)

    repo.touch_session = AsyncMock(side_effect=renew)
    cache = SimpleNamespace(get=AsyncMock(return_value=state), set=AsyncMock())

    async def invalidate(session_id):
        assert session_id == state.session_id
        cache.get.return_value = None

    cache.invalidate = AsyncMock(side_effect=invalidate)
    service._cache = cache
    refreshed = await service.refresh(raw, audit_context=AUDIT)
    now[0] = state.expires_at + timedelta(seconds=1)
    validated = await service.validate_access(refreshed.access_token, audience=Audience.ACCOUNT)
    assert validated.user_id == state.user_id
    cache.invalidate.assert_awaited_once_with(state.session_id)
