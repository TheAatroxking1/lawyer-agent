from __future__ import annotations

import pytest

from lawyer_agent.application.mcp_gateway import (
    AllowedToolRegistry,
    MCPClientGateway,
    MCPGatewayError,
    ToolSpec,
    validate_args,
)


def _echo_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "echoed text"},
            "level": {"type": "integer", "enum": [1, 2, 3]},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text"],
        "additionalProperties": False,
    }


def _echo_registry() -> tuple[AllowedToolRegistry, ToolSpec]:
    registry = AllowedToolRegistry()
    spec = registry.register(
        name="tools.echo",
        description="回显文本",
        input_schema=_echo_schema(),
        handler=lambda args: {"echoed": args["text"], "level": args.get("level")},
    )
    return registry, spec


def test_registered_tool_invoked_with_validated_args() -> None:
    registry, _ = _echo_registry()
    gateway = MCPClientGateway(registry)
    result = gateway.call(
        "tools.echo", {"text": "你好", "level": 2, "tags": ["a", "b"]}
    )
    assert result.ok is True
    assert result.output == {"echoed": "你好", "level": 2}
    assert result.error_code is None


def test_unknown_tool_returns_stable_code() -> None:
    registry, _ = _echo_registry()
    gateway = MCPClientGateway(registry)
    result = gateway.call("tools.danger", {"x": 1})
    assert result.ok is False
    assert result.error_code == "unknown_tool"


def test_allowlist_gates_registered_tools() -> None:
    registry, _ = _echo_registry()
    gateway = MCPClientGateway(registry, allowlist=[])
    assert gateway.allowed("tools.echo") is False
    blocked = gateway.call("tools.echo", {"text": "x"})
    assert blocked.ok is False
    assert blocked.error_code == "tool_not_allowed"


def test_duplicate_registration_rejected() -> None:
    registry, _ = _echo_registry()
    with pytest.raises(MCPGatewayError) as caught:
        registry.register(
            name="tools.echo",
            description="again",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: None,
        )
    assert caught.value.code == "duplicate_tool"


@pytest.mark.parametrize(
    "name",
    ["Bad Name", "1x", "UPPER", "x" * 70],
)
def test_invalid_tool_names_rejected(name: str) -> None:
    registry = AllowedToolRegistry()
    with pytest.raises(MCPGatewayError) as caught:
        registry.register(
            name=name,
            description="x",
            input_schema={"type": "object", "properties": {}},
            handler=lambda args: None,
        )
    assert caught.value.code == "invalid_tool_spec"


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string"},
        {"type": "object", "properties": {}, "surprise": 1},
        {"type": "object", "properties": {"a": {"type": "thing"}}},
        {"type": "object", "properties": {"a": {"type": "array", "items": {"type": "blob"}}}},
        {"type": "object", "properties": {"a": {"type": "string", "extra": True}}},
        {"type": "object", "required": ["ghost"]},
        {"type": "object", "properties": {}, "additionalProperties": "yes"},
    ],
)
def test_invalid_schemas_rejected(schema: dict[str, object]) -> None:
    registry = AllowedToolRegistry()
    with pytest.raises(MCPGatewayError) as caught:
        registry.register(
            name="bad.tool",
            description="x",
            input_schema=schema,
            handler=lambda args: None,
        )
    assert caught.value.code == "invalid_tool_spec"


def test_spec_construction_rejects_bad_name_and_description() -> None:
    with pytest.raises(MCPGatewayError):
        ToolSpec(name="Bad", description="x", input_schema={"type": "object", "properties": {}})
    with pytest.raises(MCPGatewayError):
        ToolSpec(name="ok.tool", description="", input_schema={"type": "object", "properties": {}})


def test_validate_args_rejects_malformed_arguments() -> None:
    _, spec = _echo_registry()
    with pytest.raises(MCPGatewayError) as caught:
        validate_args(spec, "nope")
    assert caught.value.code == "invalid_arguments"
    with pytest.raises(MCPGatewayError):
        validate_args(spec, {"level": 1})
    with pytest.raises(MCPGatewayError):
        validate_args(spec, {"text": "x", "surprise": 1})
    with pytest.raises(MCPGatewayError):
        validate_args(spec, {"text": 7})
    with pytest.raises(MCPGatewayError):
        validate_args(spec, {"text": "x", "level": 9})
    with pytest.raises(MCPGatewayError):
        validate_args(spec, {"text": "x", "tags": ["ok", 3]})


def test_validate_args_accepts_valid_payload() -> None:
    _, spec = _echo_registry()
    normalized = validate_args(spec, {"text": "ok", "tags": []})
    assert normalized == {"text": "ok", "tags": []}


def test_handler_failure_returns_stable_message_without_echo() -> None:
    registry = AllowedToolRegistry()
    registry.register(
        name="fragile.tool",
        description="fails",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: (_ for _ in ()).throw(RuntimeError("secret: nope")),
    )
    gateway = MCPClientGateway(registry)
    result = gateway.call("fragile.tool", {})
    assert result.ok is False
    assert result.error_code == "handler_failure"
    assert result.error_message == "tool handler failed"


def test_meta_list_tools_is_registered_when_empty_and_works() -> None:
    registry = AllowedToolRegistry()
    gateway = MCPClientGateway(registry)
    result = gateway.call("meta.list_tools", {})
    assert result.ok is True
    names = [entry["name"] for entry in result.output]
    assert names == ["meta.list_tools"]


def test_specs_are_sorted_by_name() -> None:
    registry = AllowedToolRegistry()
    registry.register(
        name="z.last",
        description="z",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: None,
    )
    registry.register(
        name="a.first",
        description="a",
        input_schema={"type": "object", "properties": {}},
        handler=lambda args: None,
    )
    names = [spec.name for spec in registry.specs()]
    assert names == ["a.first", "z.last"]
