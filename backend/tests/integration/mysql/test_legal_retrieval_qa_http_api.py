from __future__ import annotations

import asyncio
import os
import re
from base64 import b64encode
from collections.abc import Iterator
from datetime import date
from uuid import uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from lawyer_agent.application.evidence import EvidenceItem
from lawyer_agent.application.legal_claim_gate import LegalClaim
from lawyer_agent.application.legal_dataset_search import LegalDatasetNotPublished
from lawyer_agent.application.legal_retrieval_qa import LegalRetrievalAnswer
from lawyer_agent.application.model_gateway import (
    ModelProviderTimeout,
    ModelProviderUnavailable,
)
from lawyer_agent.config import Settings
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import LegalVersionStatus
from lawyer_agent.domain.model_gateway import TokenUsage
from lawyer_agent.infrastructure.persistence.seed_authz import seed_authorization_catalog
from lawyer_agent.main import create_app

pytestmark = [pytest.mark.integration, pytest.mark.mysql]

_ORIGIN = "https://app.test"
_PASSWORD = "correct horse battery staple"  # noqa: S105
_TEST_DATABASE_PATTERN = re.compile(r"^lawyer_test_[a-f0-9]{32}$")
_CIPHER_KEY = bytes([71]) * 32
_BLIND_KEY = bytes([73]) * 32


def _encoded(value: bytes) -> str:
    return b64encode(value).decode("ascii")


def _alembic_config(mysql_url: URL) -> Config:
    backend_dir = __import__("pathlib").Path(__file__).parents[3]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option(
        "sqlalchemy.url",
        mysql_url.render_as_string(hide_password=False).replace("%", "%%"),
    )
    return config


@pytest.fixture(scope="module")
def migrated_mysql_url(mysql_url: URL) -> Iterator[URL]:
    database_name = f"lawyer_test_{uuid4().hex}"
    if not _TEST_DATABASE_PATTERN.fullmatch(database_name):
        raise RuntimeError("refusing to manage an unexpected database name")
    isolated_url = mysql_url.set(database=database_name)
    asyncio.run(_execute_admin(mysql_url, f"CREATE DATABASE `{database_name}`"))
    try:
        command.upgrade(_alembic_config(isolated_url), "head")
        asyncio.run(_seed_catalog(isolated_url))
        yield isolated_url
    finally:
        asyncio.run(_execute_admin(mysql_url, f"DROP DATABASE `{database_name}`"))


async def _execute_admin(mysql_url: URL, statement: str) -> None:
    engine = create_async_engine(mysql_url.set(database="mysql"))
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


async def _seed_catalog(mysql_url: URL) -> None:
    engine = create_async_engine(mysql_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session, session.begin():
            await seed_authorization_catalog(session)
    finally:
        await engine.dispose()


async def _redis_available(url: str) -> bool:
    client = Redis.from_url(
        url, socket_connect_timeout=1.0, socket_timeout=1.0, retry_on_timeout=False
    )
    try:
        return bool(await client.ping())
    finally:
        await client.aclose()


def _item() -> EvidenceItem:
    return EvidenceItem(
        evidence_id=new_uuid7(),
        instrument_title="《中华人民共和国民法典》",
        version_id=new_uuid7(),
        version_label="2020",
        status=LegalVersionStatus.CURRENT,
        published_on=date(2020, 5, 28),
        effective_on=date(2021, 1, 1),
        repealed_on=None,
        provision_no="第五百七十七条",
        provision_text="当事人一方不履行合同义务或者履行合同义务不符合约定的，应当承担继续履行、采取补救措施或者赔偿损失等违约责任。",
        source_ref="corpus://civil-code-2020.docx",
        dataset_version="dataset_v1",
        authorized=True,
    )


class FakeRetrievalQa:
    """Structural stand-in for the retrieval QA service."""

    def __init__(self, outcome: object) -> None:
        self._outcome = outcome
        self.calls: list[dict[str, object]] = []

    async def answer(self, **kwargs: object) -> LegalRetrievalAnswer:
        self.calls.append(kwargs)
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _allowed_answer() -> LegalRetrievalAnswer:
    item = _item()
    claim = LegalClaim(
        text="当事人一方不履行合同义务的，应当承担违约责任。",
        evidence_ids=(item.evidence_id,),
    )
    return LegalRetrievalAnswer(
        text="1. 当事人一方不履行合同义务的，应当承担违约责任。",
        refused=False,
        reason="allowed",
        claims=(claim,),
        citations=(item,),
        usage=TokenUsage(prompt_tokens=20, completion_tokens=8, total_tokens=28),
    )


def _refused_answer(reason: str) -> LegalRetrievalAnswer:
    return LegalRetrievalAnswer(
        text="未能在当前法规数据集中找到与问题相关的有效依据，暂不作答。",
        refused=True,
        reason=reason,
    )


def _client(migrated_mysql_url: URL, *, prefix_label: str) -> TestClient:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    prefix = f"{prefix_label}:{uuid4().hex}:"
    settings = Settings(
        environment="test",
        secret_key="h" * 32,
        database_url=migrated_mysql_url.render_as_string(hide_password=False),
        redis_url=redis_url,
        redis_key_prefix=prefix,
        trusted_origins=(_ORIGIN,),
        cookie_secure=True,
        data_encryption_key_ring={7: _encoded(_CIPHER_KEY)},
        data_encryption_active_key_version=7,
        blind_index_key_ring={7: _encoded(_BLIND_KEY)},
        blind_index_active_key_version=7,
        blind_index_rollout_phase="legacy-compatible",
        blind_index_legacy_key_version=7,
        blind_index_legacy_writers_drained=False,
    )
    return TestClient(create_app(settings), base_url="https://testserver")


def _register(client: TestClient, username: str) -> str:
    response = client.post(
        "/api/v1/auth/register",
        headers={"Origin": _ORIGIN},
        json={
            "username": username,
            "password": _PASSWORD,
            "display_name": "问答用户",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


def _question() -> dict[str, object]:
    return {"question": "不履行合同义务应承担什么责任？"}


def test_retrieval_qa_requires_authentication(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-unauth")
    with client:
        reply = client.post("/api/v1/legal/questions", json=_question())
        assert reply.status_code == 401


def test_retrieval_qa_unavailable_without_service(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-none")
    with client:
        token = _register(client, "rqa-none-owner")
        headers = {"Authorization": f"Bearer {token}"}
        # Default composition: prerequisites not configured -> stable 503.
        reply = client.post("/api/v1/legal/questions", headers=headers, json=_question())
        assert reply.status_code == 503
        assert reply.json()["code"] == "retrieval_qa_unavailable"


def test_retrieval_qa_allowed_answer_over_real_mysql(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-ok")
    with client:
        client.app.state.services.legal_retrieval_qa_http = FakeRetrievalQa(
            _allowed_answer()
        )
        token = _register(client, "rqa-ok-owner")
        headers = {"Authorization": f"Bearer {token}"}
        reply = client.post("/api/v1/legal/questions", headers=headers, json=_question())
        assert reply.status_code == 200, reply.text
        body = reply.json()
        assert body["refused"] is False
        assert body["reason"] == "allowed"
        assert "违约责任" in body["text"]
        assert body["usage"]["total_tokens"] == 28
        assert len(body["citations"]) == 1
        citation = body["citations"][0]
        assert citation["instrument_title"] == "《中华人民共和国民法典》"
        assert citation["provision_no"] == "第五百七十七条"
        assert citation["evidence_id"]
        assert citation["source_ref"].startswith("corpus://")
        assert citation["dataset_version"] == "dataset_v1"
        # Whitelisted response: no unknown keys leak through.
        assert set(citation) == {
            "evidence_id",
            "instrument_title",
            "version_label",
            "provision_no",
            "provision_text",
            "source_ref",
            "dataset_version",
        }


def test_retrieval_qa_refusal_reason_is_stable(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-refused")
    with client:
        client.app.state.services.legal_retrieval_qa_http = FakeRetrievalQa(
            _refused_answer("no_evidence")
        )
        token = _register(client, "rqa-refused-owner")
        headers = {"Authorization": f"Bearer {token}"}
        reply = client.post("/api/v1/legal/questions", headers=headers, json=_question())
        assert reply.status_code == 200
        body = reply.json()
        assert body["refused"] is True
        assert body["reason"] == "no_evidence"
        assert body["citations"] == []
        assert body["usage"] is None


def test_retrieval_qa_maps_provider_failures(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")

    scenarios = [
        (ModelProviderTimeout("slow"), 504, "model_provider_timeout"),
        (ModelProviderUnavailable("down"), 503, "model_provider_unavailable"),
        (
            LegalDatasetNotPublished("dataset is not published"),
            503,
            "legal_dataset_not_published",
        ),
    ]
    for index, (failure, expected_status, expected_code) in enumerate(scenarios):
        client = _client(
            migrated_mysql_url,
            prefix_label=f"lawyer-test-rqa-fail-{index}",
        )
        with client:
            client.app.state.services.legal_retrieval_qa_http = FakeRetrievalQa(
                failure
            )
            token = _register(client, f"rqa-fail-{index}-owner")
            headers = {"Authorization": f"Bearer {token}"}
            reply = client.post(
                "/api/v1/legal/questions", headers=headers, json=_question()
            )
            assert reply.status_code == expected_status, reply.text
            assert reply.json()["code"] == expected_code


def test_retrieval_qa_rejects_invalid_question_bodies(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-invalid")
    with client:
        client.app.state.services.legal_retrieval_qa_http = FakeRetrievalQa(
            _allowed_answer()
        )
        token = _register(client, "rqa-invalid-owner")
        headers = {"Authorization": f"Bearer {token}"}
        blank = client.post(
            "/api/v1/legal/questions",
            headers=headers,
            json={"question": "   "},
        )
        assert blank.status_code == 422
        bad_alias = client.post(
            "/api/v1/legal/questions",
            headers=headers,
            json={"question": "q", "alias": "bad alias!"},
        )
        assert bad_alias.status_code == 422
        unknown = client.post(
            "/api/v1/legal/questions",
            headers=headers,
            json={"question": "q", "surprise": 1},
        )
        assert unknown.status_code == 422


def _sse_events(text: str) -> list[tuple[str, dict[str, object]]]:
    events: list[tuple[str, dict[str, object]]] = []
    for block in text.split("\n\n"):
        lines = block.splitlines()
        if not lines:
            continue
        name = ""
        data_lines: list[str] = []
        for line in lines:
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        payload: dict[str, object] = {}
        if data_lines:
            payload = __import__("json").loads("\n".join(data_lines))
        events.append((name, payload))
    return events


def test_retrieval_qa_stream_requires_authentication(migrated_mysql_url: URL) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-stream-unauth")
    with client:
        reply = client.post("/api/v1/legal/questions/stream", json=_question())
        assert reply.status_code == 401


def test_retrieval_qa_stream_delivers_events_over_real_mysql(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-stream-ok")
    with client:
        client.app.state.services.legal_retrieval_qa_http = FakeRetrievalQa(
            _allowed_answer()
        )
        token = _register(client, "rqa-stream-ok-owner")
        headers = {"Authorization": f"Bearer {token}"}
        reply = client.post(
            "/api/v1/legal/questions/stream", headers=headers, json=_question()
        )
        assert reply.status_code == 200
        assert reply.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(reply.text)
        assert [name for name, _ in events] == ["started", "answer", "done"]
        assert events[0][1]["question"].startswith("不履行合同")
        answer = events[1][1]
        assert answer["refused"] is False
        assert answer["reason"] == "allowed"
        assert "违约责任" in str(answer["text"])
        assert answer["usage"]["total_tokens"] == 28
        assert len(answer["citations"]) == 1
        assert answer["citations"][0]["provision_no"] == "第五百七十七条"
        assert events[2][1] == {}


def test_retrieval_qa_stream_carries_stable_refusal(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-stream-refused")
    with client:
        client.app.state.services.legal_retrieval_qa_http = FakeRetrievalQa(
            _refused_answer("no_evidence")
        )
        token = _register(client, "rqa-stream-refused-owner")
        headers = {"Authorization": f"Bearer {token}"}
        reply = client.post(
            "/api/v1/legal/questions/stream", headers=headers, json=_question()
        )
        assert reply.status_code == 200
        events = _sse_events(reply.text)
        assert [name for name, _ in events] == ["started", "answer", "done"]
        answer = events[1][1]
        assert answer["refused"] is True
        assert answer["reason"] == "no_evidence"
        assert answer["citations"] == []


def test_retrieval_qa_stream_delivers_error_event(
    migrated_mysql_url: URL,
) -> None:
    redis_url = os.getenv("LAWYER_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")
    if not asyncio.run(_redis_available(redis_url)):
        pytest.skip("test Redis unavailable")
    client = _client(migrated_mysql_url, prefix_label="lawyer-test-rqa-stream-fail")
    with client:
        client.app.state.services.legal_retrieval_qa_http = FakeRetrievalQa(
            ModelProviderTimeout("slow")
        )
        token = _register(client, "rqa-stream-fail-owner")
        headers = {"Authorization": f"Bearer {token}"}
        reply = client.post(
            "/api/v1/legal/questions/stream", headers=headers, json=_question()
        )
        assert reply.status_code == 200
        events = _sse_events(reply.text)
        assert [name for name, _ in events] == ["started", "error", "done"]
        failure = events[1][1]
        assert failure["status"] == 504
        assert failure["code"] == "model_provider_timeout"
        assert events[2][1] == {}
