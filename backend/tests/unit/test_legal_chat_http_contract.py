from __future__ import annotations

from types import SimpleNamespace

from lawyer_agent.api.v1.legal_chat import (
    ChatUsageBody,
    LegalChatBody,
    LegalChatReply,
)
from lawyer_agent.application.legal_chat import (
    LegalChatInvalidRequest,
    LegalChatProviderFailure,
    LegalChatTimeout,
    LegalChatUnavailable,
)


def test_legal_chat_body_round_trip() -> None:
    body = LegalChatBody(
        messages=[
            {"role": "system", "content": "你是法律助手。"},
            {"role": "user", "content": "违约金怎么算？"},
        ]
    )
    assert len(body.messages) == 2
    assert body.messages[1].role == "user"


def test_legal_chat_reply_round_trip() -> None:
    reply = LegalChatReply(
        text="违约金一般以实际损失为基础。",
        usage=ChatUsageBody(prompt_tokens=2, completion_tokens=3, total_tokens=5),
    )
    assert reply.usage.total_tokens == 5


def test_legal_chat_errors_carry_stable_codes() -> None:
    assert LegalChatInvalidRequest().status == 422
    assert LegalChatInvalidRequest().code == "legal_chat_invalid_request"
    assert LegalChatUnavailable().status == 503
    assert LegalChatUnavailable().code == "model_provider_unavailable"
    assert LegalChatProviderFailure().status == 502
    assert LegalChatProviderFailure().code == "model_provider_failure"
    assert LegalChatTimeout().status == 504
    assert LegalChatTimeout().code == "model_provider_timeout"


def test_legal_chat_service_present_in_composition() -> None:
    placeholder = object()
    services = SimpleNamespace(legal_chat_http=placeholder)
    assert services.legal_chat_http is placeholder
