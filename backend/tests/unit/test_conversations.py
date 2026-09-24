from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import anyio
import anyio.lowlevel
import pytest

from lawyer_agent.application.conversations import (
    ConversationService,
    MessageInput,
    PreparedTurn,
    TurnLease,
)
from lawyer_agent.application.legal_chat import LegalChatHttpService
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.model_gateway import ChatMessage
from lawyer_agent.infrastructure.persistence.conversations import _contract_review_context


def turn() -> PreparedTurn:
    return PreparedTurn(
        lease=TurnLease(new_uuid7(), new_uuid7(), new_uuid7(), uuid4(), "lease-token"),
        messages=(ChatMessage(role="user", content="数据库历史"),),
        assistant_id=new_uuid7(),
        content="",
        status="streaming",
    )


def test_client_cannot_supply_assistant_history():
    with pytest.raises(ValueError):
        MessageInput(content="问题", request_id=uuid4(), messages=[{"role": "assistant"}])


def test_contract_followup_context_is_bounded_and_contains_review_findings_only():
    review = SimpleNamespace(
        filename="公寓协议.pdf",
        instruction="帮我审阅",
        status="draft",
        result_json={
            "issues": [
                {
                    "quote": "押金概不退还",
                    "problem": "未区分违约原因",
                    "suggestion": "约定可扣除项目及退还期限",
                    "evidence_passages": [
                        {"quote": "格式条款应提示说明", "document_id": "law-1"}
                    ],
                }
            ]
        },
        evidence_manifest={"documents": [{"document_id": "law-1", "title": "民法典"}]},
        parsed_document={"blocks": [{"text": "不应把整份合同重新放入上下文" * 5000}]},
    )

    context = _contract_review_context(review)

    assert "公寓协议.pdf" in context
    assert "押金概不退还" in context
    assert "格式条款应提示说明" in context
    assert "民法典" in context
    assert "不应把整份合同重新放入上下文" not in context
    assert len(context) <= 24000


def test_stream_persists_before_emitting_and_completes():
    async def exercise():
        stored = []

        class Gateway:
            async def chat_stream(self, **kwargs):
                assert kwargs["messages"] == prepared.messages
                yield "真实"
                yield "答案"

        repo = SimpleNamespace(
            append=AsyncMock(side_effect=lambda lease, text: stored.append(text)),
            finish=AsyncMock(),
        )
        prepared = turn()
        service = ConversationService(repo, LegalChatHttpService(Gateway()))
        events = []
        async for event in service.stream(prepared):
            if event.kind == "delta":
                assert "".join(stored).endswith(event.text)
            events.append(event)
        assert events[-1].status == "completed"
        repo.finish.assert_awaited_once_with(prepared.lease, "completed")

    asyncio.run(exercise())


def test_close_preserves_partial_answer_as_incomplete():
    async def exercise():
        class Gateway:
            async def chat_stream(self, **kwargs):
                yield "已生成片段"
                await asyncio.sleep(100)

        repo = SimpleNamespace(append=AsyncMock(), finish=AsyncMock())
        prepared = turn()
        stream = ConversationService(repo, LegalChatHttpService(Gateway())).stream(prepared)
        await anext(stream)  # started
        assert (await anext(stream)).text == "已生成片段"
        await stream.aclose()
        repo.finish.assert_awaited_once_with(prepared.lease, "incomplete")

    asyncio.run(exercise())


def test_completed_replay_never_calls_provider():
    async def exercise():
        prepared = PreparedTurn(None, (), new_uuid7(), "原答案", "completed")
        repo = SimpleNamespace(append=AsyncMock(), finish=AsyncMock())
        events = [
            e async for e in ConversationService(repo, LegalChatHttpService(None)).stream(prepared)
        ]
        assert [e.text for e in events if e.kind == "delta"] == ["原答案"]
        assert events[-1].status == "completed"
        repo.finish.assert_not_called()

    asyncio.run(exercise())


def test_provider_failure_records_actual_prefix_and_incomplete_done():
    async def exercise():
        class Gateway:
            async def chat_stream(self, **kwargs):
                yield "片段"
                raise RuntimeError("private-provider-body")

        repo = SimpleNamespace(append=AsyncMock(), finish=AsyncMock())
        prepared = turn()
        events = [
            e
            async for e in ConversationService(repo, LegalChatHttpService(Gateway())).stream(
                prepared
            )
        ]
        assert events[-1].status == "incomplete"
        assert next(e for e in events if e.kind == "error").code == "conversation_stream_failed"
        assert "private-provider-body" not in str(events)
        repo.append.assert_awaited_once_with(prepared.lease, "片段")
        repo.finish.assert_awaited_once_with(prepared.lease, "incomplete")

    asyncio.run(exercise())


def test_server_history_and_answer_preserve_long_assistant_messages():
    async def exercise():
        long_answer = "长答案" * 3000

        class Gateway:
            async def chat_stream(self, **kwargs):
                assert kwargs["messages"][-1].content == long_answer
                yield long_answer

        repo = SimpleNamespace(append=AsyncMock(), finish=AsyncMock())
        original = turn()
        prepared = PreparedTurn(
            original.lease,
            (ChatMessage(role="assistant", content=long_answer),),
            original.assistant_id,
            "",
            "streaming",
        )
        events = [
            e
            async for e in ConversationService(repo, LegalChatHttpService(Gateway())).stream(
                prepared
            )
        ]
        assert events[-1].status == "completed"
        assert "".join(e.text for e in events if e.kind == "delta") == long_answer

    asyncio.run(exercise())


def test_disconnect_cancellation_shields_incomplete_persistence():
    async def exercise():
        saved = []

        class Gateway:
            async def chat_stream(self, **kwargs):
                yield "前缀"
                await anyio.sleep_forever()

        async def finish(lease, status):
            await anyio.lowlevel.checkpoint()
            saved.append(status)

        repo = SimpleNamespace(append=AsyncMock(), finish=finish)
        service = ConversationService(repo, LegalChatHttpService(Gateway()))
        with anyio.CancelScope() as scope:
            async for event in service.stream(turn()):
                if event.kind == "delta":
                    scope.cancel()
        assert saved == ["incomplete"]

    asyncio.run(exercise())
