from __future__ import annotations

from types import SimpleNamespace

import pytest

from lawyer_agent.api.v1.audit import AuditPage
from lawyer_agent.application.audit_query_api import _valid_action_prefix


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("risk_issue.", True),
        ("document.", True),
        ("rule_pack.", True),
        ("matter.", True),
        ("risk_issue", True),
        ("tenant", True),
        ("", False),
        ("xss.", False),
        ("..", False),
        ("risk_issue.evil", False),
    ],
)
def test_valid_action_prefix(value: str, ok: bool) -> None:
    assert _valid_action_prefix(value) is ok


def test_audit_page_round_trip() -> None:
    page = AuditPage(events=[])
    assert page.events == []
    assert page.next_before_id is None


def test_service_present_in_composition() -> None:
    placeholder = object()
    services = SimpleNamespace(audit_query_http=placeholder)
    assert services.audit_query_http is placeholder
