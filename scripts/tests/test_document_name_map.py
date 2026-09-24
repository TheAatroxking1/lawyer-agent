"""文书名称映射的来源绑定、歧义保留和查询行为测试，全部使用合成文件。"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


def write_json(path: Path, value: Any) -> bytes:
    raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
    path.write_bytes(raw)
    return raw


def write_lines(path: Path, rows: list[dict[str, Any]]) -> bytes:
    raw = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    path.write_bytes(raw)
    return raw


class DocumentNameMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.assertIsNotNone(
            importlib.util.find_spec("scripts.build_document_name_map"),
            "document name mapping implementation is missing",
        )
        self.module = importlib.import_module("scripts.build_document_name_map")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.chunks = self.root / "chunks"
        self.chunks.mkdir()
        (self.root / "provenance").mkdir()
        (self.root / "milvus-blog-lawyer_db").mkdir()
        self.documents = [
            {
                "document_id": "a" * 64,
                "id_namespace": "offline-export-v1",
                "source_path": "F:\\法律\\甲条例_20200528.docx",
                "source_relative_path": "法律/甲条例_20200528.docx",
                "source_sha256": "1" * 64,
                "chunk_count": 3,
            },
            {
                "document_id": "b" * 64,
                "id_namespace": "offline-export-v1",
                "source_path": "F:\\法律\\乙条例（修订）_20240101.doc",
                "source_relative_path": "法律/乙条例（修订）_20240101.doc",
                "source_sha256": "2" * 64,
                "chunk_count": 2,
            },
        ]
        self.report_path = self.root / "report.json"
        self.fixture()

    def fixture(self) -> None:
        manifest = write_lines(
            self.chunks / "manifest.jsonl",
            [{**doc, "status": "completed"} for doc in self.documents],
        )
        write_lines(
            self.root / "provenance/documents.jsonl",
            [{"document": doc} for doc in self.documents],
        )
        report = {
            "status": "ready",
            "scope": "full",
            "error_count": 0,
            "expected_documents": 2,
            "validated_documents": 2,
            "validated_rows": 5,
            "exported_rows": 5,
            "chunks_directory": str(self.chunks),
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        }
        raw = write_json(self.report_path, report)
        write_json(
            self.root / "milvus-blog-lawyer_db/state.json",
            {
                "status": "data_verified",
                "acknowledged_rows": 5,
                "final_count": 5,
                "binding": {"source_report_sha256": hashlib.sha256(raw).hexdigest()},
            },
        )

    def build(self) -> Any:
        directory = self.module.build(self.report_path)
        return self.module.DocumentNameMap(directory)

    def test_names_preserve_chinese_dates_and_versions(self) -> None:
        names = self.build()
        self.assertEqual(names.lookup("a" * 64), "甲条例_20200528")
        self.assertEqual(names.lookup("b" * 64), "乙条例（修订）_20240101")

    def test_same_names_keep_all_document_ids(self) -> None:
        self.documents[1]["source_path"] = "F:\\其他\\甲条例_20200528.docx"
        self.documents[1]["source_relative_path"] = "其他/甲条例_20200528.docx"
        self.fixture()
        names = self.build()
        self.assertEqual({r["document_id"] for r in names.find("甲条例")}, {"a" * 64, "b" * 64})

    def test_enrich_preserves_input_and_existing_fields(self) -> None:
        names = self.build()
        hit = {"document_id": "a" * 64, "article_no": "第一条", "text": "合成原文", "vector": [1.0]}
        result = names.enrich([hit])
        self.assertNotIn("document_name", hit)
        self.assertEqual(result[0], {**hit, "document_name": "甲条例_20200528"})

    def test_unknown_identity_fails_instead_of_guessing(self) -> None:
        names = self.build()
        with self.assertRaises(KeyError):
            names.lookup("c" * 64)
        with self.assertRaises(KeyError):
            names.enrich([{"document_id": "c" * 64}])

    def test_conflicting_hit_name_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "name_conflict"):
            self.build().enrich([{"document_id": "a" * 64, "document_name": "别的法律"}])

    def test_empty_name_search_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.build().find("  ")

    def test_duplicate_document_id_rejected(self) -> None:
        self.documents[1]["document_id"] = "a" * 64
        self.fixture()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.build()

    def test_provenance_source_mismatch_rejected(self) -> None:
        self.documents[0]["source_path"] = "F:\\法律\\被替换.docx"
        write_lines(
            self.root / "provenance/documents.jsonl", [{"document": d} for d in self.documents]
        )
        with self.assertRaisesRegex(ValueError, "source_mismatch"):
            self.build()
        self.assertFalse((self.root / "document-names").exists())

    def test_manifest_change_rejected(self) -> None:
        with (self.chunks / "manifest.jsonl").open("ab") as handle:
            handle.write(b"\n")
        with self.assertRaisesRegex(ValueError, "manifest_hash"):
            self.build()

    def test_missing_document_rejected(self) -> None:
        write_lines(self.root / "provenance/documents.jsonl", [{"document": self.documents[0]}])
        with self.assertRaisesRegex(ValueError, "coverage"):
            self.build()

    def test_wrong_chunk_total_rejected(self) -> None:
        self.documents[0]["chunk_count"] = 9
        self.fixture()
        with self.assertRaisesRegex(ValueError, "chunk_count"):
            self.build()

    def test_incomplete_import_rejected(self) -> None:
        path = self.root / "milvus-blog-lawyer_db/state.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["status"] = "running"
        write_json(path, value)
        with self.assertRaisesRegex(ValueError, "import_not_verified"):
            self.build()

    def test_map_tampering_detected_on_read_and_rerun(self) -> None:
        self.build()
        path = self.root / "document-names/document_names.json"
        write_json(path, {"a" * 64: "错误名称", "b" * 64: "乙条例"})
        with self.assertRaisesRegex(ValueError, "map_hash"):
            self.module.DocumentNameMap(path.parent)
        with self.assertRaisesRegex(ValueError, "map_hash"):
            self.build()
        self.assertIn("错误名称", path.read_text(encoding="utf-8"))

    def test_identical_rerun_keeps_files_unchanged(self) -> None:
        self.build()
        paths = list((self.root / "document-names").iterdir())
        original = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths}
        self.build()
        self.assertEqual(original, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in paths})

    def test_readonly_source_cannot_be_output(self) -> None:
        with patch.object(self.module, "READ_ONLY_SOURCE", self.root, create=True):
            with self.assertRaisesRegex(ValueError, "readonly_source"):
                self.build()
        self.assertFalse((self.root / "document-names").exists())


if __name__ == "__main__":
    unittest.main()
