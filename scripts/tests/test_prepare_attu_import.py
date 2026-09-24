"""Attu 转换脚本的合成数据测试；不访问真实语料、API 或数据库服务。"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "prepare_attu_import.py"
spec = importlib.util.spec_from_file_location("prepare_attu_import", SCRIPT)
assert spec and spec.loader
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class PreparationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.print_patch = patch("builtins.print")
        self.print_patch.start()
        self.addCleanup(self.print_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chunks = self.root / "chunks"
        self.vectors = self.root / "vectors"
        self.output = self.root / "output"
        self.chunks.mkdir()
        self.vectors.mkdir()
        self.entries: list[dict] = []
        self.add_document(1)

    def add_document(
        self, number: int, texts: tuple[str, ...] = ("第一条 合成测试。", "－1－")
    ) -> str:
        folder = f"{number:024x}"
        location = self.chunks / folder
        location.mkdir()
        rows = []
        for i, text in enumerate(texts):
            rows.append(
                {
                    "chunk_id": digest(f"{number}:{i}"),
                    "text": text,
                    "content_sha256": digest(text),
                    "chunk_type": "provision" if i == 0 else "source_paragraph",
                    "article_no": "第一条" if i == 0 else None,
                    "parent_chunk_id": None,
                    "structure_path": ["第一章 总则"],
                    "source_paragraph_ordinals": [i],
                    "source_char_range": None,
                    "parent_relative_char_span": [0, len(text)],
                }
            )
        raw = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode()
        (location / "chunks.jsonl").write_bytes(raw)
        doc = {
            "document_id": digest(f"document:{number}"),
            "source_sha256": digest(f"source:{number}"),
            "id_namespace": "offline-export-v1",
            "schema_version": "legal-corpus-export-v2",
            "chunk_count": len(rows),
            "source_path": f"合成来源/{number}.docx",
            "output_hashes": {"chunks.jsonl": digest(raw)},
        }
        write_json(location / "document.json", doc)
        entry = {
            "output_directory": folder,
            "status": "completed",
            "document_id": doc["document_id"],
            "source_sha256": doc["source_sha256"],
        }
        self.entries.append(entry)
        (self.chunks / "manifest.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.entries),
            encoding="utf-8",
        )
        write_json(
            self.vectors / f"{folder}.json",
            {
                "model": "qwen3.7-text-embedding",
                "dimensions": 1024,
                "chunks_sha256": digest(raw),
                "document": doc,
                "rows": [{**r, "vector": [0.125] * 1024} for r in rows],
            },
        )
        return folder

    def mutate(self, change, number: int = 1) -> None:
        path = self.vectors / f"{number:024x}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        write_json(path, data)

    def execute(self, **kwargs):
        config = app.Config(self.vectors, self.chunks, self.output, **kwargs)
        return app.prepare(config)

    def assert_failed(self, **kwargs):
        result = self.execute(**kwargs)
        self.assertEqual(result["status"], "failed")
        self.assertFalse((Path(result["run_directory"]) / "import").exists())
        self.assertGreater(result["error_count"], 0)
        return result

    def test_export_preserves_text_nulls_and_metadata_and_splits(self):
        before = {p: digest(p.read_bytes()) for p in self.root.rglob("*.json*")}
        result = self.execute(batch_bytes=8500)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["exported_rows"], 2)
        run = Path(result["run_directory"])
        files = list((run / "import").glob("*.jsonl"))
        self.assertEqual(len(files), 2)
        records = [
            json.loads(line)
            for p in sorted(files)
            for line in p.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(set(records[0]), set(app.IMPORT_FIELDS))
        self.assertEqual(records[0]["text"], "第一条 合成测试。")
        self.assertIsNone(records[1]["article_no"])
        self.assertEqual(records[1]["text"], "－1－")
        self.assertEqual(records[0]["vector"], [0.125] * 1024)
        self.assertEqual(records[0]["document_id"], self.entries[0]["document_id"])
        self.assertTrue((run / "provenance" / "chunks.jsonl").exists())
        for p, sha in before.items():
            self.assertEqual(digest(p.read_bytes()), sha)

    def test_check_only_has_no_import_batches(self):
        result = self.execute(check_only=True)
        self.assertEqual(result["status"], "checked")
        self.assertEqual(result["validated_rows"], 2)
        self.assertFalse((Path(result["run_directory"]) / "import").exists())

    def test_sample_does_not_claim_full_corpus(self):
        self.add_document(2)
        result = self.execute(limit=1)
        self.assertEqual(result["status"], "sample_ready")
        self.assertEqual(result["expected_documents"], 2)
        self.assertEqual(result["selected_documents"], 1)

    def test_missing_and_unexpected_files_fail(self):
        (self.vectors / f"{1:024x}.json").rename(self.vectors / "unrelated.json")
        result = self.assert_failed()
        self.assertEqual(result["missing_files"], 1)
        self.assertEqual(result["unexpected_files"], 1)

    def test_vector_types_dimensions_and_finite_float32(self):
        for vector in ([0.1] * 3, [True] * 1024, [math.inf] * 1024, [0.0] * 1024, [1e100] * 1024):
            with self.subTest(vector=vector[0]):
                self.mutate(lambda d, value=vector: d["rows"][0].update(vector=value))
                self.assert_failed()

    def test_model_and_dimension_identity(self):
        self.mutate(lambda d: d.update(model="another-model"))
        self.assert_failed()
        self.mutate(lambda d: d.update(model="qwen3.7-text-embedding", dimensions=1792))
        self.assert_failed()

    def test_text_drift_and_hash_drift_rejected(self):
        self.mutate(lambda d: d["rows"][0].update(text="被篡改的正文"))
        self.assert_failed()
        self.mutate(lambda d: d.update(chunks_sha256="0" * 64))
        self.assert_failed()

    def test_empty_text_and_utf8_limits(self):
        self.add_document(2, ("中" * 22000,))
        self.assert_failed()

    def test_duplicate_ids_across_files(self):
        self.add_document(2)
        # 同时修改第二份的原始切块及向量，证明去重不是只靠来源差异检查。
        self.rewrite_source(2, lambda rows: rows[0].update(chunk_id=digest("1:0")))
        self.assert_failed()

    def rewrite_source(self, number, change):
        path = self.vectors / f"{number:024x}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data["rows"])
        raw = "".join(
            json.dumps({k: v for k, v in row.items() if k != "vector"}, ensure_ascii=False) + "\n"
            for row in data["rows"]
        ).encode()
        data["chunks_sha256"] = digest(raw)
        data["document"]["output_hashes"]["chunks.jsonl"] = digest(raw)
        (self.chunks / f"{number:024x}" / "chunks.jsonl").write_bytes(raw)
        write_json(self.chunks / f"{number:024x}" / "document.json", data["document"])
        write_json(path, data)

    def test_missing_parent_rejected(self):
        self.rewrite_source(1, lambda rows: rows[0].update(parent_chunk_id="f" * 64))
        self.assert_failed()

    def test_document_count_and_metadata_mismatch(self):
        self.mutate(lambda d: d["document"].update(chunk_count=999))
        self.assert_failed()

    def test_repeated_runs_do_not_overwrite(self):
        a, b = self.execute(), self.execute()
        self.assertNotEqual(a["run_directory"], b["run_directory"])
        self.assertTrue((Path(a["run_directory"]) / "import").exists())

    def test_nested_output_is_rejected_before_writing(self):
        for root in (self.vectors / "output", self.chunks, self.root):
            with self.subTest(root=root), self.assertRaises(ValueError):
                app.prepare(app.Config(self.vectors, self.chunks, root))

    def test_input_limit_and_truncated_json(self):
        self.assert_failed(max_file_bytes=100)
        (self.vectors / f"{1:024x}.json").write_text('{"rows": [', encoding="utf-8")
        self.assert_failed()

    def test_duplicate_json_keys_rejected(self):
        p = self.vectors / f"{1:024x}.json"
        p.write_text(
            p.read_text(encoding="utf-8").replace(
                '"dimensions": 1024', '"dimensions": 3, "dimensions": 1024'
            ),
            encoding="utf-8",
        )
        self.assert_failed()

    def test_interrupt_never_publishes(self):
        with patch.object(app, "validate_document", side_effect=KeyboardInterrupt):
            result = self.execute()
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse((Path(result["run_directory"]) / "import").exists())

    def test_valid_child_and_foreign_parent(self):
        self.rewrite_source(1, lambda rows: rows[1].update(parent_chunk_id=rows[0]["chunk_id"]))
        self.assertEqual(self.execute()["status"], "ready")
        self.add_document(2)
        self.rewrite_source(2, lambda rows: rows[1].update(parent_chunk_id=digest("1:0")))
        self.assert_failed()

    def test_empty_text_is_rejected_even_when_source_matches(self):
        self.add_document(2, (" ",))
        self.assert_failed()

    def test_nullable_scalar_length_is_bytes(self):
        self.rewrite_source(1, lambda rows: rows[0].update(article_no="条" * 86))
        self.assert_failed()

    def test_input_changed_before_completion_is_rejected(self):
        original = app.Reader.verify_unchanged

        def modify_then_verify(reader):
            p = self.vectors / f"{1:024x}.json"
            p.write_bytes(p.read_bytes() + b" ")
            original(reader)

        with patch.object(app.Reader, "verify_unchanged", modify_then_verify):
            self.assert_failed()

    def test_corrupted_staged_output_is_rejected(self):
        original = app.BatchWriter.verify

        def corrupt_then_verify(writer):
            next(writer.directory.glob("*.jsonl")).write_text("{}\n", encoding="utf-8")
            original(writer)

        with patch.object(app.BatchWriter, "verify", corrupt_then_verify):
            self.assert_failed()

    def test_duplicate_or_unfinished_manifest(self):
        p = self.chunks / "manifest.jsonl"
        p.write_bytes(p.read_bytes() * 2)
        self.assert_failed()
        p.write_text(json.dumps({**self.entries[0], "status": "failed"}) + "\n", encoding="utf-8")
        self.assert_failed()

    def test_batch_too_small_does_not_publish_partial_document(self):
        self.assert_failed(batch_bytes=10)

    def test_output_write_failure_has_explicit_report(self):
        with patch.object(app.BatchWriter, "add", side_effect=OSError("synthetic disk full")):
            self.assert_failed()

    def test_publish_rename_failure_has_error_detail(self):
        original = Path.rename

        def fail_publish(path, target):
            if path.name == "_staging_do_not_import":
                raise OSError("synthetic publish failure")
            return original(path, target)

        with patch.object(Path, "rename", fail_publish):
            result = self.assert_failed()
        error_path = Path(result["run_directory"]) / "errors.jsonl"
        self.assertIn("publish_failed", error_path.read_text(encoding="utf-8"))

    def test_fsync_failure_closes_resources_and_reports_failure(self):
        with patch.object(app.os, "fsync", side_effect=OSError("synthetic full disk")):
            self.assert_failed()

    def test_final_report_failure_never_exposes_import_directory(self):
        original = Path.write_bytes

        def fail_report(path, data):
            if path.name == "report.json.tmp":
                raise OSError("synthetic report write failure")
            return original(path, data)

        with patch.object(Path, "write_bytes", fail_report):
            self.assert_failed()

    def test_final_report_interrupt_never_exposes_import_directory(self):
        original = Path.replace

        def interrupt_report(path, target):
            if path.name == "report.json.tmp":
                raise KeyboardInterrupt
            return original(path, target)

        with patch.object(Path, "replace", interrupt_report):
            result = self.execute()
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse((Path(result["run_directory"]) / "import").exists())

    def test_extra_row_document_id_cannot_override_provenance(self):
        self.mutate(lambda d: d["rows"][0].update(document_id="foreign-document"))
        self.assert_failed()

    def test_interrupt_immediately_after_rename_rolls_back(self):
        original = Path.rename

        def rename_then_interrupt(path, target):
            result = original(path, target)
            if path.name == "_staging_do_not_import":
                raise KeyboardInterrupt
            return result

        with patch.object(Path, "rename", rename_then_interrupt):
            result = self.execute()
        self.assertEqual(result["status"], "interrupted")
        self.assertFalse((Path(result["run_directory"]) / "import").exists())


if __name__ == "__main__":
    unittest.main()
