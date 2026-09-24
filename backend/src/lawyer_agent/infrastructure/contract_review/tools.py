"""Dynamically discovered MCP tools with bounded execution and authorization."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from llama_index.core.tools import FunctionTool
from llama_index.tools.mcp import McpToolSpec  # type: ignore[import-untyped]
from mcp import Client

from lawyer_agent.application.contract_review.contracts import (
    BlocksResult,
    DocumentBlock,
    ReviewError,
    ReviewScope,
    RunLimits,
    StructureResult,
)
from lawyer_agent.application.contract_review.ports import Authorizer
from lawyer_agent.application.contract_review.research import ResearchBudget


@dataclass(frozen=True)
class _DiscoveredTool:
    client: Client
    name: str
    capability: str
    description: str
    input_validator: Draft202012Validator
    output_validator: Draft202012Validator
    resource_path: str | None
    resource_constant: str | None


class ControlledMCPTools:
    """Client gateway whose tool contract comes exclusively from trusted MCP servers."""

    def __init__(
        self,
        *,
        scope: ReviewScope,
        authorizer: Authorizer,
        document: Client,
        legal: Client,
        limits: RunLimits | None = None,
        allow_partial_document: bool = False,
    ) -> None:
        self.scope, self.authorizer = scope, authorizer
        self.document, self.legal = document, legal
        self.limits = limits or RunLimits()
        self.allow_partial_document = allow_partial_document
        self.tools: list[FunctionTool] = []
        self._discovered: dict[str, _DiscoveredTool] = {}
        self._capabilities: dict[str, str] = {}
        self._budget = ResearchBudget(limits=self.limits)
        self._cache: dict[str, str] = {}
        self._cache_bytes = 0
        self._catalog: list[dict[str, str]] = []

    async def discover(self) -> None:
        """Discover visible tools; servers are the sole source of their JSON Schema."""
        visible: list[FunctionTool] = []
        discovered_tools: dict[str, _DiscoveredTool] = {}
        capabilities: dict[str, str] = {}
        for client in (self.document, self.legal):
            listed = (await client.list_tools()).tools
            remote_by_name = {tool.name: tool for tool in listed}
            if len(remote_by_name) != len(listed):
                raise ReviewError("mcp_schema_invalid")
            allowed: list[str] = []
            for remote in listed:
                meta = remote.meta or {}
                if meta.get("lawyer_agent/agent_visible") is not True:
                    continue
                capability = meta.get("lawyer_agent/capability")
                resource_path = meta.get("lawyer_agent/resource_path")
                resource_constant = meta.get("lawyer_agent/resource_constant")
                if (
                    not isinstance(capability, str)
                    or not capability
                    or (resource_path is None) == (resource_constant is None)
                    or (resource_path is not None and not isinstance(resource_path, str))
                    or (resource_constant is not None and not isinstance(resource_constant, str))
                    or remote.output_schema is None
                ):
                    raise ReviewError("mcp_schema_invalid")
                if remote.name in discovered_tools or capability in capabilities:
                    raise ReviewError("mcp_schema_invalid")
                try:
                    Draft202012Validator.check_schema(remote.input_schema)
                    Draft202012Validator.check_schema(remote.output_schema)
                except SchemaError:
                    raise ReviewError("mcp_schema_invalid") from None
                discovered_tools[remote.name] = _DiscoveredTool(
                    client=client,
                    name=remote.name,
                    capability=capability,
                    description=(getattr(remote, "description", None) or remote.name)[:500],
                    input_validator=Draft202012Validator(remote.input_schema),
                    output_validator=Draft202012Validator(remote.output_schema),
                    resource_path=resource_path,
                    resource_constant=resource_constant,
                )
                capabilities[capability] = remote.name
                allowed.append(remote.name)
            if not allowed:
                continue
            dynamic_tools = await McpToolSpec(
                client=client.session,
                allowed_tools=sorted(allowed),
            ).to_tool_list_async()
            if {tool.metadata.get_name() for tool in dynamic_tools} != set(allowed):
                raise ReviewError("mcp_tools_unavailable")
            for dynamic in dynamic_tools:
                name = dynamic.metadata.get_name()
                fn_schema = dynamic.metadata.fn_schema
                remote = remote_by_name[name]
                if fn_schema is None:
                    raise ReviewError("mcp_schema_invalid")
                visible.append(FunctionTool.from_defaults(
                    name=name,
                    description=remote.description or name,
                    fn_schema=fn_schema,
                    async_fn=self._wrapper(name),
                ))
        self._discovered = discovered_tools
        self._capabilities = capabilities
        self.tools = visible
        self._catalog = sorted(({
            "name": item.name,
            "capability": item.capability,
            "description": item.description[:500],
            "status": "discovered",
        } for item in discovered_tools.values()), key=lambda item: item["name"])

    def catalog(self) -> list[dict[str, str]]:
        return [dict(item) for item in self._catalog]

    def _wrapper(self, name: str) -> Any:
        async def invoke(**kwargs: Any) -> str:
            return await self.call(name, kwargs)

        return invoke

    def reset(self) -> None:
        self._budget = ResearchBudget(limits=self.limits)
        self._cache.clear()
        self._cache_bytes = 0

    @property
    def accounts_for_tool_budget(self) -> bool:
        return True

    def bind_budget(self, budget: ResearchBudget) -> None:
        if budget.limits != self.limits:
            raise ReviewError("budget_mismatch")
        self._budget = budget

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Return the model observation while authoritative data stays private."""
        raw = await self._call(name, arguments)
        discovered = self._discovered[name]
        if discovered.capability == "document.read_blocks":
            result = BlocksResult.model_validate_json(raw)
            return json.dumps({
                "document_version_id": result.document_version_id,
                "blocks": [_model_block(block) for block in result.blocks],
                "truncated": result.truncated,
            }, ensure_ascii=False, separators=(",", ":"))
        return raw

    async def _call(self, name: str, arguments: dict[str, Any]) -> str:
        discovered = self._discovered.get(name)
        if discovered is None:
            raise ReviewError("tool_not_allowed")
        try:
            discovered.input_validator.validate(arguments)
            resource = _resource_from(arguments, discovered)
        except (JsonSchemaValidationError, KeyError, TypeError, ValueError):
            raise ReviewError("invalid_tool_arguments") from None
        await self.authorizer.require(self.scope, name, resource)
        key = name + json.dumps(arguments, sort_keys=True, ensure_ascii=False)
        cacheable = discovered.capability != "document.job_status"
        if cacheable and key in self._cache:
            return self._cache[key]
        self._budget.consume_tool()
        try:
            result = await discovered.client.call_tool(name, arguments)
            if result.is_error or result.structured_content is None:
                raise ReviewError("mcp_call_failed")
            discovered.output_validator.validate(result.structured_content)
        except ReviewError:
            raise
        except JsonSchemaValidationError:
            raise ReviewError("invalid_tool_result") from None
        except Exception:
            raise ReviewError("mcp_call_failed") from None
        parsed = result.structured_content
        version = parsed.get("document_version_id")
        if (
            discovered.capability.startswith("document.")
            and version is not None
            and version != self.scope.document_version_id
        ):
            raise ReviewError("invalid_tool_result")
        text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        size = len(text.encode("utf-8"))
        if size > self.limits.max_response_bytes:
            raise ReviewError("tool_result_too_large")
        if cacheable:
            if self._cache_bytes + size > self.limits.max_context_bytes:
                raise ReviewError("context_limit_exceeded")
            self._cache[key] = text
            self._cache_bytes += size
        return text

    def _capability_name(self, capability: str) -> str:
        try:
            return self._capabilities[capability]
        except KeyError:
            raise ReviewError("mcp_tools_unavailable") from None

    async def prepare(self) -> str:
        if not self.tools:
            raise ReviewError("mcp_tools_unavailable")
        structure_tool = self._capability_name("document.structure")
        read_tool = self._capability_name("document.read_blocks")
        ids: list[str] = []
        cursor: str | None = None
        cursors: set[str] = set()
        for _ in range(100):
            raw = await self._call(
                structure_tool,
                {"request": {
                    "document_version_id": self.scope.document_version_id,
                    "cursor": cursor,
                }},
            )
            page = StructureResult.model_validate_json(raw)
            if not page.recognition_complete and not self.allow_partial_document:
                raise ReviewError("document_incomplete")
            ids.extend(page.block_ids)
            if len(ids) > 2000 or len(ids) != len(set(ids)):
                raise ReviewError("invalid_document_structure")
            cursor = page.next_cursor
            if cursor is None:
                break
            if cursor in cursors:
                raise ReviewError("invalid_document_structure")
            cursors.add(cursor)
        else:
            raise ReviewError("document_too_large")
        if not ids:
            raise ReviewError("document_incomplete")
        blocks: list[dict[str, Any]] = []
        for offset in range(0, len(ids), 20):
            selected = ids[offset : offset + 20]
            raw = await self._call(
                read_tool,
                {"request": {
                    "document_version_id": self.scope.document_version_id,
                    "block_ids": selected,
                }},
            )
            result = BlocksResult.model_validate_json(raw)
            if (
                result.truncated
                or len(result.blocks) != len(selected)
                or {block.block_id for block in result.blocks} != set(selected)
                or (not self.allow_partial_document
                    and any(block.quality != "verified" for block in result.blocks))
            ):
                raise ReviewError("document_incomplete")
            blocks.extend(_model_block(block) for block in result.blocks)
        return json.dumps(
            {"document_version_id": self.scope.document_version_id, "blocks": blocks},
            ensure_ascii=False,
            separators=(",", ":"),
        )


def _resource_from(arguments: dict[str, Any], tool: _DiscoveredTool) -> str:
    if tool.resource_constant is not None:
        return tool.resource_constant
    assert tool.resource_path is not None
    value: Any = arguments
    for raw_part in tool.resource_path.lstrip("/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict):
            raise TypeError("resource path does not resolve through an object")
        value = value[part]
    if not isinstance(value, str) or not value:
        raise ValueError("resource must be a non-empty string")
    return value


def _model_block(block: DocumentBlock) -> dict[str, Any]:
    # Pixels are only used by the authoritative gate and browser, never inferred by the model.
    return block.model_dump(mode="json", exclude={"anchors": {"__all__": {"quad"}}})
