"""名称回填仅允许已存在且来源匹配的记录；不允许覆盖冲突名称。"""

import unittest
from unittest.mock import patch

from scripts import backfill_milvus_document_names as subject


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.source = [{"chunk_id": "a", "document_id": "d", "vector": [1]}]
        self.actual = [{"chunk_id": "a", "document_id": "d", "document_name": None}]

    def test_partial_payload_has_no_original_fields(self):
        payload = subject.make_updates(self.source, self.actual, {"d": "名称"})
        self.assertEqual(payload, [{"chunk_id": "a", "document_name": "名称"}])

    def test_missing_duplicate_and_wrong_document_rejected(self):
        for rows in ([], self.actual * 2, [{"chunk_id": "a", "document_id": "wrong"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                subject.make_updates(self.source, rows, {"d": "名称"})

    def test_conflicting_name_rejected(self):
        self.actual[0]["document_name"] = "用户修改"
        with self.assertRaises(ValueError):
            subject.make_updates(self.source, self.actual, {"d": "名称"})

    def test_identical_name_is_idempotent(self):
        self.actual[0]["document_name"] = "名称"
        self.assertEqual(subject.make_updates(self.source, self.actual, {"d": "名称"}), [])

    def test_utf8_capacity_and_missing_name(self):
        for names in ({}, {"d": ""}, {"d": "中" * 342}):
            with self.subTest(names=names), self.assertRaises(ValueError):
                subject.make_updates(self.source, self.actual, names)

    def test_write_is_explicitly_partial_and_acknowledged(self):
        with patch.object(
            subject, "api", return_value={"upsertCount": 1, "upsertIds": ["a"]}
        ) as api:
            subject.write_updates([{"chunk_id": "a", "document_name": "名称"}])
            self.assertTrue(api.call_args.args[1]["partialUpdate"])
        with patch.object(subject, "api", return_value={"upsertCount": 0}):
            with self.assertRaises(ValueError):
                subject.write_updates([{"chunk_id": "a", "document_name": "名称"}])

    def test_uncertain_write_replay_only_updates_unfinished_rows(self):
        source = self.source + [{"chunk_id": "b", "document_id": "d"}]
        # 模拟服务端已提交第一条，但客户端在确认前断线。
        existing = [
            {"chunk_id": "a", "document_id": "d", "document_name": "名称"},
            {"chunk_id": "b", "document_id": "d", "document_name": None},
        ]
        self.assertEqual(
            subject.make_updates(source, existing, {"d": "名称"}),
            [{"chunk_id": "b", "document_name": "名称"}],
        )

    def test_wrong_ack_ids_rejected(self):
        with patch.object(subject, "api", return_value={"upsertCount": 1, "upsertIds": ["wrong"]}):
            with self.assertRaises(ValueError):
                subject.write_updates([{"chunk_id": "a", "document_name": "名称"}])

    def test_query_uses_strong_and_preserves_requested_ids(self):
        with patch.object(subject, "api", return_value=self.actual) as api:
            self.assertEqual(subject.query(self.source), self.actual)
            body = api.call_args.args[1]
            self.assertEqual(body["consistencyLevel"], "Strong")
            self.assertEqual(body["filter"], 'chunk_id in ["a"]')
            self.assertIn("document_id", body["outputFields"])
            self.assertIn("document_name", body["outputFields"])

    def test_already_complete_group_does_not_write(self):
        with patch.object(subject, "api") as api:
            subject.write_updates([])
            api.assert_not_called()


if __name__ == "__main__":
    unittest.main()
