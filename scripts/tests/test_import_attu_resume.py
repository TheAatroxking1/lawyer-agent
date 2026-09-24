"""导入恢复与归属保护的合成测试；所有 API 调用均由内存集合替代。"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "import_attu_resume_subject",
    Path(__file__).resolve().parents[1] / "import_attu_jsonl.py",
)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class ResumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = {}
        self.writes = []
        self.timeout_writes = 0
        self.rows = [
            {
                "chunk_id": app.sha(f"chunk:{index}".encode()),
                "text": f"第{index}条 合成正文。",
                "model": app.MODEL,
                "document_id": "synthetic-document",
                "chunk_type": "provision",
                "article_no": None,
                "parent_chunk_id": None,
                "vector": [0.1] * 1024,
            }
            for index in range(300)
        ]
        self.report_path = self.make_report("first")
        for replacement in (
            patch.object(app, "api", side_effect=self.fake_api),
            patch.object(app, "LOCK_ROOT", self.root / "shared-locks", create=True),
            patch.object(app.time, "sleep"),
            patch("builtins.print"),
            # 即使后续重构漏过 api mock，也绝不允许测试访问实际服务。
            patch.object(
                app.urllib.request,
                "urlopen",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)

    def make_report(self, name):
        folder = self.root / name / "import"
        folder.mkdir(parents=True)
        batches = []
        # 跨文件边界继续，覆盖最后一个不足256行的请求和文件游标复位。
        for number, rows in enumerate((self.rows[:260], self.rows[260:]), 1):
            raw = (
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n"
            ).encode()
            filename = f"import_{number:05d}.jsonl"
            (folder / filename).write_bytes(raw)
            batches.append(
                {
                    "file": filename,
                    "rows": len(rows),
                    "bytes": len(raw),
                    "sha256": app.sha(raw),
                }
            )
        report = {
            "status": "ready",
            "scope": "full",
            "error_count": 0,
            "model": app.MODEL,
            "dimensions": 1024,
            "expected_documents": 1,
            "validated_documents": 1,
            "validated_rows": 300,
            "exported_rows": 300,
            "batches": batches,
        }
        path = folder.parent / "report.json"
        path.write_text(json.dumps(report), encoding="utf-8")
        return path

    def schema(self):
        fields = [
            {
                "name": name,
                "type": "VarChar",
                "params": [
                    {"key": "max_length", "value": "65535" if name == "text" else "256"}
                ],
                "primaryKey": name == "chunk_id",
                "nullable": name in ("article_no", "parent_chunk_id"),
            }
            for name in app.FIELDS
            if name != "vector"
        ]
        fields.extend(
            [
                {
                    "name": "vector",
                    "type": "FloatVector",
                    "params": [{"key": "dim", "value": "1024"}],
                },
                {
                    "name": "sparse_vector",
                    "type": "SparseFloatVector",
                    "isFunctionOutput": True,
                },
            ]
        )
        return {
            "collectionID": 469076060698085606,
            "autoId": False,
            "enableDynamicField": False,
            "fields": fields,
            "functions": [
                {
                    "inputFieldNames": ["text"],
                    "outputFieldNames": ["sparse_vector"],
                    "type": 1,
                }
            ],
        }

    def fake_api(self, route, body, timeout=120):
        if route == "collections/describe":
            return self.schema()
        if route == "entities/query":
            if body["outputFields"] == ["count(*)"]:
                return [{"count(*)": len(self.database)}]
            ids = json.loads(body["filter"].split(" in ", 1)[1])
            return [dict(self.database[key]) for key in ids if key in self.database]
        if route == "entities/get":
            ids = body.get("id", body.get("ids", []))
            return [dict(self.database[key]) for key in ids if key in self.database]
        if route == "entities/upsert":
            rows = body["data"]
            self.writes.append([row["chunk_id"] for row in rows])
            self.database.update({row["chunk_id"]: dict(row) for row in rows})
            if self.timeout_writes:
                self.timeout_writes -= 1
                raise TimeoutError("synthetic response lost after commit")
            return {
                "upsertCount": len(rows),
                "upsertIds": [row["chunk_id"] for row in rows],
            }
        raise AssertionError(f"unexpected API route: {route}")

    def execute(self, max_requests=None, report=None):
        return app.run(report or self.report_path, True, max_requests)

    def checkpoint(self):
        path = self.report_path.parent / "milvus-blog-lawyer_db" / "state.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def prepare_uncertain_write(self):
        self.timeout_writes = 3
        with self.assertRaises(TimeoutError):
            self.execute()
        self.assertEqual(len(self.database), 256)
        self.assertEqual(self.checkpoint()["acknowledged_rows"], 0)

    def test_normal_pause_resume_preserves_counts_and_file_cursor(self):
        first = self.execute(max_requests=1)
        self.assertEqual(first["status"], "paused")
        self.assertEqual(first["acknowledged_rows"], 256)
        final = self.execute()
        self.assertEqual(final["status"], "data_verified")
        self.assertEqual(final["acknowledged_rows"], 300)
        self.assertEqual(final["verified_files"], 2)
        self.assertEqual(final["file_index"], 2)
        self.assertEqual(final["row_offset"], 0)
        self.assertEqual([len(group) for group in self.writes], [256, 4, 40])
        self.assertEqual(self.database, {row["chunk_id"]: row for row in self.rows})

    def test_paused_resume_rejects_foreign_next_id_without_overwriting(self):
        self.execute(max_requests=1)
        foreign = {**self.rows[256], "text": "其他写入者的正文，不得覆盖"}
        self.database[foreign["chunk_id"]] = foreign
        before = len(self.writes)
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(len(self.writes), before)
        self.assertEqual(self.database[foreign["chunk_id"]], foreign)

    def test_committed_timeout_is_replayed_without_double_counting(self):
        self.prepare_uncertain_write()
        self.assertIsNotNone(self.checkpoint().get("pending"))
        final = self.execute()
        self.assertEqual(final["status"], "data_verified")
        self.assertEqual(final["acknowledged_rows"], 300)
        self.assertEqual(len(self.database), 300)
        self.assertEqual(self.writes[0], self.writes[1])
        self.assertEqual(self.writes[0], self.writes[3])
        self.assertIsNone(self.checkpoint().get("pending"))

    def test_single_lost_response_retries_same_batch(self):
        self.timeout_writes = 1
        final = self.execute()
        self.assertEqual(final["status"], "data_verified")
        self.assertEqual(final["acknowledged_rows"], 300)
        self.assertEqual(self.writes[0], self.writes[1])
        self.assertEqual(len(self.database), 300)

    def test_uncertain_resume_rejects_changed_pending_content(self):
        self.prepare_uncertain_write()
        chunk_id = self.rows[100]["chunk_id"]
        self.database[chunk_id] = {
            **self.database[chunk_id],
            "text": "不属于原批次的内容",
        }
        before = len(self.writes)
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(len(self.writes), before)
        self.assertEqual(self.database[chunk_id]["text"], "不属于原批次的内容")

    def test_uncertain_resume_rejects_unexplained_additional_rows(self):
        self.prepare_uncertain_write()
        self.database["foreign-unrelated-id"] = {
            **self.rows[299],
            "chunk_id": "foreign-unrelated-id",
        }
        before = len(self.writes)
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(len(self.writes), before)
        self.assertIn("foreign-unrelated-id", self.database)

    def test_collection_lock_path_is_shared_across_report_directories(self):
        paths = []
        original = Path.open

        def observed_open(path, *args, **kwargs):
            if path.suffix == ".lock":
                paths.append(path.resolve())
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", observed_open):
            self.execute(max_requests=1)
            # 同一个合成集合的新空状态只用于比较锁位置，不写真实数据库。
            self.database.clear()
            self.execute(max_requests=1, report=self.make_report("second"))
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0], paths[1])
        self.assertTrue(paths[0].is_relative_to(self.root / "shared-locks"))


if __name__ == "__main__":
    unittest.main()
