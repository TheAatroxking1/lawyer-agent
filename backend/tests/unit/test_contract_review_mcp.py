from uuid import uuid4

import pytest

pytest.importorskip("mcp", reason="requires contract-review extra")
from mcp import Client  # noqa: E402

from lawyer_agent.application.contract_review.contracts import (  # noqa: E402
    BlocksResult,
    ReadBlocksInput,
    ReviewError,
    ReviewScope,
)


class Access:
    def __init__(self) -> None:
        self.scope = ReviewScope(
            tenant_id=uuid4(),
            actor_id=uuid4(),
            run_id=uuid4(),
            document_version_id="doc-1",
        )
        self.allowed_tenant = self.scope.tenant_id
        self.calls: list[str] = []

    async def current(self):
        return self.scope

    async def require(self, scope, action, resource_id):
        self.calls.append(action)
        if scope.tenant_id != self.allowed_tenant:
            raise ReviewError("resource_unavailable")


class Documents:
    def __init__(self) -> None:
        self.calls = 0
        self.wrong_version = False
        self.fail = False
        self.invalid_result = False

    async def read_blocks(self, scope, request: ReadBlocksInput):
        self.calls += 1
        if self.fail:
            raise RuntimeError("PRIVATE_PROVIDER_PAYLOAD")
        result = BlocksResult(
            document_version_id="other" if self.wrong_version else request.document_version_id,
            blocks=(),
        )
        if self.invalid_result:
            return result.model_copy(update={"truncated": "PRIVATE_INVALID_RESULT"})
        return result


@pytest.mark.asyncio
async def test_real_mcp_discovery_and_authorized_call():
    from lawyer_agent.infrastructure.contract_review.mcp_servers import build_document_mcp

    access, docs = Access(), Documents()
    server = build_document_mcp(service=docs, scopes=access, authorizer=access)
    async with Client(server) as client:
        listed = await client.list_tools()
        assert {t.name for t in listed.tools} == {
            "document_parse",
            "document_get_job_status",
            "document_get_structure",
            "document_read_blocks",
            "document_inspect_region",
        }
        assert "tenant_id" not in str([t.input_schema for t in listed.tools])
        catalog = {tool.name: tool for tool in listed.tools}
        assert catalog["document_parse"].meta == {
            "lawyer_agent/capability": "document.parse",
            "lawyer_agent/agent_visible": False,
            "lawyer_agent/resource_path": "/request/document_version_id",
        }
        assert catalog["document_read_blocks"].meta == {
            "lawyer_agent/capability": "document.read_blocks",
            "lawyer_agent/agent_visible": True,
            "lawyer_agent/resource_path": "/request/document_version_id",
        }
        assert catalog["document_read_blocks"].description
        assert catalog["document_read_blocks"].output_schema is not None
        result = await client.call_tool(
            "document_read_blocks",
            {
                "request": {"document_version_id": "doc-1", "block_ids": ["b1"]},
            },
        )
        assert not result.is_error
        assert result.structured_content["document_version_id"] == "doc-1"
    assert docs.calls == 1
    assert access.calls == ["document_read_blocks"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["tenant", "version", "forged", "backend", "wrong_result"])
async def test_mcp_denies_or_sanitizes_failures(kind):
    from lawyer_agent.infrastructure.contract_review.mcp_servers import build_document_mcp

    access, docs = Access(), Documents()
    if kind == "tenant":
        access.scope = access.scope.model_copy(update={"tenant_id": uuid4()})
    docs.fail = kind == "backend"
    docs.wrong_version = kind == "wrong_result"
    request = {
        "document_version_id": "other" if kind == "version" else "doc-1",
        "block_ids": ["b1"],
    }
    if kind == "forged":
        request["tenant_id"] = str(uuid4())
    async with Client(build_document_mcp(service=docs, scopes=access, authorizer=access)) as client:
        result = await client.call_tool("document_read_blocks", {"request": request})
        assert result.is_error
        assert "PRIVATE_PROVIDER_PAYLOAD" not in str(result)
    if kind in {"tenant", "version", "forged"}:
        assert docs.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"request": {"document_version_id": "doc-1", "block_ids": ["b1"]}, "tenant_id": "other"},
        {"request": {"document_version_id": "doc-1", "block_ids": "SECRET_SENTINEL"}},
    ],
)
async def test_sdk_envelope_rejection_never_echoes_input(arguments, caplog):
    from lawyer_agent.infrastructure.contract_review.mcp_servers import build_document_mcp

    access, docs = Access(), Documents()
    async with Client(build_document_mcp(service=docs, scopes=access, authorizer=access)) as client:
        result = await client.call_tool("document_read_blocks", arguments)
        assert result.is_error
        assert "SECRET_SENTINEL" not in str(result)
        assert "SECRET_SENTINEL" not in caplog.text
        assert docs.calls == 0


@pytest.mark.asyncio
async def test_revalidate_provider_model_instances(caplog, recwarn):
    from lawyer_agent.infrastructure.contract_review.mcp_servers import build_document_mcp

    access, docs = Access(), Documents()
    docs.invalid_result = True
    async with Client(build_document_mcp(service=docs, scopes=access, authorizer=access)) as client:
        result = await client.call_tool(
            "document_read_blocks",
            {
                "request": {"document_version_id": "doc-1", "block_ids": ["b1"]},
            },
        )
        assert result.is_error
        assert "PRIVATE_INVALID_RESULT" not in str(result) + caplog.text + str(list(recwarn))
