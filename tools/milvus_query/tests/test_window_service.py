import asyncio
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from legal_query.config import QueryError
from legal_query.window_corpus import COLLECTION, VERSION, digest, load, save, sha
from legal_query.window_service import (
    DocumentStore,
    ReadRequest,
    Runtime,
    create_app,
    excerpt_ranges,
)

KEY = "k" * 40
AUTH = {"Authorization": "Bearer " + KEY}


@pytest.fixture
def corpus(tmp_path):
    rows, completions, metadata = [], {}, {}
    config = {"version": "synthetic-v2", "script_sha256": "b" * 64}
    save(tmp_path / "config.json", config)
    for i in range(2):
        sid, did = str(i) * 24, str(i) * 64
        folder = tmp_path / "documents" / sid
        folder.mkdir(parents=True)
        text = "合成法规正文" * 4000
        source = {
            "id": sid,
            "source_sha256": str(i + 2) * 64,
            "source_relative_path": f"法律/合成{i}.txt",
        }
        meta = {
            "source": source,
            "document_id": did,
            "document_name": f"合成{i}",
            "fulltext_sha256": digest(text),
        }
        save(folder / "document.json", meta)
        (folder / "fulltext.txt").write_text(text, encoding="utf-8")
        chunk = {"chunk_id": str(i + 6) * 64, "document_id": did,
                 "start_char": 10000, "end_char": 10100, "text": text[10000:10100]}
        (folder / "chunks.jsonl").write_text(json.dumps(chunk) + "\n", encoding="utf-8")
        data = {
            "status": "completed",
            "document_id": did,
            "output_hashes": {
                n: sha(folder / n) for n in ("document.json", "fulltext.txt", "chunks.jsonl")
            },
        }
        save(
            folder / "completed.json",
            {
                "data": data,
                "data_sha256": digest(data),
                "identity": digest([config, source]),
            },
        )
        completions[sid] = sha(folder / "completed.json")
        rows.append(
            {
                "id": sid,
                "directory": str(folder),
                "title": meta["document_name"],
                "source_sha256": source["source_sha256"],
                "source_relative_path": source["source_relative_path"],
                "chunks": 1,
                "old_quality_flags": ["images_not_ocr"],
            }
        )
        metadata[did] = {
            "source_id": sid,
            **{k: meta[k] for k in ("document_id", "document_name", "fulltext_sha256")},
            "source_sha256": source["source_sha256"],
        }
    report = tmp_path / "report.json"
    save(
        report,
        {
            "status": "passed",
            "errors": [],
            "verified_documents": 2,
            "expected_documents": 2,
            "rows": rows,
            "chunks": 2,
        },
    )
    release = sha(report)
    save(
        tmp_path / "source-artifacts.json",
        {"release_sha256": release, "completions": completions},
    )
    save(
        tmp_path / "ready.json",
        {
            "status": "ready",
            "collection": COLLECTION,
            "release_sha256": release,
            "source_report": str(report),
            "identity": {
                "source_snapshot_sha256": sha(tmp_path / "source-artifacts.json"),
                "release_sha256": release,
            },
        },
    )
    for row in metadata.values():
        row["release_sha256"] = release
    return tmp_path, metadata


def test_pages_rebuild_and_cursor_bound_to_document(corpus):
    root, metadata = corpus
    store = DocumentStore(root, KEY)
    first = store.read(metadata["0" * 64], None)
    second = store.read(metadata["0" * 64], first["next_cursor"])
    assert first["text"] + second["text"] == "合成法规正文" * 4000
    assert second["complete"] and second["next_cursor"] is None
    assert len(first["next_cursor"]) <= 160
    assert first["validity"] == "unknown" and "images_not_ocr" in first["quality_flags"]
    with pytest.raises(QueryError):
        store.read(metadata["1" * 64], first["next_cursor"])


def test_excerpts_only_return_hit_context_with_source_offsets(corpus):
    root, metadata = corpus
    store = DocumentStore(root, KEY)
    result = store.excerpts(metadata["0" * 64], ["6" * 64])
    assert result["content_scope"] == "retrieved_excerpts"
    assert result["complete"] and result["next_cursor"] is None
    assert len(result["text"]) < 3100
    assert result["context_hash"] == digest(result["text"])
    span = result["source_ranges"][0]
    assert span["start_char"] <= 10000 and 10100 <= span["end_char"]
    assert span["chunk_ids"] == ["6" * 64]
    original = (root / "documents" / ("0" * 24) / "fulltext.txt").read_text(encoding="utf-8")
    assert result["text"] == original[span["start_char"]:span["end_char"]]
    assert "context_boundary_incomplete" in result["quality_flags"]
    with pytest.raises(QueryError):
        store.excerpts(metadata["0" * 64], ["7" * 64])
    (root / "documents" / ("0" * 24) / "chunks.jsonl").write_text("{}", encoding="utf-8")
    with pytest.raises(QueryError):
        store.excerpts(metadata["0" * 64], ["6" * 64])


def test_cut_articles_completed_and_overlapping_windows_merged():
    text = "第一条　不相关。\n第二条　应履行维修义务。\n第三条　保证金约定。\n第四条　其他。\n"
    start = text.index("应履行")
    end = text.index("保证金") + 2
    chunks = [{"chunk_id": "a", "start_char": start, "end_char": end, "text": text[start:end]},
              {"chunk_id": "b", "start_char": start + 1, "end_char": end, "text": text[start+1:end]}]
    spans, incomplete = excerpt_ranges(text, chunks)
    assert not incomplete and len(spans) == 1
    assert text[spans[0]["start_char"]:spans[0]["end_char"]] == (
        "第二条　应履行维修义务。\n第三条　保证金约定。\n"
    )
    assert spans[0]["chunk_ids"] == ["a", "b"]


def test_second_version_quality_warnings_are_not_lost(corpus):
    root, metadata = corpus
    store = DocumentStore(root, KEY)
    entry = store.entries["0" * 24]
    entry.update(
        old_quality_flags=[],
        needs_review=True,
        quality={"source_xml_paragraphs_not_verbatim_contained": 2, "pictures": 1},
    )
    page = store.read(metadata["0" * 64], None)
    assert {
        "source_text_coverage_warning",
        "source_images_not_verified",
        "parser_review_required",
    } <= set(page["quality_flags"])
    assert page["validity"] == "unknown"


@pytest.mark.parametrize("name", ["fulltext.txt", "document.json", "completed.json"])
def test_artifact_tampering_rejected(corpus, name):
    root, metadata = corpus
    store = DocumentStore(root, KEY)
    (root / "documents" / ("0" * 24) / name).write_text("tampered", encoding="utf-8")
    with pytest.raises(QueryError):
        store.read(metadata["0" * 64], None)


class FakeRuntime:
    key = KEY

    def run(self, operation, payload=None, **kwargs):
        if operation == "search":
            return {
                "mode": "rrf_then_model_rerank",
                "rerank": {"usage": {"total_tokens": 5}},
                "hits": [],
            }
        return {"status": "ready"}

    async def close(self):
        return True


def test_http_auth_limits_and_usage():
    with TestClient(create_app(FakeRuntime())) as client:
        assert client.get("/readyz").status_code == 401
        assert client.post("/search", json={"query": "x"}).status_code == 401
        assert client.post("/documents/excerpts", json={}).status_code == 401
        response = client.post("/search", headers=AUTH, json={"query": "合同"})
        assert response.json()["rerank"]["usage"]["total_tokens"] == 5
        assert response.headers["x-request-id"] == response.json()["request_id"]
        for body in (
            {"query": "x", "top_k": 6},
            {"query": "x" * 2001},
            {"query": " "},
            {"query": "x", "region": "invalid"},
        ):
            assert client.post("/search", headers=AUTH, json=body).status_code == 422
        for body in (
            {"document_id": "0" * 64, "chunk_ids": []},
            {"document_id": "0" * 64, "chunk_ids": ["6" * 64] * 2},
            {"document_id": "0" * 64, "chunk_ids": ["6" * 64], "cursor": None},
            {"document_id": "0" * 64, "chunk_ids": ["../source"]},
        ):
            assert client.post("/documents/excerpts", headers=AUTH, json=body).status_code == 422
        assert client.post("/documents/excerpts", headers=AUTH,
                           json={"document_id": "0" * 64, "chunk_ids": ["6" * 64]}).status_code == 200
        assert (
            client.post(
                "/documents/read",
                headers=AUTH,
                json={"document_id": "0" * 64, "path": "secret"},
            ).status_code
            == 422
        )
        assert (
            client.post("/search", headers=AUTH, content=b"x" * 65537).status_code
            == 413
        )


def test_errors_do_not_expose_dependency_messages():
    runtime = FakeRuntime()

    def broken(*args, **kwargs):
        raise QueryError("private body and key")

    runtime.run = broken
    with TestClient(create_app(runtime)) as client:
        result = client.post("/search", headers=AUTH, json={"query": "private query"})
        assert result.status_code == 503
        assert "private" not in result.text


def test_rate_limit():
    with TestClient(create_app(FakeRuntime())) as client:
        for _ in range(30):
            assert client.get("/readyz", headers=AUTH).status_code == 200
        assert client.get("/readyz", headers=AUTH).status_code == 429


def test_metadata_cannot_redirect_filesystem_or_mismatch_identity(corpus):
    root, metadata = corpus
    store = DocumentStore(root, KEY)
    row = {
        **metadata["0" * 64],
        "directory": "C:/secret",
        "source_relative_path": "C:/secret",
    }
    assert store.read(row, None)["title"] == "合成0"
    for key, value in (
        ("document_id", "1" * 64),
        ("document_name", "different"),
        ("source_id", "x" * 24),
    ):
        with pytest.raises(QueryError):
            store.read({**row, key: value}, None)


def test_cancelled_wait_keeps_capacity():
    async def scenario():
        started, finish = threading.Event(), threading.Event()
        runtime = Runtime(KEY, None, Path("unused"), client_factory=lambda: None)

        def blocked(*args):
            started.set()
            finish.wait(5)
            return {}

        runtime._execute = blocked
        one = asyncio.create_task(asyncio.to_thread(runtime.run, "ready"))
        await asyncio.to_thread(started.wait, 2)
        two = asyncio.create_task(asyncio.to_thread(runtime.run, "ready"))
        await asyncio.sleep(0.05)
        one.cancel()
        with pytest.raises(asyncio.CancelledError):
            await one
        with pytest.raises(QueryError, match="service_busy"):
            await asyncio.to_thread(runtime.run, "ready")
        assert not await runtime.close(timeout=0.01)
        finish.set()
        await two

    asyncio.run(scenario())


def test_runtime_reads_exact_document_and_closes_client(corpus):
    root, metadata = corpus
    receipt = load(root / "ready.json")
    receipt["identity"]["target"] = {
        "uri": "http://localhost:19530",
        "database": "blog",
    }
    receipt["collection_id"] = 123
    save(root / "ready.json", receipt)
    store = DocumentStore(root, KEY)

    class Client:
        closed = False

        def describe_collection(self, collection):
            assert collection == COLLECTION
            return {
                "description": VERSION + ":" + store.release_hash,
                "collection_id": 123,
            }

        def query(self, collection, **kwargs):
            assert collection == COLLECTION
            assert 'document_id == "' + "0" * 64 + '"' in kwargs["filter"]
            assert store.release_hash in kwargs["filter"]
            assert kwargs["limit"] == 1
            return [metadata["0" * 64]]

        def close(self):
            self.closed = True

    client = Client()
    runtime = Runtime(KEY, store, Path("unused"), client_factory=lambda: client)
    page = runtime.run("read", ReadRequest(document_id="0" * 64))
    assert page["title"] == "合成0" and client.closed
    assert asyncio.run(runtime.close())
