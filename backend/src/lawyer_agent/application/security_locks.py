from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7


@dataclass(frozen=True, slots=True)
class TenantSecurityWriteLockRequest:
    tenant_id: UUID
    actor_user_id: UUID
    actor_session_id: UUID
    actor_membership_id: UUID
    target_membership_ids: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.tenant_id, "tenant security lock tenant_id"),
            (self.actor_user_id, "tenant security lock actor_user_id"),
            (self.actor_session_id, "tenant security lock actor_session_id"),
            (self.actor_membership_id, "tenant security lock actor_membership_id"),
        ):
            require_uuid7(value, field=name)
        for value in self.target_membership_ids:
            require_uuid7(value, field="tenant security lock target_membership_id")
        if self.target_membership_ids != tuple(
            sorted(set(self.target_membership_ids), key=str)
        ):
            raise ValueError("tenant security lock targets must be unique and ordered")


@dataclass(frozen=True, slots=True)
class SessionSecurityWriteLockRequest:
    session_id: UUID

    def __post_init__(self) -> None:
        require_uuid7(self.session_id, field="session security lock session_id")


class SecurityWriteLockRepositoryPort(Protocol):
    async def acquire_tenant_gate(self, tenant_id: UUID) -> bool: ...

    async def acquire_tenant_write(self, request: TenantSecurityWriteLockRequest) -> bool: ...

    async def acquire_session_revoke(
        self, request: SessionSecurityWriteLockRequest
    ) -> bool: ...
