import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from evaluate_ragas import (
    article_span,
    coverage,
    evaluation_lock,
    retrieval_identity,
    summarize,
)
from legal_query.window_corpus import save


class RagasEvalTests(unittest.TestCase):
    def test_cache_identity_tracks_model_endpoint_but_not_key(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            embedding = root / "embedding.json"
            secret = {
                "base_url": "https://example.invalid/v1",
                "api_key": "synthetic",
                "model": "qwen3.7-text-embedding",
                "dimensions": 1024,
            }
            save(embedding, secret)
            config = root / "config.json"
            settings = {
                "embedding_config_file": str(embedding),
                "rerank": {
                    "model": "qwen3-rerank",
                    "endpoint": "https://example.invalid/reranks",
                },
            }
            save(config, settings)
            before = retrieval_identity(config)
            secret["api_key"] = "rotated"
            save(embedding, secret)
            self.assertEqual(before, retrieval_identity(config))
            settings["rerank"]["model"] = "qwen3.7-text-rerank"
            save(config, settings)
            self.assertNotEqual(before, retrieval_identity(config))

    def test_same_output_cannot_have_two_runners(self):
        with (
            TemporaryDirectory() as tmp,
            evaluation_lock(Path(tmp)),
            self.assertRaisesRegex(ValueError, "evaluation_already_running"),
            evaluation_lock(Path(tmp)),
        ):
            pass

    def test_article_boundary_not_inline_citation_or_next_heading(self):
        text = "法名\n第一条 内容参见第二条。\n第二款内容。\n第二章 新章\n第二条 其他。"
        start, end = article_span(text, "第一条")
        self.assertEqual(text[start:end], "第一条 内容参见第二条。\n第二款内容。")
        with self.assertRaises(ValueError):
            article_span(text, "第三条")

    def test_overlap_coverage_unions_only_correct_document(self):
        refs = [{"document_id": "a", "start": 0, "end": 10}]

        def row(doc, start, end):
            return {
                "metadata": {"document_id": doc, "start_char": start, "end_char": end}
            }

        self.assertEqual(
            coverage(refs, [row("a", 0, 6), row("a", 4, 10)])["full_article_recall"], 1
        )
        self.assertEqual(
            coverage(refs, [row("a", 0, 4), row("b", 4, 10)])["character_coverage"], 0.4
        )
        self.assertEqual(
            coverage(refs, [row("a", 0, 4), row("a", 6, 10)])["full_article_recall"], 0
        )

    def test_missing_or_invalid_metric_cannot_be_complete(self):
        records = [{"case_id": "a", "stage": "rrf", "precision": 1.0, "recall": 0.5}]
        self.assertEqual(summarize(records, ["a"])["status"], "incomplete")
        records.append(
            {"case_id": "a", "stage": "rerank", "precision": 0.5, "recall": 1.0}
        )
        self.assertEqual(
            summarize(records, ["a"])["stages"]["rrf"]["context_recall"], 0.5
        )
        records[-1]["recall"] = float("nan")
        with self.assertRaises(ValueError):
            summarize(records, ["a"])


if __name__ == "__main__":
    unittest.main()
