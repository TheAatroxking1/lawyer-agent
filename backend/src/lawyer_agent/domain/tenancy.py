from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7

if TYPE_CHECKING:
    from lawyer_agent.domain.authorization import AuthorizationScope


class TenantStatus(StrEnum):
    PENDING_VERIFICATION = "pending_verification"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    CLOSED = "closed"


class DepartmentStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class MembershipStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    SUSPENDED = "suspended"
    REVOKED = "revoked"


class MemberType(StrEnum):
    OWNER = "owner"
    INTERNAL = "internal"
    STUDENT = "student"
    EXTERNAL_CLIENT = "external_client"


@dataclass(frozen=True, slots=True)
class NormalizedTenantName:
    display_value: str
    normalized_value: str


def normalize_tenant_name(value: object) -> NormalizedTenantName:
    if not isinstance(value, str):
        raise ValueError("tenant name is invalid")
    display_value = unicodedata.normalize("NFKC", value).strip()
    if not display_value or len(display_value) > 255:
        raise ValueError("tenant name is invalid")
    return NormalizedTenantName(
        display_value=display_value,
        normalized_value=display_value.casefold(),
    )


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: UUID
    membership_id: UUID | None
    membership_user_id: UUID | None
    department_id: UUID | None
    tenant_status: TenantStatus
    membership_status: MembershipStatus | None
    valid_from: datetime | None
    valid_until: datetime | None
    authz_version: int | None
    session_authz_version: int | None
    scope: AuthorizationScope


@dataclass(frozen=True, slots=True)
class Tenant:
    id: UUID
    name: str
    normalized_name: str
    tenant_type: str
    status: TenantStatus
    version: int
    created_by_user_id: UUID
    review_status: str

    def __post_init__(self) -> None:
        _require_uuid7(self.id)
        _require_text(self.name, "tenant name")
        _require_text(self.normalized_name, "normalized tenant name")
        if self.tenant_type not in {"law_firm", "enterprise", "university"}:
            raise ValueError("unknown tenant type")
        if not isinstance(self.status, TenantStatus):
            raise ValueError("tenant status must be strongly typed")
        _require_version(self.version)
        _require_uuid7(self.created_by_user_id)
        if self.review_status not in {"pending", "approved", "rejected"}:
            raise ValueError("unknown tenant review status")


@dataclass(frozen=True, slots=True)
class Department:
    id: UUID
    tenant_id: UUID
    parent_id: UUID | None
    name: str
    status: DepartmentStatus
    version: int

    def __post_init__(self) -> None:
        _require_uuid7(self.id)
        _require_uuid7(self.tenant_id)
        if self.parent_id is not None:
            _require_uuid7(self.parent_id)
        _require_text(self.name, "department name")
        if not isinstance(self.status, DepartmentStatus):
            raise ValueError("department status must be strongly typed")
        _require_version(self.version)


@dataclass(frozen=True, slots=True)
class Membership:
    id: UUID
    tenant_id: UUID
    user_id: UUID
    department_id: UUID | None
    member_type: MemberType
    status: MembershipStatus
    valid_from: datetime
    valid_until: datetime | None
    authz_version: int
    version: int

    def __post_init__(self) -> None:
        for value in (self.id, self.tenant_id, self.user_id):
            _require_uuid7(value)
        if self.department_id is not None:
            _require_uuid7(self.department_id)
        if not isinstance(self.member_type, MemberType):
            raise ValueError("member type must be strongly typed")
        if not isinstance(self.status, MembershipStatus):
            raise ValueError("membership status must be strongly typed")
        _require_utc(self.valid_from)
        if self.valid_until is not None:
            _require_utc(self.valid_until)
            if self.valid_until <= self.valid_from:
                raise ValueError("membership validity window is invalid")
        _require_version(self.authz_version)
        _require_version(self.version)


def _require_uuid7(value: object) -> None:
    require_uuid7(value)


def _require_utc(value: object) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("timestamp must be UTC-aware")


def _require_version(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("version must be a positive integer")


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 255:
        raise ValueError(f"{name} is invalid")
