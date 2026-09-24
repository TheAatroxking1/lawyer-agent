from __future__ import annotations

import asyncio
from datetime import date, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.application.conversations import ConversationError
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.infrastructure.contract_review.storage import ContractRunRepository
from lawyer_agent.infrastructure.persistence.conversations import ConversationRepository
from lawyer_agent.infrastructure.persistence.models import (
    AuthSessionModel,
    ConversationMessageModel,
    ConversationModel,
)
from tests.integration.mysql.test_contract_review_repository import NOW, _config, _seed_actor

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


async def exercise(url):
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessions.begin() as session:
            owner = (await _seed_actor(session, label="chat-owner")).actor
            other = (
                await _seed_actor(session, tenant_id=owner.context.tenant_id, label="chat-other")
            ).actor
            foreign = (await _seed_actor(session, label="chat-foreign")).actor
        repo = ConversationRepository(sessions, now=NOW)
        conversation = await repo.create(owner, "历史测试")
        for actor in (other, foreign):
            assert (await repo.list(actor)).items == ()
            with pytest.raises(ConversationError, match="resource_unavailable"):
                await repo.get(actor, conversation.id)
            with pytest.raises(ConversationError, match="resource_unavailable"):
                await repo.start(actor, conversation.id, "越权", uuid4())
        first_id = uuid4()
        first = await repo.start(owner, conversation.id, "第一问", first_id)
        with pytest.raises(ConversationError, match="conversation_busy"):
            await repo.start(owner, conversation.id, "同时发送", uuid4())
        await repo.append(first.lease, "未完成回答")
        await repo.finish(first.lease, "incomplete")
        replay = await repo.start(owner, conversation.id, "第一问", first_id)
        assert replay.lease is None and replay.status == "incomplete"
        with pytest.raises(ConversationError, match="idempotency_conflict"):
            await repo.start(owner, conversation.id, "变更内容", first_id)
        second = await repo.start(owner, conversation.id, "第二问", uuid4())
        assert all(
            "第一问" not in x.content and "未完成回答" not in x.content for x in second.messages
        )
        await repo.append(second.lease, "完整回答")
        await repo.finish(second.lease, "completed")
        third = await repo.start(owner, conversation.id, "第三问", uuid4())
        assert [(m.role, m.content) for m in third.messages][-3:] == [
            ("user", "第二问"),
            ("assistant", "完整回答"),
            ("user", "第三问"),
        ]
        # A crashed producer is recoverable; its durable partial answer stays incomplete.
        async with sessions.begin() as session:
            await session.execute(
                update(ConversationModel)
                .where(ConversationModel.id == conversation.id)
                .values(active_expires_at=NOW - timedelta(seconds=1))
            )
        detail = await repo.get(owner, conversation.id)
        assert detail.messages[-1].status == "incomplete"
        with pytest.raises(ConversationError, match="conversation_lease_lost"):
            await repo.append(third.lease, "过期任务")
        extra = await repo.create(owner, "另一会话")
        page = await repo.list(owner, limit=1)
        assert len(page.items) == 1 and page.next_cursor
        rest = await repo.list(owner, limit=1, cursor=page.next_cursor)
        assert {page.items[0].id, rest.items[0].id} == {conversation.id, extra.id}
        with pytest.raises(ConversationError, match="invalid_history_cursor"):
            await repo.list(other, cursor=page.next_cursor)
        with pytest.raises(ConversationError, match="resource_unavailable"):
            await repo.get(owner, uuid4())
        races = await asyncio.gather(
            repo.start(owner, extra.id, "竞争一", uuid4()),
            repo.start(owner, extra.id, "竞争二", uuid4()),
            return_exceptions=True,
        )
        assert (
            sum(isinstance(r, ConversationError) and r.code == "conversation_busy" for r in races)
            == 1
        )
        winner = next(r for r in races if not isinstance(r, Exception))
        long_answer = "中文" * 20000  # > MySQL TEXT byte limit; must round-trip MEDIUMTEXT.
        await repo.append(winner.lease, long_answer)
        await repo.finish(winner.lease, "completed")
        assert (await repo.get(owner, extra.id)).messages[-1].content == long_answer
        again = await repo.start(owner, extra.id, "继续", uuid4())
        assert any(m.content == long_answer for m in again.messages)
        await repo.finish(again.lease, "incomplete")
        # Cursor pages cover durable ordered history without loading an unbounded conversation.
        async with sessions.begin() as session:
            for index in range(202):
                session.add(
                    ConversationMessageModel(
                        id=new_uuid7(),
                        tenant_id=owner.context.tenant_id,
                        creator_user_id=owner.principal.user_id,
                        conversation_id=conversation.id,
                        request_id=uuid4(),
                        sequence=index + 6,
                        role="user",
                        content=str(index),
                        status="completed",
                        created_at=NOW,
                        updated_at=NOW,
                    )
                )
        latest = await repo.get(owner, conversation.id)
        assert len(latest.messages) == 200 and latest.has_more and latest.next_message_cursor
        older = await repo.get(owner, conversation.id, message_cursor=latest.next_message_cursor)
        assert len(older.messages) == 8 and not older.has_more
        assert {m.id for m in latest.messages}.isdisjoint(m.id for m in older.messages)
        reviews = ContractRunRepository(sessions, now=NOW)
        run = await reviews.create(owner, "history.pdf", "审查", date(2026, 9, 21), "history-key")
        assert (await reviews.list_owned(owner)).items[0].id == run.id
        assert (await reviews.list_owned(other)).items == ()
        assert (await reviews.list_owned(foreign)).items == ()
        await reviews.create(owner, "second.pdf", "审查", date(2026, 9, 21), "history-key2")
        first_page = await reviews.list_owned(owner, limit=1)
        second_page = await reviews.list_owned(owner, limit=1, cursor=first_page.next_cursor)
        assert first_page.items[0].id != second_page.items[0].id
        async with sessions.begin() as session:
            await session.execute(
                update(AuthSessionModel)
                .where(AuthSessionModel.id == owner.principal.session_id)
                .values(revoked_at=NOW)
            )
        with pytest.raises(ConversationError, match="resource_unavailable"):
            await repo.list(owner)
    finally:
        await engine.dispose()


def test_conversation_history_isolation_replay_and_interruption(mysql_url: URL):
    command.upgrade(_config(mysql_url), "head")
    asyncio.run(exercise(mysql_url))
