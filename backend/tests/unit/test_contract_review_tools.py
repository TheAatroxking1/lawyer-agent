import json

import pytest

pytest.importorskip("llama_index.core", reason="requires contract-review extra")

from mcp import Client  # noqa: E402
from mcp.server import MCPServer  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from lawyer_agent.application.contract_review.contracts import ReviewError, RunLimits  # noqa: E402
from lawyer_agent.application.contract_review.research import ResearchBudget  # noqa: E402
from lawyer_agent.cli.contract_review_smoke import SyntheticServices  # noqa: E402
from lawyer_agent.infrastructure.contract_review.mcp_servers import (  # noqa: E402
    build_document_mcp,
    build_legal_mcp,
)
from lawyer_agent.infrastructure.contract_review.tools import ControlledMCPTools  # noqa: E402


@pytest.mark.asyncio
async def test_model_document_keeps_text_and_anchor_offsets_without_geometry():
    services = SyntheticServices()
    original = services.block.model_dump(mode="json")
    async with (
        Client(build_document_mcp(service=services, scopes=services, authorizer=services)) as dc,
        Client(build_legal_mcp(service=services, scopes=services, authorizer=services)) as lc,
    ):
        tools = ControlledMCPTools(
            scope=services.scope, authorizer=services, document=dc, legal=lc,
        )
        await tools.discover()
        prepared = json.loads(await tools.prepare())
        args = {"request": {"document_version_id": services.scope.document_version_id,
                            "block_ids": ["b1"]}}
        observed = json.loads(await tools.call("document_read_blocks", args))
        for result in (prepared, observed):
            block = result["blocks"][0]
            assert block["text"] == original["text"]
            assert block["block_id"] == original["block_id"]
            assert block["anchors"] == [{
                key: value for key, value in original["anchors"][0].items() if key != "quad"
            }]
        # The real MCP wire and canonical source retain exact geometry for the gate.
        raw = await dc.call_tool("document_read_blocks", args)
        assert raw.structured_content["blocks"][0] == original
        assert services.block.model_dump(mode="json") == original


@pytest.mark.asyncio
async def test_zero_budget_prevents_preparation_business_calls():
    services = SyntheticServices()

    async def forbidden(scope, request):
        pytest.fail("zero budget reached document service")

    services.structure = forbidden
    async with (
        Client(build_document_mcp(service=services, scopes=services, authorizer=services)) as dc,
        Client(build_legal_mcp(service=services, scopes=services, authorizer=services)) as lc,
    ):
        tools = ControlledMCPTools(
            scope=services.scope,
            authorizer=services,
            document=dc,
            legal=lc,
            limits=RunLimits(max_tool_calls=0),
        )
        await tools.discover()
        with pytest.raises(ReviewError, match="tool_limit_exceeded"):
            await tools.prepare()


@pytest.mark.asyncio
async def test_controlled_tools_use_bound_shared_budget_for_prepare_and_calls():
    services = SyntheticServices()
    async with (
        Client(build_document_mcp(service=services, scopes=services, authorizer=services)) as dc,
        Client(build_legal_mcp(service=services, scopes=services, authorizer=services)) as lc,
    ):
        limits = RunLimits(max_tool_calls=1)
        tools = ControlledMCPTools(
            scope=services.scope,
            authorizer=services,
            document=dc,
            legal=lc,
            limits=limits,
        )
        await tools.discover()
        budget = ResearchBudget(limits=limits)
        tools.bind_budget(budget)
        with pytest.raises(ReviewError, match="tool_limit_exceeded"):
            await tools.prepare()
        assert budget.tool_calls == 1


@pytest.mark.asyncio
async def test_all_seven_protocol_tools_and_typed_results():
    services = SyntheticServices()
    doc = build_document_mcp(service=services, scopes=services, authorizer=services)
    law = build_legal_mcp(service=services, scopes=services, authorizer=services)
    version = services.scope.document_version_id
    async with Client(doc) as dc, Client(law) as lc:
        cases = [
            (dc, "document_parse", {"document_version_id": version}),
            (dc, "document_get_job_status", {"job_id": "synthetic-job-1"}),
            (dc, "document_get_structure", {"document_version_id": version}),
            (dc, "document_read_blocks", {"document_version_id": version, "block_ids": ["b1"]}),
            (dc, "document_inspect_region", {"document_version_id": version, "region_id": "b1"}),
            (lc, "legal_search", {"query": "付款", "region": "中国大陆", "as_of": "2026-09-16"}),
            (lc, "legal_read", {"evidence_id": "synthetic-law-1"}),
        ]
        for client, name, request in cases:
            result = await client.call_tool(name, {"request": request})
            assert not result.is_error, name
            assert result.structured_content is not None


@pytest.mark.asyncio
async def test_caller_authorizes_the_actual_legal_resource():
    services = SyntheticServices()

    class StrictAuthorization:
        async def require(self, scope, action, resource_id):
            expected = "synthetic-law-1" if action == "legal_read" else "public-law"
            if resource_id != expected:
                raise ReviewError("resource_unavailable")

    async with (
        Client(build_document_mcp(service=services, scopes=services, authorizer=services)) as dc,
        Client(build_legal_mcp(service=services, scopes=services, authorizer=services)) as lc,
    ):
        tools = ControlledMCPTools(
            scope=services.scope, authorizer=StrictAuthorization(), document=dc, legal=lc
        )
        await tools.discover()
        await tools.call("legal_read", {"request": {"evidence_id": "synthetic-law-1"}})
        await tools.call(
            "legal_search",
            {
                "request": {
                    "query": "付款",
                    "region": "中国大陆",
                    "as_of": "2026-09-16",
                }
            },
        )


@pytest.mark.asyncio
async def test_discovery_uses_server_tool_catalog_without_client_schema_registration():
    services = SyntheticServices()
    dynamic = MCPServer("dynamic-law")

    class LookupRequest(BaseModel):
        phrase: str

    class LookupResult(BaseModel):
        answer: str

    @dynamic.tool(
        meta={
            "lawyer_agent/capability": "legal.dynamic_lookup",
            "lawyer_agent/agent_visible": True,
            "lawyer_agent/resource_constant": "public-law",
        }
    )
    async def renamed_lookup(request: LookupRequest) -> LookupResult:
        """由测试 MCP Server 动态声明的法律检索工具。"""
        return LookupResult(answer=request.phrase)

    async with (
        Client(build_document_mcp(service=services, scopes=services, authorizer=services)) as dc,
        Client(dynamic) as lc,
    ):
        tools = ControlledMCPTools(scope=services.scope, authorizer=services, document=dc, legal=lc)
        await tools.discover()
        visible = {tool.metadata.get_name(): tool for tool in tools.tools}
        assert "renamed_lookup" in visible
        assert "动态声明" in visible["renamed_lookup"].metadata.description
        schema = visible["renamed_lookup"].metadata.fn_schema.model_json_schema()
        assert schema["properties"]["request"]["$ref"].endswith("LookupRequest")
        assert json.loads(
            await tools.call("renamed_lookup", {"request": {"phrase": "租赁"}})
        ) == {"answer": "租赁"}


@pytest.mark.asyncio
async def test_toolset_scope_allowlist_and_actual_call_budget():
    services = SyntheticServices()
    async with (
        Client(build_document_mcp(service=services, scopes=services, authorizer=services)) as dc,
        Client(build_legal_mcp(service=services, scopes=services, authorizer=services)) as lc,
    ):
        tools = ControlledMCPTools(
            scope=services.scope,
            authorizer=services,
            document=dc,
            legal=lc,
            limits=RunLimits(max_tool_calls=1),
        )
        await tools.discover()
        with pytest.raises(ReviewError, match="tool_not_allowed"):
            await tools.call("document_parse", {"request": {"document_version_id": "other"}})
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await tools.call(
                "document_get_structure", {"request": {"document_version_id": "other"}}
            )
        args = {"request": {"evidence_id": "synthetic-law-1"}}
        assert await tools.call("legal_read", args) == await tools.call("legal_read", args)
        with pytest.raises(ReviewError, match="tool_limit_exceeded"):
            await tools.call(
                "document_get_structure",
                {
                    "request": {
                        "document_version_id": services.scope.document_version_id,
                    }
                },
            )
