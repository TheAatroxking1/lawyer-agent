from __future__ import annotations

from typing import Protocol

from lawyer_agent.domain.ai_jobs import (
    ClaimedExecution,
    ClaimJobRequest,
    ClaimRejected,
    FencedFinalizeSuccess,
    FencedHeartbeat,
)


class AIJobRuntimeRepositoryPort(Protocol):
    async def claim_due_job(
        self, request: ClaimJobRequest
    ) -> ClaimedExecution | ClaimRejected: ...

    async def heartbeat(self, request: FencedHeartbeat) -> bool: ...

    async def finalize_success(self, request: FencedFinalizeSuccess) -> bool: ...
