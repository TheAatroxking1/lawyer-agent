from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from lawyer_agent.api.dependencies import ApplicationServices, application_services
from lawyer_agent.api.errors import (
    APPLICATION_EXCEPTIONS,
    ApiProblem,
    api_problem_handler,
    application_exception_handler,
    http_exception_handler,
    request_validation_handler,
)
from lawyer_agent.api.router import health_router
from lawyer_agent.api.v1.router import api_v1_router
from lawyer_agent.config import Settings, get_settings

ServiceFactory = Callable[[Settings], AbstractAsyncContextManager[ApplicationServices]]
_TRACE_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,64}\Z", re.ASCII)


def create_app(
    settings: Settings | None = None,
    *,
    service_factory: ServiceFactory = application_services,
) -> FastAPI:
    active_settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with service_factory(active_settings) as services:
            app.state.services = services
            app.state.readiness = services.readiness
            yield

    app = FastAPI(
        title="Lawyer Agent API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = active_settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_settings.trusted_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-CSRF-Token",
            "X-Step-Up-Grant",
            "X-Request-ID",
        ],
    )

    @app.middleware("http")
    async def request_context(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        candidate = request.headers.get("x-request-id")
        request.state.trace_id = (
            candidate
            if candidate is not None and _TRACE_PATTERN.fullmatch(candidate)
            else uuid4().hex
        )
        sensitive_query_names = {
            "token",
            "access_token",
            "refresh_token",
            "step_up_grant",
        }
        response: Response
        if any(key.lower() in sensitive_query_names for key in request.query_params):
            response = await api_problem_handler(
                request,
                ApiProblem(
                    422,
                    "credential_in_query",
                    "Credentials are not accepted in query parameters",
                ),
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.trace_id
        return response

    app.include_router(health_router)
    app.include_router(api_v1_router)
    app.add_exception_handler(ApiProblem, api_problem_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, request_validation_handler)  # type: ignore[arg-type]
    for exception_type in APPLICATION_EXCEPTIONS:
        app.add_exception_handler(exception_type, application_exception_handler)
    app.add_exception_handler(Exception, application_exception_handler)
    return app


app = create_app()
