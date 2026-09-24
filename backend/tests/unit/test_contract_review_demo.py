from __future__ import annotations

import json

import pytest

from lawyer_agent.application.contract_review.demo import DEMO_PDF, demo_events, demo_run


def test_demo_run_is_bounded_and_explicitly_marked():
    run = demo_run()
    payload = run.model_dump(mode="json")
    assert payload["is_demo"] is True
    assert run.status == "draft"
    assert len(run.issues) >= 2
    assert run.evidence_documents
    assert run.mcp_tools
    assert "schema" not in json.dumps(payload, ensure_ascii=False).lower()
    assert "api_key" not in json.dumps(payload, ensure_ascii=False).lower()


def test_demo_pdf_is_a_small_valid_pdf():
    assert DEMO_PDF.startswith(b"%PDF-")
    assert len(DEMO_PDF) < 64 * 1024


@pytest.mark.asyncio
async def test_demo_events_emit_all_interview_stages():
    events = [event async for event in demo_events()]
    assert events[-2]["type"] == "result"
    assert events[-2]["run"]["is_demo"] is True
    stages = [event["stage"] for event in events if event["type"] == "progress"]
    assert stages == [
        "parsing", "contract_understanding", "preparing", "researching",
        "legal_search", "legal_selection", "legal_read", "reviewing", "saved",
    ]
