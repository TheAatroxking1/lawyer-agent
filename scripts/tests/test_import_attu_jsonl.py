"""仅合成数据验证导入器，不连接实际 Milvus。"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "import_attu_jsonl", Path(__file__).resolve().parents[1] / "import_attu_jsonl.py"
)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


class ImportTests(unittest.TestCase):
    def test_only_complete_report_is_accepted(self):
        for status in ("running", "failed", "sample_ready", "interrupted"):
            with self.subTest(status=status), self.assertRaises(ValueError):
                app.validate_report({"status": status})

    def test_report_counts_must_agree(self):
        report = {
            "status": "ready",
            "scope": "full",
            "error_count": 0,
            "model": app.MODEL,
            "dimensions": 1024,
            "expected_documents": 2,
            "validated_documents": 2,
            "validated_rows": 3,
            "exported_rows": 3,
            "batches": [{"file": "import_00001.jsonl", "rows": 3, "bytes": 20, "sha256": "a" * 64}],
        }
        app.validate_report(report)
        report["batches"][0]["rows"] = 2
        with self.assertRaises(ValueError):
            app.validate_report(report)

    def test_matching_ack_ids_and_counts(self):
        rows = [{"chunk_id": "a"}, {"chunk_id": "b"}]
        app.validate_ack({"upsertCount": 2, "upsertIds": ["b", "a"]}, rows)
        for result in (
            {"upsertCount": 1, "upsertIds": ["a"]},
            {"upsertCount": 2, "upsertIds": ["a", "a"]},
            {"upsertCount": 2, "upsertIds": ["a", "c"]},
        ):
            with self.assertRaises(ValueError):
                app.validate_ack(result, rows)

    def test_float32_readback_and_text_mismatch(self):
        source = {"chunk_id": "a", "text": "第一条", "vector": [0.1] * 1024}
        app.validate_readback(source, {**source, "vector": [0.10000000149011612] * 1024})
        with self.assertRaises(ValueError):
            app.validate_readback(source, {**source, "text": "改过的正文"})

    def test_source_file_proof_and_path_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = b'{"chunk_id":"a"}\n'
            p = root / "import_00001.jsonl"
            p.write_bytes(raw)
            proof = {"file": p.name, "bytes": len(raw), "rows": 1, "sha256": app.sha(raw)}
            self.assertEqual(app.read_batch(root, proof), [{"chunk_id": "a"}])
            proof["sha256"] = "0" * 64
            with self.assertRaises(ValueError):
                app.read_batch(root, proof)
            proof["file"] = "../input.jsonl"
            with self.assertRaises(ValueError):
                app.read_batch(root, proof)

    def test_checkpoint_is_written_as_utf8_json(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "state.json"
            app.save(p, {"status": "paused", "note": "合成"})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["note"], "合成")

    def test_resume_rejects_unexplained_extra_rows(self):
        state = {"acknowledged_rows": 3, "pending": None}
        with self.assertRaises(ValueError):
            app.validate_resume_count(state, 4, 0)
        app.validate_resume_count(state, 3, 0)

    def test_pending_count_only_accepts_verified_existing_records(self):
        state = {"acknowledged_rows": 3, "pending": {"count": 2}}
        app.validate_resume_count(state, 4, 1)
        with self.assertRaises(ValueError):
            app.validate_resume_count(state, 5, 1)

    def test_existing_pending_rows_must_be_identical(self):
        rows = [{"chunk_id": "a", "text": "原文", "vector": [0.1] * 1024}]
        app.validate_existing(rows, rows)
        with self.assertRaises(ValueError):
            app.validate_existing(rows, [{**rows[0], "text": "其它导入的正文"}])
        with self.assertRaises(ValueError):
            app.validate_existing(rows, [{**rows[0], "chunk_id": "foreign"}])


if __name__ == "__main__":
    unittest.main()
