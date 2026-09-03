from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite

from fastapi import APIRouter, Request

from lawyer_agent import __version__
from lawyer_agent.api.errors import ApiProblem

ReadinessCheck = Callable[[], Awaitable[object]]


async def _run_readiness_check(check: ReadinessCheck) -> None:
    await check()


@dataclass(frozen=True, slots=True)
class ConcurrentReadinessProbe:
    """Run required dependency probes concurrently within one shared deadline."""

    checks: tuple[ReadinessCheck, ...]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.checks:
            raise ValueError("readiness checks must not be empty")
        if not isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("readiness timeout must be positive")

    async def check(self) -> None:
        async with asyncio.timeout(self.timeout_seconds):
            async with asyncio.TaskGroup() as tasks:
                for check in self.checks:
                    tasks.create_task(_run_readiness_check(check))

health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@health_router.get("/ready")
async def ready(request: Request) -> dict[str, str]:
    probe = getattr(request.app.state, "readiness", None)
    try:
        if not isinstance(probe, ConcurrentReadinessProbe):
            raise RuntimeError("readiness probe is unavailable")
        await probe.check()
    except Exception:
        raise ApiProblem(503, "service_not_ready", "Service is not ready") from None
    return {"status": "ready"}
