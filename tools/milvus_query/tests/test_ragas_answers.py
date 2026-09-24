import unittest

from evaluate_ragas_answers import answer_messages, cache_key, summarize


class AnswerEvaluationTests(unittest.TestCase):
    def test_generation_never_receives_reference(self):
        case = {"question": "问题", "reference": "GOLD_CANARY"}
        messages = answer_messages(case, ["检索内容"])
        self.assertIn("检索内容", messages[1]["content"])
        self.assertNotIn("GOLD_CANARY", str(messages))

    def test_strictness_samples_have_distinct_cache_keys(self):
        self.assertNotEqual(cache_key("a", 1, "p", {}), cache_key("a", 2, "p", {}))

    def test_complete_matrix_and_finite_scores_required(self):
        rows = [
            {
                "case_id": "a",
                "stage": s,
                "precision": 1,
                "recall": 1,
                "answer_relevancy": -0.1,
                "faithfulness": 0.5,
            }
            for s in ("rrf", "rerank")
        ]
        self.assertEqual(summarize(rows, ["a"])["status"], "completed")
        self.assertEqual(summarize(rows[:1], ["a"])["status"], "incomplete")
        with self.assertRaises(ValueError):
            summarize(rows + rows, ["a"])
        rows[0]["faithfulness"] = float("nan")
        with self.assertRaises(ValueError):
            summarize(rows, ["a"])
