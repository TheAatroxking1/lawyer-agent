import pytest

pytest.importorskip("llama_index.core", reason="requires contract-review extra")


@pytest.mark.asyncio
async def test_real_workflow_and_two_mcp_servers_synthetic_smoke():
    from lawyer_agent.cli.contract_review_smoke import run_smoke

    result = await run_smoke()
    assert result["synthetic"] is True
    assert result["mcp_tools"] == 7
    assert result["saved"] is True
    assert len(result["review"]["issues"]) == 1
    assert result["review"]["issues"][0]["anchor_id"] == "a1"
    assert "reasoning" not in str(result)
