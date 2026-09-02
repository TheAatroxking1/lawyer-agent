from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lawyer_agent.application.idempotency import (
    IdempotencyConflictError,
    IdempotencyInProgressError,
    InvalidIdempotencyKey,
    InvalidIdempotencyRequest,
)
from lawyer_agent.application.identity import IdentityConflictError
from lawyer_agent.application.invitations import (
    InvitationAuthorizationDenied,
    InvitationDeliveryError,
    InvitationRoleDenied,
    InvitationUnavailable,
)
from lawyer_agent.application.platform import (
    PlatformApplicationUnavailable,
    PlatformAuthorizationDenied,
    PlatformCommittedWithCleanupWarning,
    StepUpRequired,
)
from lawyer_agent.application.sessions import InvalidRefreshToken, InvalidSession
from lawyer_agent.application.tenancy import (
    InvalidMemberCursor,
    InvalidMemberUpdate,
    InvalidStrongETag,
    MembershipMutationDenied,
    PostCommitCacheInvalidationError,
    PreconditionRequired,
    TenantApplicantUnavailable,
    TenantAuthorizationDenied,
    TenantResourceNotFound,
    VersionConflict,
)
from lawyer_agent.infrastructure.redis.client import RedisDependencyError
from lawyer_agent.infrastructure.security.csrf import InvalidCsrfToken, UntrustedOrigin
from lawyer_agent.infrastructure.security.passwords import InvalidPassword

APPLICATION_EXCEPTIONS: tuple[type[Exception], ...] = (
    InvalidSession,
    InvalidRefreshToken,
    UntrustedOrigin,
    InvalidCsrfToken,
    TenantResourceNotFound,
    InvitationUnavailable,
    PlatformApplicationUnavailable,
    TenantAuthorizationDenied,
    InvitationAuthorizationDenied,
    PlatformAuthorizationDenied,
    StepUpRequired,
    InvitationRoleDenied,
    MembershipMutationDenied,
    PreconditionRequired,
    IdentityConflictError,
    IdempotencyConflictError,
    IdempotencyInProgressError,
    VersionConflict,
    InvalidIdempotencyKey,
    InvalidIdempotencyRequest,
    InvalidStrongETag,
    InvalidMemberCursor,
    InvalidMemberUpdate,
    InvalidPassword,
    RedisDependencyError,
    PostCommitCacheInvalidationError,
    InvitationDeliveryError,
    PlatformCommittedWithCleanupWarning,
    TenantApplicantUnavailable,
)


@dataclass(frozen=True, slots=True)
class ApiProblem(Exception):
    status: int
    code: str
    title: str
    headers: dict[str, str] | None = None


def _trace_id(request: Request) -> str:
    value = getattr(request.state, "trace_id", None)
    return value if isinstance(value, str) else uuid4().hex


def _response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    headers: Mapping[str, str] | None = None,
    errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": status,
        "code": code,
        "trace_id": _trace_id(request),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(
        status_code=status,
        content=body,
        headers=headers,
        media_type="application/problem+json",
    )


async def api_problem_handler(request: Request, exc: ApiProblem) -> JSONResponse:
    return _response(
        request,
        status=exc.status,
        code=exc.code,
        title=exc.title,
        headers=exc.headers,
    )


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    code = {
        401: "authentication_failed",
        403: "permission_denied",
        404: "route_not_found",
        405: "method_not_allowed",
    }.get(exc.status_code, "http_error")
    title = {
        401: "Authentication failed",
        403: "Permission denied",
        404: "Not Found",
        405: "Method Not Allowed",
    }.get(exc.status_code, "HTTP Error")
    return _response(
        request,
        status=exc.status_code,
        code=code,
        title=title,
        headers=exc.headers,
    )


async def request_validation_handler(
    request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    errors = [
        {
            "field": ".".join(str(part) for part in item["loc"] if part != "body"),
            "code": str(item["type"]),
        }
        for item in exc.errors()
    ]
    return _response(
        request,
        status=422,
        code="request_validation_failed",
        title="Request validation failed",
        errors=errors,
    )


def _application_problem(exc: Exception) -> tuple[int, str, str]:
    if isinstance(exc, (InvalidSession, InvalidRefreshToken)):
        return 401, "authentication_failed", "Authentication failed"
    if isinstance(exc, (UntrustedOrigin, InvalidCsrfToken)):
        return 403, exc.code, "Request origin or CSRF validation failed"
    if isinstance(
        exc,
        (TenantResourceNotFound, InvitationUnavailable, PlatformApplicationUnavailable),
    ):
        code = (
            "tenant_resource_not_found"
            if isinstance(exc, TenantResourceNotFound)
            else exc.code
        )
        return 404, code, "Resource not found"
    if isinstance(
        exc,
        (TenantAuthorizationDenied, InvitationAuthorizationDenied, PlatformAuthorizationDenied),
    ):
        return 403, "authorization_denied", "Action is not authorized"
    if isinstance(exc, (StepUpRequired, InvitationRoleDenied, MembershipMutationDenied)):
        return 403, exc.code, "Action is not authorized"
    if isinstance(exc, PreconditionRequired):
        return 428, exc.code, "If-Match is required"
    if isinstance(
        exc,
        (
            IdentityConflictError,
            IdempotencyConflictError,
            IdempotencyInProgressError,
            VersionConflict,
        ),
    ):
        return 409, exc.code, "Request conflicts with current state"
    if isinstance(
        exc,
        (
            InvalidIdempotencyKey,
            InvalidIdempotencyRequest,
            InvalidStrongETag,
            InvalidMemberCursor,
            InvalidMemberUpdate,
            InvalidPassword,
            ValueError,
        ),
    ):
        return 422, getattr(exc, "code", "invalid_request"), "Request is invalid"
    if isinstance(exc, (RedisDependencyError, PostCommitCacheInvalidationError)):
        return 503, "security_dependency_unavailable", "Security dependency unavailable"
    if isinstance(exc, (InvitationDeliveryError, PlatformCommittedWithCleanupWarning)):
        return 503, exc.code, "Operation committed with a delivery warning"
    if isinstance(exc, TenantApplicantUnavailable):
        return 409, exc.code, "Tenant applicant is unavailable"
    return 500, "internal_error", "Internal server error"


async def application_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    status, code, title = _application_problem(exc)
    return _response(request, status=status, code=code, title=title)
