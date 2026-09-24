"""合成输入测试；不连接数据库，不调用付费模型。"""

import math
import unittest
from unittest.mock import Mock

from legal_query.config import QueryError, validate_vector
from legal_query.search import SearchOptions, build_requests, exact, format_hits, search
from pymilvus import RRFRanker

DOC = "d" * 64
CHUNK = "c" * 64
VECTOR = [0.01] * 1024


class QueryTests(unittest.TestCase):
    def test_invalid_vector(self):
        for value in ([0] * 1024, [1] * 3, [math.nan] * 1024, [True] * 1024):
            with self.subTest(value=value[:2]), self.assertRaises(QueryError):
                validate_vector(value)

    def test_valid_vector(self):
        self.assertEqual(validate_vector(VECTOR), VECTOR)

    def test_limits_and_scope_validation(self):
        for kwargs in (
            {"top_k": 51},
            {"candidate_k": 101},
            {"top_k": True},
            {"document_ids": ("bad",)},
            {"mode": "other"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(QueryError):
                SearchOptions(**kwargs).validate()

    def test_both_paths_have_same_scope_and_metrics(self):
        options = SearchOptions(document_ids=(DOC,))
        requests = build_requests("测试", VECTOR, options)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].expr, requests[1].expr)
        self.assertIn(DOC, requests[0].expr)
        self.assertEqual(requests[0].anns_field, "vector")
        self.assertEqual(requests[1].anns_field, "sparse_vector")
        self.assertEqual(requests[0].param["metric_type"], "COSINE")
        self.assertEqual(requests[1].param["metric_type"], "BM25")
        self.assertEqual(requests[1].data, ["测试"])

    def test_empty_question_never_calls_embedding(self):
        embed = Mock()
        with self.assertRaises(QueryError):
            search(Mock(), " ", SearchOptions(), embed)
        embed.assert_not_called()

    def test_embedding_failure_never_falls_back(self):
        client = Mock()
        with self.assertRaises(QueryError):
            search(
                client, "问题", SearchOptions(), Mock(side_effect=QueryError("embedding_denied"))
            )
        client.hybrid_search.assert_not_called()
        client.search.assert_not_called()

    def test_rrf_request_has_candidate_limit_and_output_fields(self):
        client = Mock()
        client.hybrid_search.return_value = [[]]
        result = search(client, "问题", SearchOptions(), Mock(return_value=VECTOR))
        args = client.hybrid_search.call_args.kwargs
        self.assertEqual(args["limit"], 50)
        self.assertIsInstance(args["ranker"], RRFRanker)
        self.assertEqual(args["ranker"].dict(), {"strategy": "rrf", "params": {"k": 60}})
        self.assertIn("document_name", args["output_fields"])
        self.assertEqual(result["hits"], [])
        self.assertEqual(result["status"], "empty")

    def test_bm25_never_embeds(self):
        client, embed = Mock(), Mock()
        client.search.return_value = [[]]
        search(client, "问题", SearchOptions(mode="bm25"), embed)
        embed.assert_not_called()
        self.assertEqual(client.search.call_args.kwargs["anns_field"], "sparse_vector")

    def test_output_preserves_citation_and_names(self):
        entity = {
            "chunk_id": CHUNK,
            "document_id": DOC,
            "document_name": "测试条例",
            "text": "第一条 正文",
            "article_no": "第一条",
            "chunk_type": "provision",
            "parent_chunk_id": None,
            "model": "qwen3.7-text-embedding",
        }
        hit = {"id": CHUNK, "distance": 0.02, "entity": entity}
        output = format_hits([hit], 10, "hybrid")
        self.assertEqual(output[0]["document_name"], "测试条例")
        self.assertEqual(output[0]["chunk_id"], CHUNK)
        self.assertEqual(output[0]["fusion_score"], 0.02)
        with self.assertRaises(QueryError):
            format_hits([hit, hit], 10, "hybrid")

    def test_exact_query_requires_document_and_escapes_article(self):
        client = Mock()
        client.query.return_value = []
        exact(client, DOC, '第一条" or true', 10)
        self.assertIn('\\"', client.query.call_args.kwargs["filter"])
        self.assertIn(DOC, client.query.call_args.kwargs["filter"])
        with self.assertRaises(QueryError):
            exact(client, "bad", "第一条", 10)


if __name__ == "__main__":
    unittest.main()
