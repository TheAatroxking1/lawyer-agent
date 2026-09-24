from __future__ import annotations

from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lawyer_agent.api.dependencies import (
    AccountSession,
    Audit,
    Bearer,
    Services,
    TrustedOrigin,
)
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.identity import LoginIdentifier, RegisterCommand
from lawyer_agent.application.platform import PlatformActor
from lawyer_agent.application.sessions import REMEMBER_DEVICE_SECONDS, SwitchTenantCommand
from lawyer_agent.domain.authorization import Principal, PrincipalAudience
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.identity import IdentityKind, normalize_identifier, normalize_username
from lawyer_agent.domain.sessions import AccessTokenClaims, Audience, RevocationReason
from lawyer_agent.infrastructure.redis.rate_limit import (
    LOGIN_RULE,
    REAUTH_RULE,
    REFRESH_RULE,
    REGISTER_RULE,
    SWITCH_RULE,
    RateLimitRule,
)
from lawyer_agent.infrastructure.security.csrf import CSRF_COOKIE_NAME, REFRESH_COOKIE_NAME

router = APIRouter(prefix="/auth", tags=["authentication"])
_PLATFORM_STEP_UP_ACTIONS = frozenset(
    {
        "tenant_application.review:approve",
        "tenant_application.review:reject",
    }
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=255)

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        normalize_username(value)
        return value

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("display_name must not be blank")
        return value


class LoginRequest(StrictModel):
    kind: Literal["username", "phone", "email", "wechat_unionid", "wechat_openid"]
    identifier: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=128)
    issuer: str | None = Field(default=None, max_length=255)


class SwitchTenantRequest(StrictModel):
    tenant_id: UUID
    membership_id: UUID


class ReauthRequest(StrictModel):
    kind: Literal["username", "phone", "email", "wechat_unionid", "wechat_openid"]
    identifier: str = Field(min_length=1, max_length=512)
    password: str = Field(min_length=1, max_length=128)
    tenant_id: UUID | None = None
    action: Literal[
        "tenant_application.read",
        "tenant_application.review:approve",
        "tenant_application.review:reject",
    ]
    issuer: str | None = Field(default=None, max_length=255)


class AccessTokenResponse(StrictModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105


class TenantAccessResponse(StrictModel):
    access_token: str


class StepUpResponse(StrictModel):
    access_token: str
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105
    step_up_grant: str


def _client_ip(request: Request) -> object:
    value = request.client.host if request.client is not None else "127.0.0.1"
    try:
        return ip_address(value)
    except ValueError:
        return ip_address("127.0.0.1")


async def _limit(
    services: Services,
    rule: RateLimitRule,
    dimensions: dict[str, object],
) -> None:
    decision = await services.rate_limiter.consume(rule, dimensions)
    if not decision.allowed:
        retry = int(decision.retry_after_seconds)
        raise ApiProblem(
            429,
            "rate_limit_exceeded",
            "Rate limit exceeded",
            {"Retry-After": str(retry)},
        )


def _set_session_cookies(
    response: Response,
    request: Request,
    services: Services,
    result: Any,
) -> None:
    session_id = result.session_id
    refresh_token = result.refresh_token
    csrf_token = services.csrf.issue(session_id)
    # The __Host- prefix is a browser-enforced security contract: Secure,
    # Path=/ and no Domain are mandatory in every environment.
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh_token,
        max_age=REMEMBER_DEVICE_SECONDS,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        CSRF_COOKIE_NAME,
        csrf_token,
        max_age=REMEMBER_DEVICE_SECONDS,
        httponly=False,
        secure=True,
        samesite="lax",
        path="/",
    )


@router.post(
    "/register",
    response_model=AccessTokenResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    services: Services,
    audit: Audit,
    trusted_origin: TrustedOrigin,
) -> AccessTokenResponse:
    del trusted_origin
    await _limit(services, REGISTER_RULE, {"ip": _client_ip(request)})
    registered = await services.identity.register(
        RegisterCommand(body.username, body.password, body.display_name),
        audit_context=audit,
    )
    session = await services.sessions.start(
        user_id=registered.user_id,
        audit_context=audit,
    )
    _set_session_cookies(response, request, services, session)
    return AccessTokenResponse(access_token=session.access_token)


@router.post("/login", response_model=AccessTokenResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    services: Services,
    audit: Audit,
    trusted_origin: TrustedOrigin,
) -> AccessTokenResponse:
    del trusted_origin
    try:
        normalized = normalize_identifier(
            IdentityKind(body.kind),
            body.identifier,
            issuer=body.issuer,
        )
    except ValueError:
        normalized = normalize_identifier(
            IdentityKind.USERNAME,
            "invalid-login-identity",
        )
    await _limit(
        services,
        LOGIN_RULE,
        {"ip": _client_ip(request), "identity": normalized},
    )
    authenticated = await services.identity.authenticate(
        LoginIdentifier(IdentityKind(body.kind), body.identifier, body.issuer),
        body.password,
        audit_context=audit,
    )
    if authenticated is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    session = await services.sessions.start(
        user_id=authenticated.user_id,
        audit_context=audit,
    )
    _set_session_cookies(response, request, services, session)
    return AccessTokenResponse(access_token=session.access_token)


async def _cookie_session(
    request: Request,
    services: Services,
    csrf_header: str | None,
) -> tuple[str, UUID]:
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw is None:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")
    session_id = await services.sessions.refresh_session_id(raw)
    services.csrf.verify(
        origin=request.headers.get("origin"),
        header_token=csrf_header,
        cookie_token=request.cookies.get(CSRF_COOKIE_NAME),
        session_id=session_id,
    )
    return raw, session_id


@router.post("/refresh", response_model=AccessTokenResponse)
async def refresh(
    request: Request,
    response: Response,
    services: Services,
    audit: Audit,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AccessTokenResponse:
    raw, session_id = await _cookie_session(request, services, csrf_header)
    await _limit(services, REFRESH_RULE, {"session": session_id})
    result = await services.sessions.refresh(raw, audit_context=audit)
    _set_session_cookies(response, request, services, result)
    return AccessTokenResponse(access_token=result.access_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    services: Services,
    audit: Audit,
    csrf_header: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    _, session_id = await _cookie_session(request, services, csrf_header)
    await services.sessions.revoke(
        session_id,
        reason=RevocationReason.LOGOUT,
        audit_context=audit,
    )
    response.delete_cookie(
        REFRESH_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        CSRF_COOKIE_NAME,
        path="/",
        secure=True,
        httponly=False,
        samesite="lax",
    )


@router.post("/tenant-access", response_model=TenantAccessResponse)
async def tenant_access(
    body: SwitchTenantRequest, services: Services, current: AccountSession,
    audit: Audit, trusted_origin: TrustedOrigin,
) -> TenantAccessResponse:
    del trusted_origin
    await _limit(services, SWITCH_RULE, {"session": current.session_id})
    token = await services.sessions.tenant_access(
        SwitchTenantCommand(current.session_id, body.tenant_id, body.membership_id),
        audit_context=audit,
    )
    return TenantAccessResponse(access_token=token)


@router.post("/switch-tenant", response_model=AccessTokenResponse)
async def switch_tenant(
    body: SwitchTenantRequest,
    request: Request,
    response: Response,
    services: Services,
    current: AccountSession,
    audit: Audit,
    trusted_origin: TrustedOrigin,
) -> AccessTokenResponse:
    del trusted_origin
    await _limit(services, SWITCH_RULE, {"session": current.session_id})
    result = await services.sessions.switch_tenant(
        SwitchTenantCommand(current.session_id, body.tenant_id, body.membership_id),
        audit_context=audit,
    )
    _set_session_cookies(response, request, services, result)
    return AccessTokenResponse(access_token=result.access_token)


@router.post("/reauth", response_model=AccessTokenResponse | StepUpResponse)
async def reauth(
    body: ReauthRequest,
    token: Bearer,
    services: Services,
    current: AccountSession,
    audit: Audit,
    trusted_origin: TrustedOrigin,
) -> AccessTokenResponse | StepUpResponse:
    del trusted_origin
    read_exchange = body.action == "tenant_application.read"
    if (read_exchange and body.tenant_id is not None) or (
        not read_exchange and body.tenant_id is None
    ):
        raise ApiProblem(422, "reauth_request_invalid", "Reauthentication request is invalid")
    await _limit(services, REAUTH_RULE, {"session": current.session_id})
    authenticated = await services.identity.authenticate(
        LoginIdentifier(IdentityKind(body.kind), body.identifier, body.issuer),
        body.password,
        audit_context=audit,
    )
    if authenticated is None or authenticated.user_id != current.user_id:
        raise ApiProblem(401, "authentication_failed", "Authentication failed")

    now = datetime.now(UTC).replace(microsecond=0)
    account_claims = services.token_service.verify(
        token,
        audience=Audience.ACCOUNT,
        now=now,
    )
    candidate = PlatformActor(
        Principal(
            user_id=current.user_id,
            session_id=current.session_id,
            audience=PrincipalAudience.PLATFORM,
            tenant_id=None,
            membership_id=None,
            user_status="active",
            session_valid=True,
            auth_version=account_claims.auth_version,
            session_auth_version=account_claims.auth_version,
            permissions=frozenset(),
            role_codes=frozenset(),
            authenticated_at=now,
        )
    )
    await services.platform.authorize_access(
        candidate,
        permission=(
            "tenant_application.read"
            if read_exchange
            else "tenant_application.review"
        ),
        audit_context=audit,
    )
    platform_claims = AccessTokenClaims(
        user_id=current.user_id,
        session_id=current.session_id,
        token_id=new_uuid7(),
        audience=Audience.PLATFORM,
        issued_at=now,
        not_before=now,
        expires_at=now + timedelta(minutes=5),
        auth_version=account_claims.auth_version,
    )
    platform_token = services.token_service.issue_platform(platform_claims)
    if read_exchange:
        return AccessTokenResponse(access_token=platform_token)
    assert body.tenant_id is not None
    assert body.action in _PLATFORM_STEP_UP_ACTIONS
    grant = await services.step_up.issue(
        user_id=current.user_id,
        session_id=current.session_id,
        tenant_id=body.tenant_id,
        action=body.action,
    )
    return StepUpResponse(
        access_token=platform_token,
        step_up_grant=grant.value,
    )
