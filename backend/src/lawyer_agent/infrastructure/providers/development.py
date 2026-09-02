from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from lawyer_agent.domain.common import require_uuid7

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z", re.ASCII)


@dataclass(slots=True)
class TestInvitationDeliveryAdapter:
    """Test-only one-shot capture for credentials that a real provider would deliver."""

    environment: str
    _tokens: dict[UUID, str] = field(default_factory=dict, init=False, repr=False)

    __test__ = False

    def __post_init__(self) -> None:
        if self.environment != "test":
            raise ValueError("invitation capture is available only in the test environment")

    async def deliver(self, *, invitation_id: UUID, token: str) -> None:
        require_uuid7(invitation_id, field="invitation delivery invitation_id")
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise ValueError("invitation delivery token has an invalid format")
        if invitation_id in self._tokens:
            raise RuntimeError("invitation token was already captured")
        self._tokens[invitation_id] = token

    def take(self, invitation_id: UUID) -> str | None:
        require_uuid7(invitation_id, field="invitation delivery invitation_id")
        return self._tokens.pop(invitation_id, None)
