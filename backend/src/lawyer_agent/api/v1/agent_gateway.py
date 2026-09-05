"""HTTP surface for the controlled MCP client gateway (account-authenticated).

Exposes the whitelisted tool registry as a narrow, versioned API: listing the
tools an agent may call and invoking one by exact name. The gateway itself is
the single authority for allow/deny and argument validation; this layer never
executes or fabricates anything on its own.
"""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Body
from pydantic import BaseModel, ConfigDict, Field

from lawyer_agent.api.dependencies import AccountSession, Services
from lawyer_agent.api.errors import ApiProblem
from lawyer_agent.application.mcp_gateway import MCPClientGateway

router = APIRouter(prefix="/platform/agent", tags=["agent-gateway"])


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentToolInfo(StrictModel):
    name: str
    description: str


class AgentToolCallBody(StrictModel):
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)


class AgentToolCallResult(StrictModel):
    ok: bool
    output: Any | None = None
    error_code: str | None = None
    error_message: str | None = None


def _gateway(services: Services) -> MCPClientGateway:
    value = getattr(services, "mcp_gateway_http", None)
    if value is None:
        raise ApiProblem(503, "agent_gateway_unavailable", "Agent gateway is unavailable")
    return cast(MCPClientGateway, value)


@router.get("/tools", response_model=list[AgentToolInfo])
async def list_agent_tools(
    current: AccountSession,
    services: Services,
) -> list[AgentToolInfo]:
    del current
    gateway = _gateway(services)
    return [
        AgentToolInfo(name=spec.name, description=spec.description)
        for spec in gateway.specs()
    ]


@router.post("/tools/call", response_model=AgentToolCallResult)
async def call_agent_tool(
    body: Annotated[AgentToolCallBody, Body()],
    current: AccountSession,
    services: Services,
) -> AgentToolCallResult:
    del current
    gateway = _gateway(services)
    result = gateway.call(body.tool, body.args)
    return AgentToolCallResult(
        ok=result.ok,
        output=result.output,
        error_code=result.error_code,
        error_message=result.error_message,
    )
