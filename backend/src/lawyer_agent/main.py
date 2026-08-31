from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from lawyer_agent.api.errors import http_exception_handler
from lawyer_agent.api.router import health_router
from lawyer_agent.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    active_settings = settings or get_settings()
    app = FastAPI(
        title="Lawyer Agent API",
        version="0.1.0",
        default_response_class=JSONResponse,
    )
    app.state.settings = active_settings
    app.include_router(health_router)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    return app


app = create_app()
