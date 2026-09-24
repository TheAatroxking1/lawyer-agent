"""Two MCP SDK 2 servers. No default services, credentials or public listener."""

import json
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent

from lawyer_agent.application.contract_review.contracts import (
    BlocksResult,
    EvidenceInput,
    JobInput,
    JobResult,
    LegalEvidence,
    ReadBlocksInput,
    RegionInput,
    ReviewError,
    ReviewScope,
    SearchInput,
    SearchResult,
    StructureInput,
    StructureResult,
    VersionInput,
    WireModel,
)
from lawyer_agent.application.contract_review.ports import (
    Authorizer,
    DocumentService,
    LegalService,
    ScopeProvider,
)


def _tool_meta(
    capability: str,
    *,
    agent_visible: bool = True,
    resource_path: str | None = None,
    resource_constant: str | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "lawyer_agent/capability": capability,
        "lawyer_agent/agent_visible": agent_visible,
    }
    if resource_path is not None:
        meta["lawyer_agent/resource_path"] = resource_path
    if resource_constant is not None:
        meta["lawyer_agent/resource_constant"] = resource_constant
    return meta


async def _guard_arguments(ctx: ServerRequestContext[Any], call_next: CallNext) -> HandlerResult:
    if ctx.method == "tools/call":
        params = ctx.params or {}
        name, arguments = params.get("name"), params.get("arguments")
        code: str | None = None
        if not isinstance(name, str):
            code = "unknown_tool"
        elif not isinstance(arguments, dict) or set(arguments) != {"request"}:
            code = "invalid_tool_arguments"
        else:
            try:
                if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > 65536:
                    raise ValueError("argument budget")
            except (ValueError, TypeError):
                code = "invalid_tool_arguments"
        if code is not None:
            return CallToolResult(is_error=True, content=[TextContent(type="text", text=code)])
    result = await call_next(ctx)
    failed = (
        isinstance(result, CallToolResult) and result.is_error
    ) or (
        isinstance(result, dict) and result.get("isError") is True
    )
    if ctx.method == "tools/call" and failed:
        return CallToolResult(
            is_error=True,
            content=[TextContent(type="text", text="tool_failure")],
        )
    return result


async def _invoke[ResultT: WireModel](
    *,
    scopes: ScopeProvider,
    authorizer: Authorizer,
    action: str,
    resource: str,
    version: str | None,
    call: Callable[[ReviewScope], Awaitable[ResultT]],
    result_type: type[ResultT],
) -> ResultT:
    try:
        scope = await scopes.current()
        if version is not None and version != scope.document_version_id:
            raise ReviewError("resource_unavailable")
        await authorizer.require(scope, action, resource)
        raw = await call(scope)
        result = result_type.model_validate(raw)
        if (
            isinstance(result, VersionInput)
            and result.document_version_id != scope.document_version_id
        ):
            raise ReviewError("invalid_tool_result")
        if len(result.model_dump_json().encode("utf-8")) > 262144:
            raise ReviewError("tool_result_too_large")
        return result
    except ReviewError as exc:
        code = (
            exc.code
            if exc.code
            in {
                "resource_unavailable",
                "tool_not_allowed",
                "invalid_tool_result",
                "tool_result_too_large",
                "service_unavailable",
            }
            else "tool_failure"
        )
        raise ToolError(code) from None
    except Exception:
        raise ToolError("tool_failure") from None


def build_document_mcp(
    *,
    service: DocumentService,
    scopes: ScopeProvider,
    authorizer: Authorizer,
) -> MCPServer:
    """Identity must come from verified per-request auth; never a process-global user."""
    server = MCPServer("contract-document", middleware=[_guard_arguments])

    @server.tool(meta=_tool_meta(
        "document.parse",
        agent_visible=False,
        resource_path="/request/document_version_id",
    ))
    async def document_parse(request: VersionInput) -> JobResult:
        """为已登记原件创建幂等版面分析任务；固定上传流程调用。"""
        return await _invoke(
            scopes=scopes,
            authorizer=authorizer,
            action="document_parse",
            resource=request.document_version_id,
            version=request.document_version_id,
            call=lambda scope: service.parse(scope, request),
            result_type=JobResult,
        )

    @server.tool(meta=_tool_meta(
        "document.job_status",
        resource_path="/request/job_id",
    ))
    async def document_get_job_status(request: JobInput) -> JobResult:
        """查询授权文档任务状态；不得由模型持续轮询。"""
        return await _invoke(
            scopes=scopes,
            authorizer=authorizer,
            action="document_get_job_status",
            resource=request.job_id,
            version=None,
            call=lambda scope: service.job_status(scope, request),
            result_type=JobResult,
        )

    @server.tool(meta=_tool_meta(
        "document.structure",
        resource_path="/request/document_version_id",
    ))
    async def document_get_structure(request: StructureInput) -> StructureResult:
        """分页读取文档块的阅读顺序及识别覆盖；游标非空表示尚未读完。"""
        return await _invoke(
            scopes=scopes,
            authorizer=authorizer,
            action="document_get_structure",
            resource=request.document_version_id,
            version=request.document_version_id,
            call=lambda scope: service.structure(scope, request),
            result_type=StructureResult,
        )

    @server.tool(meta=_tool_meta(
        "document.read_blocks",
        resource_path="/request/document_version_id",
    ))
    async def document_read_blocks(request: ReadBlocksInput) -> BlocksResult:
        """读取结构清单内的原文及位置；不能将识别文本当作现行法律。"""
        return await _invoke(
            scopes=scopes,
            authorizer=authorizer,
            action="document_read_blocks",
            resource=request.document_version_id,
            version=request.document_version_id,
            call=lambda scope: service.read_blocks(scope, request),
            result_type=BlocksResult,
        )

    @server.tool(meta=_tool_meta(
        "document.inspect_region",
        resource_path="/request/document_version_id",
    ))
    async def document_inspect_region(request: RegionInput) -> JobResult:
        """提交指定区域的有界视觉复核任务；只接受既有区域 ID。"""
        return await _invoke(
            scopes=scopes,
            authorizer=authorizer,
            action="document_inspect_region",
            resource=request.document_version_id,
            version=request.document_version_id,
            call=lambda scope: service.inspect_region(scope, request),
            result_type=JobResult,
        )

    return server


def build_legal_mcp(
    *,
    service: LegalService,
    scopes: ScopeProvider,
    authorizer: Authorizer,
) -> MCPServer:
    server = MCPServer("legal-retrieval", middleware=[_guard_arguments])

    @server.tool(meta=_tool_meta(
        "legal.search",
        resource_constant="public-law",
    ))
    async def legal_search(request: SearchInput) -> SearchResult:
        """按地域和适用日期检索法规候选；结果仍须引用与语义支持校验。"""
        return await _invoke(
            scopes=scopes,
            authorizer=authorizer,
            action="legal_search",
            resource="public-law",
            version=None,
            call=lambda scope: service.search(scope, request),
            result_type=SearchResult,
        )

    @server.tool(meta=_tool_meta(
        "legal.read",
        resource_path="/request/evidence_id",
    ))
    async def legal_read(request: EvidenceInput) -> LegalEvidence:
        """读取授权证据全文、来源、版本和效力状态；未知状态保留未知。"""
        return await _invoke(
            scopes=scopes,
            authorizer=authorizer,
            action="legal_read",
            resource=request.evidence_id,
            version=None,
            call=lambda scope: service.read(scope, request),
            result_type=LegalEvidence,
        )

    return server
