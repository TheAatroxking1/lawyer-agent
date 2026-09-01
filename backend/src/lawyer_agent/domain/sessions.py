from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from uuid import RFC_4122, UUID


class Audience(StrEnum):
    ACCOUNT = "lawyer-account"
    TENANT = "lawyer-tenant"
    PLATFORM = "lawyer-platform"
    STEP_UP = "lawyer-step-up"


class RevocationReason(StrEnum):
    LOGOUT = "logout"
    USER_DISABLED = "user_disabled"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 - fixed reason code, not a secret
    MEMBERSHIP_REVOKED = "membership_revoked"
    AUTHORIZATION_CHANGED = "authorization_changed"
    ADMIN_REVOKED = "admin_revoked"
    REFRESH_INVALID = "refresh_invalid"
    AUTHORITATIVE_STATE_CHANGED = "authoritative_state_changed"
    REFRESH_REPLAY = "refresh_replay"
    TENANT_SWITCHED = "tenant_switched"


class InvalidToken(Exception):
    code = "authentication_failed"

    def __init__(self) -> None:
        super().__init__("invalid access token")


@dataclass(frozen=True, slots=True)
class AccessTokenClaims:
    user_id: UUID
    session_id: UUID
    token_id: UUID
    audience: Audience
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    auth_version: int
    tenant_id: UUID | None = None
    membership_id: UUID | None = None
    authz_version: int | None = None

    def __post_init__(self) -> None:
        if any(
            not _is_uuid7(value)
            for value in (self.user_id, self.session_id, self.token_id)
        ):
            raise ValueError("token identifiers must be RFC 9562 UUIDv7 values")
        if not isinstance(self.audience, Audience):
            raise ValueError("audience must be strongly typed")
        for value in (self.issued_at, self.not_before, self.expires_at):
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() != UTC.utcoffset(value)
            ):
                raise ValueError("token timestamps must be UTC-aware")
        if self.not_before < self.issued_at or self.expires_at <= self.not_before:
            raise ValueError("token time window is invalid")
        maximum_lifetime = (
            timedelta(minutes=5)
            if self.audience in {Audience.PLATFORM, Audience.STEP_UP}
            else timedelta(minutes=10)
        )
        if self.expires_at - self.issued_at > maximum_lifetime:
            raise ValueError("token lifetime exceeds the audience policy")
        if (
            isinstance(self.auth_version, bool)
            or not isinstance(self.auth_version, int)
            or self.auth_version < 1
        ):
            raise ValueError("auth_version must be a positive integer")

        tenant_values = (self.tenant_id, self.membership_id, self.authz_version)
        if self.audience is Audience.TENANT:
            if any(value is None for value in tenant_values):
                raise ValueError("tenant token requires complete tenant context")
            if not _is_uuid7(self.tenant_id) or not _is_uuid7(self.membership_id):
                raise ValueError("tenant context identifiers must be RFC 9562 UUIDv7 values")
            if (
                isinstance(self.authz_version, bool)
                or not isinstance(self.authz_version, int)
                or self.authz_version < 1
            ):
                raise ValueError("tenant authz_version must be a positive integer")
        elif any(value is not None for value in tenant_values):
            raise ValueError("non-tenant token cannot contain tenant context")


def _is_uuid7(value: object) -> bool:
    return (
        isinstance(value, UUID)
        and value.version == 7
        and value.variant == RFC_4122
        and value.int != 0
    )
