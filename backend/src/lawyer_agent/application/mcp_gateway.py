"""Controlled MCP client gateway for agent tool calls (non-model).

spec: MCP runs through a *controlled Client Gateway* that enhances the agent;
first-phase capabilities must not depend on any concrete external MCP server.
This module is that gateway: a registrar holds the only tools an agent may
invoke, each with a strict input schema, and the gateway dispatches by exact
name after whitelist + argument validation. Unknown tools, tools outside the
enabled allowlist and malformed arguments are rejected with stable codes —
nothing is silently coerced, dynamically imported or executed.
"""

from __future__ import annotations

import inspect
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_SCHEMA_OBJECT_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties", "description"}
)
_PROPERTY_KEYS = frozenset(
    {"type", "description", "items", "enum"}
)
_ALLOWED_TYPES = frozenset(
    {"string", "integer", "number", "boolean", "array", "object", "null"}
)

_logger = logging.getLogger("lawyer_agent.mcp_gateway")


class MCPGatewayError(ValueError):
    """Stable gateway failure carrying a code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One whitelisted agent tool: identity, description and input schema."""

    name: str
    description: str
    input_schema: dict[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _TOOL_NAME.fullmatch(self.name) is None:
            raise MCPGatewayError(
                "invalid_tool_spec",
                "tool name must match [a-z][a-z0-9_.]{0,63}",
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise MCPGatewayError("invalid_tool_spec", "tool description must be non-empty")
        if len(self.description) > 512:
            raise MCPGatewayError("invalid_tool_spec", "tool description is too long")
        _validate_schema(self.input_schema)


@dataclass(frozen=True, slots=True)
class ToolResult:
    """Structured outcome of a gateway tool call (never echoes raw args)."""

    ok: bool
    output: Any | None = None
    error_code: str | None = None
    error_message: str | None = None


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MCPGatewayError("invalid_tool_spec", f"{label} must be an object")
    return value


def _validate_schema(schema: dict[str, Any]) -> None:
    root = _require_object(schema, "input_schema")
    unknown = set(root) - _SCHEMA_OBJECT_KEYS
    if unknown:
        raise MCPGatewayError(
            "invalid_tool_spec", "input_schema contains unsupported keys"
        )
    if root.get("type") != "object":
        raise MCPGatewayError("invalid_tool_spec", "input_schema type must be object")
    properties = root.get("properties")
    if properties is not None:
        props = _require_object(properties, "input_schema.properties")
        for prop_name, raw in props.items():
            _validate_property(prop_name, raw)
    required = root.get("required", [])
    if not isinstance(required, list) or not all(
        isinstance(item, str) for item in required
    ):
        raise MCPGatewayError("invalid_tool_spec", "required must be a list of names")
    if len(set(required)) != len(required):
        raise MCPGatewayError("invalid_tool_spec", "required names must be unique")
    for name in required:
        if properties is None or name not in properties:
            raise MCPGatewayError(
                "invalid_tool_spec", "required property is not declared"
            )
    extra_allowed = root.get("additionalProperties", False)
    if not isinstance(extra_allowed, bool):
        raise MCPGatewayError("invalid_tool_spec", "additionalProperties must be boolean")


def _validate_property(name: str, raw: Any) -> None:
    prop = _require_object(raw, f"input_schema.properties.{name}")
    unknown = set(prop) - _PROPERTY_KEYS
    if unknown:
        raise MCPGatewayError(
            "invalid_tool_spec", f"property {name} contains unsupported keys"
        )
    prop_type = prop.get("type")
    if prop_type not in _ALLOWED_TYPES:
        raise MCPGatewayError("invalid_tool_spec", f"property {name} has an invalid type")
    if prop_type == "array":
        items = prop.get("items")
        if not isinstance(items, dict) or items.get("type") not in _ALLOWED_TYPES:
            raise MCPGatewayError(
                "invalid_tool_spec", f"property {name} array items are invalid"
            )
    enum = prop.get("enum")
    if enum is not None:
        if not isinstance(enum, list) or not enum:
            raise MCPGatewayError("invalid_tool_spec", f"property {name} enum is invalid")
        for value in enum:
            if not _type_ok(value, str(prop_type)):
                raise MCPGatewayError(
                    "invalid_tool_spec",
                    f"property {name} enum value does not match the declared type",
                )


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "null":
        return value is None
    return False


def _validate_value(name: str, raw: Any, prop: dict[str, Any]) -> None:
    prop_type = prop.get("type")
    if prop_type == "null":
        if raw is not None:
            raise MCPGatewayError(
                "invalid_arguments", f"argument {name} must be null"
            )
        return
    if not _type_ok(raw, str(prop_type)):
        raise MCPGatewayError(
            "invalid_arguments", f"argument {name} must be of type {prop_type}"
        )
    if prop_type == "array":
        items = prop.get("items", {})
        item_type = items.get("type") if isinstance(items, dict) else None
        if item_type in _ALLOWED_TYPES:
            for index, entry in enumerate(raw):
                if not _type_ok(entry, str(item_type)):
                    raise MCPGatewayError(
                        "invalid_arguments",
                        f"argument {name}[{index}] must be of type {item_type}",
                    )
    enum = prop.get("enum")
    if enum is not None and raw not in enum:
        raise MCPGatewayError("invalid_arguments", f"argument {name} is not an allowed value")


def validate_args(spec: ToolSpec, args: Any) -> dict[str, Any]:
    """Validates raw arguments against the tool schema; raises MCPGatewayError."""
    if not isinstance(args, dict):
        raise MCPGatewayError("invalid_arguments", "arguments must be an object")
    properties = spec.input_schema.get("properties", {})
    extra_allowed = spec.input_schema.get("additionalProperties", False)
    if not extra_allowed:
        extra = set(args) - set(properties)
        if extra:
            raise MCPGatewayError(
                "invalid_arguments", "unknown argument(s) supplied"
            )
    required = spec.input_schema.get("required", [])
    missing = [name for name in required if name not in args]
    if missing:
        raise MCPGatewayError("invalid_arguments", "missing required argument(s)")
    for name, raw in args.items():
        prop = properties.get(name)
        if prop is None:
            if extra_allowed:
                continue
            raise MCPGatewayError("invalid_arguments", "unknown argument(s) supplied")
        _validate_value(name, raw, prop)
    return dict(args)


Handler = Callable[[dict[str, Any]], object]


class AllowedToolRegistry:
    """The closed set of tools the gateway may invoke."""

    def __init__(self) -> None:
        self._tools: dict[str, tuple[ToolSpec, Handler]] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        handler: Handler,
    ) -> ToolSpec:
        spec = ToolSpec(name=name, description=description, input_schema=input_schema)
        if spec.name in self._tools:
            raise MCPGatewayError("duplicate_tool", "tool is already registered")
        if not callable(handler):
            raise MCPGatewayError("invalid_tool_spec", "tool handler must be callable")
        self._tools[spec.name] = (spec, handler)
        return spec

    def spec(self, name: str) -> ToolSpec | None:
        entry = self._tools.get(name)
        return None if entry is None else entry[0]

    def handler(self, name: str) -> Handler | None:
        entry = self._tools.get(name)
        return None if entry is None else entry[1]

    def ensure_meta(self) -> None:
        """Register the built-in introspection tool unless already present."""
        if "meta.list_tools" not in self._tools:
            self.register(
                name="meta.list_tools",
                description="列出当前网关内全部可用工具的名称与说明",
                input_schema={"type": "object", "properties": {}},
                handler=_list_tools(self),
            )

    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(
            entry[0]
            for entry in sorted(self._tools.values(), key=lambda pair: pair[0].name)
        )


def _list_tools(registry: AllowedToolRegistry) -> Callable[[dict[str, Any]], object]:
    def handle(_args: dict[str, Any]) -> object:
        return [
            {"name": spec.name, "description": spec.description}
            for spec in registry.specs()
        ]

    return handle


class MCPClientGateway:
    """Whitelist-gated dispatcher: exact name + schema-validated arguments.

    Unregistered or disallowed tools, malformed arguments and handler failures
    all return stable ``ToolResult`` error codes; the gateway never fabricates
    output and never runs anything outside the registry.
    """

    def __init__(
        self,
        registry: AllowedToolRegistry,
        *,
        allowlist: Sequence[str] | None = None,
    ) -> None:
        if not isinstance(registry, AllowedToolRegistry):
            raise TypeError("mcp gateway requires an AllowedToolRegistry")
        self._registry = registry
        self._allowlist = None if allowlist is None else frozenset(allowlist)
        registry.ensure_meta()

    def allowed(self, name: str) -> bool:
        if self._registry.spec(name) is None:
            return False
        if self._allowlist is not None and name not in self._allowlist:
            return False
        return True

    def specs(self) -> tuple[ToolSpec, ...]:
        """Read-only projection of the tools callers may currently invoke.

        When an allowlist is configured only the allowed subset is shown, so
        callers never learn about registry tools that are not enabled for them.
        """
        if self._allowlist is None:
            return self._registry.specs()
        return tuple(
            spec
            for spec in self._registry.specs()
            if spec.name in self._allowlist
        )

    def call(self, name: str, args: Any) -> ToolResult:
        started = time.perf_counter()
        spec = self._registry.spec(name)
        if spec is None:
            return ToolResult(ok=False, error_code="unknown_tool", error_message="unknown tool")
        if self._allowlist is not None and name not in self._allowlist:
            return ToolResult(
                ok=False, error_code="tool_not_allowed", error_message="tool is not allowed"
            )
        try:
            validated = validate_args(spec, args)
            handler = self._registry.handler(name)
            if handler is None:
                return ToolResult(
                    ok=False, error_code="unknown_tool", error_message="unknown tool"
                )
            output = handler(validated)
        except MCPGatewayError as exc:
            return ToolResult(ok=False, error_code=exc.code, error_message=exc.message)
        except Exception:  # noqa: BLE001 - handler boundary
            _logger.warning(
                "mcp_tool_call name=%s ok=false code=handler_failure latency_ms=%d",
                name,
                int((time.perf_counter() - started) * 1000),
            )
            return ToolResult(
                ok=False,
                error_code="handler_failure",
                error_message="tool handler failed",
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _logger.info(
            "mcp_tool_call name=%s ok=true latency_ms=%d", name, elapsed_ms
        )
        return ToolResult(ok=True, output=output)

    async def call_async(self, name: str, args: Any) -> ToolResult:
        """Async dispatch: awaits handlers that return awaitables.

        Registries may mix sync and async handlers; sync results pass through.
        """
        started = time.perf_counter()
        spec = self._registry.spec(name)
        if spec is None:
            return ToolResult(ok=False, error_code="unknown_tool", error_message="unknown tool")
        if self._allowlist is not None and name not in self._allowlist:
            return ToolResult(
                ok=False, error_code="tool_not_allowed", error_message="tool is not allowed"
            )
        try:
            validated = validate_args(spec, args)
            handler = self._registry.handler(name)
            if handler is None:
                return ToolResult(
                    ok=False, error_code="unknown_tool", error_message="unknown tool"
                )
            output = handler(validated)
            if inspect.isawaitable(output):
                output = await output
        except MCPGatewayError as exc:
            return ToolResult(ok=False, error_code=exc.code, error_message=exc.message)
        except Exception:  # noqa: BLE001 - handler boundary
            _logger.warning(
                "mcp_tool_call name=%s ok=false code=handler_failure latency_ms=%d",
                name,
                int((time.perf_counter() - started) * 1000),
            )
            return ToolResult(
                ok=False,
                error_code="handler_failure",
                error_message="tool handler failed",
            )
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _logger.info(
            "mcp_tool_call name=%s ok=true latency_ms=%d", name, elapsed_ms
        )
        return ToolResult(ok=True, output=output)
