"""MCP client adapter for bounded contract legal research."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from mcp import Client
from pydantic import TypeAdapter, ValidationError

from lawyer_agent.application.contract_review.contracts import ReviewError, ReviewScope
from lawyer_agent.application.contract_review.ports import Authorizer
from lawyer_agent.application.contract_review.research import (
    LegalCandidate,
    LegalDocumentPage,
    ResearchRegion,
)


class _CallResult(Protocol):
    is_error: bool
    structured_content: dict[str, Any] | None


@dataclass(frozen=True)
class _ResearchTool:
    name: str
    capability: str
    description: str
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator


class MCPResearchTools:
    """The authenticated MCP client is run-bound; scope is never model input."""

    def __init__(
        self,
        *,
        client: Client,
        authorizer: Authorizer,
        max_response_bytes: int = 262_144,
    ) -> None:
        self.client = client
        self.authorizer = authorizer
        if not 1024 <= max_response_bytes <= 4 * 1024 * 1024:
            raise ValueError("invalid response byte limit")
        self.max_response_bytes = max_response_bytes
        self._candidate_items = TypeAdapter(tuple[LegalCandidate, ...])
        self._tools: dict[str, _ResearchTool] = {}
        self._catalog: list[dict[str, str]] = []

    async def discover(self) -> None:
        """Resolve required semantic capabilities from the MCP server catalog."""
        required = {"legal.search_documents", "legal.read_document"}
        found: dict[str, _ResearchTool] = {}
        for remote in (await self.client.list_tools()).tools:
            meta = remote.meta or {}
            capability = meta.get("lawyer_agent/capability")
            if capability not in required or meta.get("lawyer_agent/agent_visible") is not True:
                continue
            if capability in found or remote.output_schema is None:
                raise ReviewError("mcp_schema_invalid")
            try:
                Draft202012Validator.check_schema(remote.input_schema)
                Draft202012Validator.check_schema(remote.output_schema)
            except SchemaError:
                raise ReviewError("mcp_schema_invalid") from None
            found[capability] = _ResearchTool(
                name=remote.name,
                capability=capability,
                description=(getattr(remote, "description", None) or remote.name)[:500],
                input_validator=Draft202012Validator(remote.input_schema),
                output_validator=Draft202012Validator(remote.output_schema),
            )
        if set(found) != required:
            raise ReviewError("mcp_tools_unavailable")
        self._tools = found
        self._catalog = sorted(({
            "name": item.name,
            "capability": item.capability,
            "description": item.description[:500],
            "status": "discovered",
        } for item in found.values()), key=lambda item: item["name"])

    def catalog(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._catalog]

    def _tool(self, capability: str) -> _ResearchTool:
        try:
            return self._tools[capability]
        except KeyError:
            raise ReviewError("mcp_tools_unavailable") from None

    async def search(
        self, scope: ReviewScope, query: str, *, region: ResearchRegion = "",
    ) -> tuple[LegalCandidate, ...]:
        tool = self._tool("legal.search_documents")
        await self.authorizer.require(scope, tool.name, "public-law")
        request = {"query": query}
        if region:
            request["region"] = region
        result = await self._call(tool, {"request": request})
        try:
            return self._candidate_items.validate_python(result["items"])
        except (KeyError, ValidationError, TypeError):
            raise ReviewError("invalid_tool_result") from None

    async def read(
        self, scope: ReviewScope, document_id: str, cursor: str | None
    ) -> LegalDocumentPage:
        tool = self._tool("legal.read_document")
        await self.authorizer.require(scope, tool.name, document_id)
        result = await self._call(
            tool,
            {"request": {"document_id": document_id, "cursor": cursor}},
        )
        try:
            return LegalDocumentPage.model_validate(result)
        except ValidationError:
            raise ReviewError("invalid_tool_result") from None

    async def _call(
        self, tool: _ResearchTool, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            tool.input_validator.validate(arguments)
        except JsonSchemaValidationError:
            raise ReviewError("invalid_tool_arguments") from None
        try:
            result: _CallResult = await self.client.call_tool(tool.name, arguments)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise ReviewError("mcp_call_failed") from None
        if result.is_error or result.structured_content is None:
            raise ReviewError("mcp_call_failed")
        try:
            tool.output_validator.validate(result.structured_content)
        except JsonSchemaValidationError:
            raise ReviewError("invalid_tool_result") from None
        try:
            size = len(
                json.dumps(result.structured_content, ensure_ascii=False).encode("utf-8")
            )
        except (TypeError, ValueError, RecursionError):
            raise ReviewError("invalid_tool_result") from None
        if size > self.max_response_bytes:
            raise ReviewError("tool_result_too_large")
        return result.structured_content
