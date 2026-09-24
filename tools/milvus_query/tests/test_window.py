"""第二版窗口导入与LlamaIndex检索的合成边界测试。"""

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from legal_query import window_corpus as corpus
from legal_query import window_retrieval as retrieval
from legal_query.config import QueryError


def fixture():
    text = "第七百零三条 租赁合同。"
    chunk = {
        "chunk_id": "c" * 64,
        "document_id": "d" * 64,
        "document_name": "合成法",
        "chunk_index": 0,
        "start_char": 0,
        "end_char": len(text),
        "text": text,
        "embedding_text": "法律名称：合成法\n正文：" + text,
        "fulltext_sha256": corpus.digest(text),
    }
    chunk["embedding_text_sha256"] = corpus.digest(chunk["embedding_text"])
    vector = {
        "chunk_id": chunk["chunk_id"],
        "embedding_text_sha256": chunk["embedding_text_sha256"],
        "vector": [0.01] * 1024,
    }
    entry = {
        "id": "e" * 24,
        "title": "合成法",
        "source_relative_path": "地方法规/湖北/合成法.docx",
        "source_sha256": "a" * 64,
        "needs_review": True,
        "old_quality_flags": ["images_or_textboxes_not_ocr"],
        "quality": {"pictures": 1, "source_xml_paragraphs_not_verbatim_contained": 0},
    }
    return chunk, vector, entry


class WindowTests(unittest.TestCase):
    def test_ready_rejects_wrong_target_and_recreated_collection(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ready.json"
            receipt = {
                "status": "ready",
                "collection": corpus.COLLECTION,
                "release_sha256": "f" * 64,
                "collection_id": 123,
                "identity": {
                    "target": {"uri": "http://localhost:19530", "database": "blog"}
                },
            }
            corpus.save(path, receipt)
            client = Mock()
            client.describe_collection.return_value = {
                "collection_id": 123,
                "description": corpus.VERSION + ":" + "f" * 64,
            }
            self.assertEqual(corpus.validate_ready(client, path), receipt)
            with self.assertRaisesRegex(QueryError, "window_target_changed"):
                corpus.validate_ready(
                    client,
                    path,
                    target={"uri": "http://other:19530", "database": "blog"},
                )
            client.describe_collection.return_value["collection_id"] = 456
            with self.assertRaisesRegex(QueryError, "window_collection_changed"):
                corpus.validate_ready(client, path)

    def test_refuses_existing_unrelated_collection(self):
        client = Mock()
        client.has_collection.return_value = True
        client.describe_collection.return_value = {"description": "another corpus"}
        with self.assertRaises(QueryError):
            corpus.ensure_collection(client, SimpleNamespace(sha256="f" * 64))
        client.upsert.assert_not_called()
        client.create_collection.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "本地Windows导入器使用msvcrt文件锁")
    def test_partial_import_resumes_only_unconfirmed_batch_and_verifies_readback(self):
        c, v, e = fixture()
        a = corpus.build_row(c, v, e, "f" * 64)
        b = deepcopy(a)
        b["chunk_id"] = "b" * 64
        b["row_sha256"] = corpus.row_digest(b)
        release = SimpleNamespace(
            sha256="f" * 64,
            path=Path("synthetic-report.json"),
            report={"chunks": 2, "rows": [{}]},
            batches=lambda: iter([[a], [b]]),
            freeze=lambda root: "s" * 64,
        )
        client = Mock()
        client.describe_collection.return_value = {"collection_id": 123}
        written = {}
        calls = []

        def upsert(name, data, timeout):
            calls.append(data[0]["chunk_id"])
            if len(calls) == 2:
                raise ConnectionError("synthetic network interruption")
            for row in data:
                written[row["chunk_id"]] = deepcopy(row)
            return {"upsert_count": len(data)}

        def query(name, **kwargs):
            if kwargs["output_fields"] == ["count(*)"]:
                return [{"count(*)": len(written)}]
            import json

            ids = json.loads(kwargs["filter"].split(" in ", 1)[1])
            return [written[x] for x in ids if x in written]

        client.upsert.side_effect = upsert
        client.query.side_effect = query
        with (
            TemporaryDirectory() as tmp,
            patch.object(corpus, "ensure_collection"),
            patch.object(corpus, "indexes"),
        ):
            root = Path(tmp)
            with self.assertRaises(ConnectionError):
                corpus.import_release(client, release, root)
            self.assertFalse((root / "ready.json").exists())
            corpus.import_release(client, release, root)
            self.assertEqual(calls, [a["chunk_id"], b["chunk_id"], b["chunk_id"]])
            self.assertEqual(corpus.load(root / "ready.json")["rows"], 2)
            with self.assertRaisesRegex(QueryError, "import_checkpoint_mismatch"):
                corpus.import_release(
                    client,
                    release,
                    root,
                    target={"uri": "http://other:19530", "database": "blog"},
                )
            client.describe_collection.return_value = {"collection_id": 456}
            with self.assertRaisesRegex(QueryError, "import_collection_changed"):
                corpus.import_release(client, release, root)
            self.assertFalse((root / "ready.json").exists())
            release.sha256 = "changed"
            with self.assertRaises(QueryError):
                corpus.import_release(client, release, root)

    def test_reranker_uses_full_input_and_preserves_metadata_without_fallback(self):
        import json

        from llama_index.core.schema import NodeWithScore, TextNode

        nodes = [
            NodeWithScore(
                node=TextNode(
                    id_=str(i),
                    text=f"法律名称：合成法\n正文：内容{i}",
                    metadata={"start_char": i},
                ),
                score=0.1,
            )
            for i in range(2)
        ]

        def handle(request):
            data = json.loads(request.content)
            self.assertEqual(data["documents"], [n.node.text for n in nodes])
            self.assertEqual(data["top_n"], 1)
            return httpx.Response(
                200,
                json={
                    "results": [{"index": 1, "relevance_score": 0.8}],
                    "usage": {"total_tokens": 42},
                },
            )

        reranker = retrieval.AlibabaReranker(
            api_key="synthetic",
            endpoint="https://example.invalid/reranks",
            top_n=1,
            transport=httpx.MockTransport(handle),
        )
        result = reranker.postprocess_nodes(nodes, query_str="合成问题")
        self.assertEqual(result[0].node.metadata["start_char"], 1)
        self.assertEqual(reranker.call_info["usage"]["total_tokens"], 42)
        self.assertNotIn("synthetic", repr(reranker))
        failing = retrieval.AlibabaReranker(
            api_key="synthetic",
            endpoint="https://example.invalid/reranks",
            transport=httpx.MockTransport(lambda r: httpx.Response(403)),
        )
        with self.assertRaisesRegex(QueryError, "rerank_http_403"):
            failing.postprocess_nodes(nodes, query_str="问题")

    def test_row_preserves_exact_embedding_source_and_quality(self):
        c, v, e = fixture()
        row = corpus.build_row(c, v, e, "f" * 64)
        self.assertEqual(row["embedding_text"], c["embedding_text"])
        self.assertEqual(row["region"], "湖北")
        self.assertTrue(row["needs_review"])
        self.assertEqual(row["source_id"], e["id"])
        self.assertNotEqual(corpus.COLLECTION, "lawyer_db")
        self.assertEqual(row["quality"]["flags"], e["old_quality_flags"])

    def test_row_rejects_wrong_vector_and_silent_input_change(self):
        c, v, e = fixture()
        for kind in ("identity", "embedding", "dimension"):
            x, y = deepcopy(c), deepcopy(v)
            if kind == "identity":
                y["chunk_id"] = "b" * 64
            if kind == "embedding":
                x["embedding_text"] = x["text"]
            if kind == "dimension":
                y["vector"] = [1]
            with self.assertRaises(QueryError):
                corpus.build_row(x, y, e, "f" * 64)

    def test_both_retrievers_share_region_quality_and_release_scope(self):
        opts = retrieval.Options(region="湖北", clean_only=True)
        expr = opts.expression("f" * 64)
        self.assertIn('region == "湖北"', expr)
        self.assertIn('scope == "national"', expr)
        self.assertIn("needs_review == false", expr)
        self.assertIn("release_sha256", expr)
        with self.assertRaises(QueryError):
            retrieval.Options(region='湖北" or true').expression("f" * 64)

    def test_official_rrf_fuses_common_hits_and_retains_distinct_documents(self):
        from llama_index.core.schema import NodeWithScore, TextNode

        def node(ident, score):
            return NodeWithScore(
                node=TextNode(id_=ident, text="同样正文", metadata={"chunk_id": ident}),
                score=score,
            )

        dense = [node("a", 0.9), node("b", 0.8)]
        sparse = [node("b", 10), node("c", 9)]
        result = retrieval.fuse_nodes(dense, sparse, 3)
        self.assertEqual([n.node.node_id for n in result], ["b", "a", "c"])
        self.assertAlmostEqual(result[0].score, 1 / 61 + 1 / 60)
        self.assertEqual(dense[1].score, 0.8)

    def test_rrf_same_chunk_with_different_nested_json_key_order(self):
        c, v, e = fixture()
        row = corpus.build_row(c, v, e, "f" * 64)
        changed = deepcopy(row)
        changed["quality"] = dict(reversed(list(row["quality"].items())))
        client = Mock()
        client.search.side_effect = [
            [[{"chunk_id": row["chunk_id"], "distance": 0.9, "entity": row}]],
            [[{"chunk_id": row["chunk_id"], "distance": 10.0, "entity": changed}]],
        ]
        dense = retrieval.MilvusWindowRetriever(
            client, lambda q: v["vector"], "dense", retrieval.Options(), "f" * 64
        )
        sparse = retrieval.MilvusWindowRetriever(
            client, None, "bm25", retrieval.Options(), "f" * 64
        )
        result = retrieval.fusion([dense, sparse], 7).retrieve("合成问题")
        self.assertEqual(len(result), 1)
        self.assertAlmostEqual(result[0].score, 2 / 60)

    def test_artifact_snapshot_rejects_changed_completion_receipt(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "source"
            folder.mkdir()
            corpus.save(folder / "completed.json", {"synthetic": 1})
            release = object.__new__(corpus.Release)
            release.sha256 = "f" * 64
            release.report = {"rows": [{"id": "source", "directory": str(folder)}]}
            first = release.freeze(root / "state")
            self.assertEqual(first, release.freeze(root / "state"))
            corpus.save(folder / "completed.json", {"synthetic": 2})
            with self.assertRaisesRegex(QueryError, "release_snapshot_changed"):
                release.freeze(root / "state")

    def test_rerank_rejects_duplicate_out_of_range_or_nonfinite_scores(self):
        for results in (
            [{"index": 0, "relevance_score": 0.9}] * 2,
            [{"index": 9, "relevance_score": 0.9}],
            [{"index": 0, "relevance_score": float("nan")}],
            [],
        ):
            with self.assertRaises(QueryError):
                retrieval.validate_rerank(results, 2, 2)
        self.assertEqual(
            retrieval.validate_rerank(
                [
                    {"index": 1, "relevance_score": 0.9},
                    {"index": 0, "relevance_score": 0.1},
                ],
                2,
                2,
            ),
            [(1, 0.9), (0, 0.1)],
        )


if __name__ == "__main__":
    unittest.main()
