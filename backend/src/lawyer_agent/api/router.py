from fastapi import APIRouter

from lawyer_agent import __version__

health_router = APIRouter(prefix="/health", tags=["health"])


@health_router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
