"""HTTP契约使用合成资料；真实服务验证另行执行，不调用付费模型。"""

import asyncio
import contextlib
import threading
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from legal_query.config import QueryError
from legal_query.http_runtime import Runtime, WorkGate, evidence_bundle
from legal_query.http_service import create_app

KEY = "synthetic-local-rag-key-12345678901234567890"
AUTH = {"Authorization": "Bearer " + KEY}
DOC, CHUNK = "d" * 64, "c" * 64


def row():
    return {
        "chunk_id": CHUNK,
        "document_id": DOC,
        "document_name": "合成测试条例",
        "article_no": "第一条",
        "text": "第一条 合成正文。",
        "chunk_type": "provision",
        "parent_chunk_id": None,
        "model": "qwen3.7-text-embedding",
        "rank": 1,
        "fusion_score": 0.03,
    }


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Mock()
        self.runtime.key = KEY
        self.runtime.run.return_value = {"status": "ok", "evidence": [], "warnings": []}
        self.client = TestClient(create_app(self.runtime))

    def tearDown(self):
        self.client.close()

    def test_health_public_but_search_requires_key_before_body_validation(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        for headers in ({}, {"Authorization": "Bearer wrong"}):
            response = self.client.post("/api/v1/rag/search", content="invalid", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertIn("request_id", response.json())
        self.runtime.run.assert_not_called()

    def test_search_contract_and_whitelist(self):
        response = self.client.post(
            "/api/v1/rag/search",
            json={"query": "测试问题", "mode": "bm25", "top_k": 5},
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["request_id"], response.headers["x-request-id"])
        method, body = self.runtime.run.call_args.args
        self.assertEqual(method, "search")
        self.assertEqual(body.query, "测试问题")
        self.assertEqual(body.mode, "bm25")
        self.assertEqual(
            self.client.post(
                "/api/v1/rag/search", json={"query": "测试", "collection": "private"}, headers=AUTH
            ).status_code,
            422,
        )

    def test_validation_never_echoes_input_or_credentials(self):
        for body in (
            {"query": "synthetic-secret", "top_k": 99999},
            {"query": " "},
            {"query": "ok", "top_k": True},
        ):
            response = self.client.post("/api/v1/rag/search", json=body, headers=AUTH)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("synthetic-secret", response.text)
            self.assertNotIn(KEY, response.text)

    def test_read_requires_document_and_exactly_one_selector(self):
        for body in (
            {"chunk_id": CHUNK},
            {"document_id": DOC},
            {"document_id": DOC, "chunk_id": CHUNK, "article_no": "第一条"},
        ):
            self.assertEqual(
                self.client.post("/api/v1/rag/read", json=body, headers=AUTH).status_code, 422
            )
        self.assertEqual(
            self.client.post(
                "/api/v1/rag/read", json={"document_id": DOC, "article_no": "第一条"}, headers=AUTH
            ).status_code,
            200,
        )

    def test_errors_are_stable_and_redacted(self):
        for error, status in (
            (QueryError("embedding_access_denied"), 502),
            (RuntimeError("synthetic-secret"), 503),
            (QueryError("service_busy"), 429),
        ):
            self.runtime.run.side_effect = error
            response = self.client.post("/api/v1/rag/search", json={"query": "测试"}, headers=AUTH)
            self.assertEqual(response.status_code, status)
            self.assertNotIn("synthetic-secret", response.text)

    def test_malformed_dependency_result_is_redacted(self):
        self.runtime.run.return_value = {
            "status": "ok",
            "evidence": [{"text": "synthetic-secret"}],
            "warnings": [],
        }
        response = self.client.post("/api/v1/rag/search", json={"query": "测试"}, headers=AUTH)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("synthetic-secret", response.text)

    def test_ready_requires_loaded_collection_and_always_closes(self):
        client = Mock()
        runtime = Runtime(KEY, Mock())
        with (
            patch("legal_query.http_runtime.MilvusClient", return_value=client),
            patch("legal_query.http_runtime.preflight", return_value={"load": "NotLoad"}),
        ):
            with self.assertRaises(QueryError):
                runtime.run("ready")
        client.close.assert_called_once()

    def test_large_body_and_rate_limit(self):
        response = self.client.post("/api/v1/rag/search", content=b"x" * 65537, headers=AUTH)
        self.assertEqual(response.status_code, 413)
        for _ in range(30):
            self.assertEqual(self.client.get("/readyz", headers=AUTH).status_code, 200)
        self.assertEqual(self.client.get("/readyz", headers=AUTH).status_code, 429)

    def test_streamed_body_limit(self):
        def chunks():
            yield b"a" * 40000
            yield b"b" * 40000

        self.assertEqual(
            self.client.post("/api/v1/rag/search", content=chunks(), headers=AUTH).status_code, 413
        )
        self.runtime.run.assert_not_called()

    def test_evidence_identity_and_budget_do_not_claim_completeness(self):
        full = evidence_bundle([row()], "r1", 100)
        hit = full["evidence"][0]
        self.assertEqual(hit["chunk_id"], CHUNK)
        self.assertEqual(hit["text"], row()["text"])
        self.assertIsNone(hit["is_complete"])
        self.assertNotEqual(
            hit["evidence_id"], evidence_bundle([row()], "r2", 100)["evidence"][0]["evidence_id"]
        )
        small = evidence_bundle([row()], "r1", 4)
        self.assertEqual(len(small["evidence"][0]["text"]), 4)
        self.assertFalse(small["evidence"][0]["is_complete"])
        self.assertTrue(small["truncated"])

    def test_cancellation_does_not_release_running_work(self):
        async def scenario():
            gate = WorkGate(1)
            started, release = threading.Event(), threading.Event()

            def work():
                started.set()
                release.wait(5)

            task = asyncio.create_task(asyncio.to_thread(gate.run, work))
            await asyncio.to_thread(started.wait, 2)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            try:
                with self.assertRaisesRegex(QueryError, "service_busy"):
                    gate.run(lambda: None)
            finally:
                release.set()

        asyncio.run(scenario())
