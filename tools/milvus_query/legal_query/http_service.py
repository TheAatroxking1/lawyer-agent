"""只读共享法律集合的内网HTTP接口；独立于公网多租户业务API。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal
from uuid import uuid4

from fastapi import FastAPI, Request, Security
from fastapi.exceptions import RequestValidationError, ResponseValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBearer
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)
from starlette.types import ASGIApp, Receive, Scope, Send

from legal_query.config import QueryError
from legal_query.http_runtime import Runtime

Identity = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
CharBudget = Annotated[int, Field(strict=True, ge=1000, le=40000)]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=8000)
    mode: Literal["hybrid", "dense", "bm25"] = "hybrid"
    top_k: int = Field(default=10, strict=True, ge=1, le=20)
    document_ids: list[Identity] = Field(default_factory=list, max_length=20)
    raw_bm25: bool = Field(default=False, strict=True)
    max_chars: CharBudget = 20000

    @field_validator("query")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty_query")
        return value.strip()


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: Identity
    chunk_id: Identity | None = None
    article_no: str | None = Field(default=None, min_length=1, max_length=256)
    max_chars: CharBudget = 20000

    @model_validator(mode="after")
    def selector(self) -> ReadRequest:
        if (self.chunk_id is None) == (self.article_no is None):
            raise ValueError("exactly_one_selector_required")
        if self.article_no is not None and not self.article_no.strip():
            raise ValueError("empty_article")
        return self


class Evidence(BaseModel):
    evidence_id: str
    document_id: str
    chunk_id: str
    document_name: str
    article_no: str | None
    text: str
    chunk_type: str
    parent_chunk_id: str | None
    model: str
    rank: int
    fusion_score: float | None = None
    retrieval_score: float | None = None
    is_complete: bool | None = None
    text_truncated: bool = False


class EvidenceResponse(BaseModel):
    request_id: str
    status: Literal["ok", "empty"]
    collection: str = "lawyer_db"
    corpus_version: str | None = None
    mode: str | None = None
    ranker: str | None = None
    candidate_count: int | None = None
    bm25_query: str | None = None
    evidence: list[Evidence]
    warnings: list[str]
    truncated: bool = False


def problem(status: int, code: str, request_id: str) -> JSONResponse:
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if status == 429:
        headers["Retry-After"] = "60"
    return JSONResponse(
        {
            "type": "about:blank",
            "title": code,
            "status": status,
            "code": code,
            "request_id": request_id,
        },
        status_code=status,
        headers=headers,
        media_type="application/problem+json",
    )


class Guard:
    """在读取/解析正文前鉴权；流式计数限制正文，限流状态只保存30个时间戳。"""

    def __init__(self, app: ASGIApp, owner: FastAPI) -> None:
        self.app, self.owner = app, owner
        self.calls: deque[float] = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope["path"]
        protected = path not in ("/healthz", "/docs", "/openapi.json", "/docs/oauth2-redirect")
        if protected:
            headers = dict(scope["headers"])
            supplied = headers.get(b"authorization", b"")
            runtime = getattr(self.owner.state, "runtime", None)
            expected = ("Bearer " + runtime.key).encode() if runtime is not None else b""
            if not expected or not hmac.compare_digest(
                hashlib.sha256(supplied).digest(), hashlib.sha256(expected).digest()
            ):
                await problem(401, "unauthorized", request_id)(scope, receive, send)
                return
        if scope["method"] in ("POST", "PUT", "PATCH"):
            data = bytearray()
            try:
                async with asyncio.timeout(10):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        chunk = message.get("body", b"")
                        if len(data) + len(chunk) > 65536:
                            await problem(413, "request_too_large", request_id)(
                                scope, receive, send
                            )
                            return
                        data.extend(chunk)
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                await problem(408, "request_body_timeout", request_id)(scope, receive, send)
                return
            original_receive = receive
            delivered = False

            async def replay() -> Any:
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": bytes(data), "more_body": False}
                return await original_receive()

            receive = replay
        if protected:
            now = time.monotonic()
            while self.calls and self.calls[0] <= now - 60:
                self.calls.popleft()
            if len(self.calls) >= 30:
                await problem(429, "rate_limited", request_id)(scope, receive, send)
                return
            self.calls.append(now)

        async def with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in (b"x-request-id", b"cache-control")
                ] + [(b"x-request-id", request_id.encode()), (b"cache-control", b"no-store")]
            await send(message)

        await self.app(scope, receive, with_headers)


def create_app(runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
        app.state.runtime = runtime if runtime is not None else Runtime.from_env()
        yield

    app = FastAPI(
        title="律师RAG内网接口",
        version="1.0.0",
        lifespan=lifespan,
        redoc_url=None,
        description="只读共享法律语料。返回检索证据，不生成法律答案；地域、全文完整性及引用支持需后续核验。",
    )
    if runtime is not None:
        app.state.runtime = runtime
    app.add_middleware(Guard, owner=app)
    bearer = HTTPBearer(auto_error=False)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, _error: RequestValidationError) -> JSONResponse:
        return problem(422, "invalid_request", request.state.request_id)

    @app.exception_handler(ResponseValidationError)
    async def invalid_response(request: Request, _error: ResponseValidationError) -> JSONResponse:
        return problem(503, "dependency_response_invalid", request.state.request_id)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "alive"}

    async def execute(request: Request, operation: str, payload: Any = None) -> Any:
        try:
            # Starlette工作线程：不阻塞事件循环；并发名额由线程内WorkGate持有。
            from starlette.concurrency import run_in_threadpool

            result = await run_in_threadpool(
                app.state.runtime.run, operation, payload, request_id=request.state.request_id
            )
            return {**result, "request_id": request.state.request_id}
        except QueryError as error:
            code = str(error)
            if code == "service_busy":
                return problem(429, code, request.state.request_id)
            # 不透传任意异常字符串，SDK/供应商错误都用白名单脱敏。
            if code in {
                "embedding_access_denied",
                "embedding_rate_limited",
                "embedding_unavailable",
            }:
                return problem(502, code, request.state.request_id)
            return problem(503, "dependency_unavailable", request.state.request_id)
        except Exception:
            return problem(503, "dependency_unavailable", request.state.request_id)

    @app.get("/readyz", dependencies=[Security(bearer)])
    async def ready(request: Request) -> Any:
        return await execute(request, "ready")

    @app.post(
        "/api/v1/rag/search", response_model=EvidenceResponse, dependencies=[Security(bearer)]
    )
    async def search_route(body: SearchRequest, request: Request) -> Any:
        return await execute(request, "search", body)

    @app.post("/api/v1/rag/read", response_model=EvidenceResponse, dependencies=[Security(bearer)])
    async def read_route(body: ReadRequest, request: Request) -> Any:
        return await execute(request, "read", body)

    return app


app = create_app()
