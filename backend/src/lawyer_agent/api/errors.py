from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import ORJSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def error_code(status_code: int) -> str:
    if status_code == 404:
        return "route_not_found"
    if status_code == 401:
        return "authentication_required"
    if status_code == 403:
        return "permission_denied"
    return "http_error"


async def http_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> ORJSONResponse:
    trace_id = request.headers.get("x-request-id") or uuid4().hex
    title = str(exc.detail) if isinstance(exc.detail, str) else "HTTP Error"
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title,
        "status": exc.status_code,
        "code": error_code(exc.status_code),
        "trace_id": trace_id,
    }
    return ORJSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)
