"""Authenticated PDF upload and bounded contract-review progress streams."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import StreamingResponse

from lawyer_agent.api.dependencies import Services, TenantActorDependency
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.contract_review.contracts import (
    DocumentVerificationError,
    ReviewError,
)
from lawyer_agent.application.contract_review.demo import DEMO_PDF, demo_events
from lawyer_agent.application.contract_review.web import (
    ContractReviewFinal,
    ContractReviewWeb,
    ContractRunPage,
    ContractRunView,
    CreateReviewInput,
)
from lawyer_agent.application.tenancy import TenantActor

router = APIRouter(prefix="/tenants/{tenant_id}/contract-reviews", tags=["contract-reviews"])
_MAX_PDF_BYTES = 10 * 1024 * 1024
_MAX_STREAM_BYTES = 2 * 1024 * 1024
_STREAM_RESERVE_BYTES = 8192
_STAGES = frozenset({
    "parsing", "content_warning", "preparing", "researching", "contract_understanding",
    "legal_search",
    "legal_selection", "legal_read", "reviewing", "validating", "saved", "no_evidence",
})


def _service(tenant_id: UUID, actor: TenantActor, services: Any) -> ContractReviewWeb:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "resource_unavailable", "Resource is unavailable")
    service = getattr(services, "contract_review_http", None)
    if service is None:
        raise ApiProblem(503, "contract_review_unavailable", "合同审查服务尚未配置")
    return cast(ContractReviewWeb, service)


def _problem(exc: Exception) -> ApiProblem:
    code = exc.code if isinstance(exc, ReviewError) else "contract_review_failed"
    if code == "resource_unavailable":
        return ApiProblem(404, code, "Resource is unavailable")
    if code in {"contract_run_conflict", "idempotency_conflict"}:
        return ApiProblem(409, code, "审查状态冲突，请刷新后重试")
    if code in {"document_too_large", "object_response_too_large"}:
        return ApiProblem(413, code, "PDF 超出当前处理限制")
    if code in {"document_invalid", "invalid_review_request"}:
        return ApiProblem(422, code, "合同文件或请求不符合要求")
    if code == "legal_metadata_unverified":
        return ApiProblem(422, code, "法律材料效力尚未核验，需要人工复核")
    if code == "model_output_truncated":
        return ApiProblem(503, code, "模型输出被长度上限截断，重新生成后仍未完整返回")
    if code in {"document_needs_review", "document_incomplete"}:
        if isinstance(exc, DocumentVerificationError) and exc.report.pages:
            labels = {
                "missing_native_coordinates": "缺少原生文字坐标",
                "unreadable": "页面文字无法辨认",
                "missing_text": "模型报告疑似缺字",
                "garbled_text": "模型报告疑似乱码",
                "text_mismatch": "文字比对不一致",
                "unconfirmed_coverage": "模型未明确具体差异，覆盖情况待核对",
                "visual_read_failed": "视觉读取未完成，仅保留已有文字，该页需人工核对",
            }
            details = "；".join(
                f"第{page.page}页：" + "、".join(labels[reason] for reason in page.reasons)
                for page in exc.report.pages
            )
            return ApiProblem(
                422, code, f"内容核对未通过。{details}。请对照原文复核，尚未生成批注。",
            )
        return ApiProblem(
            422, code, "页面内容无法完整核对或缺少可靠文字定位，需要人工复核，尚未生成批注",
        )
    if code == "vision_response_invalid":
        return ApiProblem(502, code, "Qwen 页面读取结果不完整或格式异常，请重试")
    return ApiProblem(503, code, "审查未完成，请依据错误码检查服务或材料")


@router.post("", response_model=ContractRunView, status_code=201)
async def create_review(
    tenant_id: UUID, body: CreateReviewInput, actor: TenantActorDependency,
    services: Services,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
) -> ContractRunView:
    service = _service(tenant_id, actor, services)
    try:
        return await service.create(actor, body, idempotency_key)
    except Exception as exc:
        raise _problem(exc) from None


@router.get("", response_model=ContractRunPage)
async def list_reviews(
    tenant_id: UUID, actor: TenantActorDependency, services: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> ContractRunPage:
    service = _service(tenant_id, actor, services)
    try:
        return await service.list_owned(actor, limit=limit, cursor=cursor)
    except Exception as exc:
        raise _problem(exc) from None


@router.post("/demo/events")
async def demo_review_events(
    tenant_id: UUID, actor: TenantActorDependency, services: Services,
) -> StreamingResponse:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "resource_unavailable", "Resource is unavailable")
    # The fixture is deliberately independent from configured model/RAG services.
    async def stream() -> AsyncIterator[str]:
        async for event in demo_events():
            name = str(event["type"])
            yield _event(name, {key: value for key, value in event.items() if key != "type"})

    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })


@router.get("/demo/document")
async def demo_review_document(
    tenant_id: UUID, actor: TenantActorDependency, services: Services,
) -> Response:
    if tenant_id != actor.context.tenant_id:
        raise ApiProblem(404, "resource_unavailable", "Resource is unavailable")
    return Response(DEMO_PDF, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": 'inline; filename="demo-contract.pdf"',
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
    })


@router.get("/{run_id}", response_model=ContractRunView)
async def get_review(
    tenant_id: UUID, run_id: UUID, actor: TenantActorDependency, services: Services,
) -> ContractRunView:
    service = _service(tenant_id, actor, services)
    try:
        return await service.get(actor, run_id)
    except Exception as exc:
        raise _problem(exc) from None


@router.put("/{run_id}/document", response_model=ContractRunView)
async def upload_document(
    tenant_id: UUID, run_id: UUID, request: Request,
    actor: TenantActorDependency, services: Services,
) -> ContractRunView:
    service = _service(tenant_id, actor, services)
    try:
        await service.get(actor, run_id)
        if request.headers.get("content-type", "").split(";", 1)[0] != "application/pdf":
            raise ReviewError("document_invalid")
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > _MAX_PDF_BYTES):
            raise ReviewError("document_too_large")
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_PDF_BYTES:
                raise ReviewError("document_too_large")
            body.extend(chunk)
        return await service.upload(actor, run_id, bytes(body))
    except Exception as exc:
        raise _problem(exc) from None


@router.get("/{run_id}/document")
async def get_document(
    tenant_id: UUID, run_id: UUID, actor: TenantActorDependency, services: Services,
) -> Response:
    service = _service(tenant_id, actor, services)
    try:
        payload = await service.document(actor, run_id)
    except Exception as exc:
        raise _problem(exc) from None
    return Response(payload, media_type="application/pdf", headers={
        "Cache-Control": "no-store", "Content-Disposition": 'inline; filename="contract.pdf"',
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox",
    })


def _event(name: str, payload: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _stream(
    service: ContractReviewWeb, actor: TenantActor, run_id: UUID,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=64)

    def progress(stage: str) -> None:
        if stage not in _STAGES:
            return
        if queue.full():
            queue.get_nowait()
        queue.put_nowait(stage)

    task = asyncio.create_task(service.run(actor, run_id, progress))
    status = "failed"
    emitted_bytes = 0

    def bounded(frame: str, *, reserve: bool = True) -> str:
        nonlocal emitted_bytes
        size = len(frame.encode("utf-8"))
        limit = _MAX_STREAM_BYTES - (_STREAM_RESERVE_BYTES if reserve else 0)
        if emitted_bytes + size > limit:
            raise ReviewError("contract_stream_too_large")
        emitted_bytes += size
        return frame

    try:
        while not task.done() or not queue.empty():
            if not queue.empty():
                yield bounded(_event("progress", {"stage": queue.get_nowait()}))
                continue
            pending = asyncio.create_task(queue.get())
            try:
                done, _ = await asyncio.wait(
                    (task, pending), timeout=15, return_when=asyncio.FIRST_COMPLETED,
                )
                if pending in done:
                    yield bounded(_event("progress", {"stage": pending.result()}))
                elif not done:
                    yield bounded(": keepalive\n\n")
            finally:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
        result = ContractRunView.model_validate(await task)
        if result.status not in {"draft", "no_evidence"}:
            raise ReviewError("contract_review_incomplete")
        final = ContractReviewFinal.from_run(result)
        final_frame = bounded(_event("result", final.model_dump(mode="json")))
        status = result.status
        yield final_frame
    except Exception as exc:  # noqa: BLE001 - never export provider/request payloads
        status = "failed"
        problem = _problem(exc)
        yield bounded(
            _event("error", {"code": problem.code, "title": problem.title}),
            reserve=False,
        )
    finally:
        if not task.done():
            if task.cancelling() == 0:
                task.cancel()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(service.cancel(actor, run_id), timeout=6)
        if task.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                task.result()
    yield bounded(_event("done", {"status": status}), reserve=False)


@router.post("/{run_id}/events")
async def review_events(
    tenant_id: UUID, run_id: UUID, actor: TenantActorDependency, services: Services,
) -> StreamingResponse:
    service = _service(tenant_id, actor, services)
    try:
        await service.get(actor, run_id)
    except Exception as exc:
        raise _problem(exc) from None
    return StreamingResponse(_stream(service, actor, run_id), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
