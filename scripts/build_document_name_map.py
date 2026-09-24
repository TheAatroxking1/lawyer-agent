"""建立离线 document_id 到文书显示名称的映射；不连接数据库或调用模型。

名称取自原文件名，仅去掉最后的扩展名，保留日期和版本后缀。
build 读取已完成导入批次的报告、原切块清单和追溯文件；lookup/find 查询输出。
DocumentNameMap.enrich 可为本地检索结果补名称，不修改输入记录或现有向量。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any
from uuid import uuid4

MAX_BYTES = 64 * 1024 * 1024
READ_ONLY_SOURCE = Path("F:/ai律师数据库")
SOURCE_KEYS = ("id_namespace", "source_path", "source_relative_path", "source_sha256")


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def decode(raw: bytes) -> Any:
    return json.loads(raw, object_pairs_hook=unique_keys)


def read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    require(len(raw) <= MAX_BYTES, "input_too_large")
    return raw


def valid_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def write_json(path: Path, value: Any) -> bytes:
    raw = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    require(len(raw) <= MAX_BYTES, "output_too_large")
    with path.open("xb") as handle:
        handle.write(raw)
    return raw


class DocumentNameMap:
    """校验映射摘要后加载。一次加载、多次查询；名称匹配保留全部候选版本。"""

    def __init__(self, directory: Path) -> None:
        self.report = decode(read_bounded(directory / "report.json"))
        require(
            self.report.get("status") == "ready"
            and self.report.get("format_version") == "document-name-map-v1",
            "map_not_ready",
        )
        raw = read_bounded(directory / "document_names.json")
        require(digest(raw) == self.report["map_sha256"], "map_hash_mismatch")
        data = decode(raw)
        require(
            isinstance(data, dict) and len(data) == self.report["documents"], "map_count_mismatch"
        )
        require(0 < len(data) <= 100_000, "invalid_map_size")
        for key, name in data.items():
            require(valid_id(key), "invalid_document_id")
            require(isinstance(name, str) and bool(name.strip()), "invalid_document_name")
        self._names: dict[str, str] = data

    def lookup(self, document_id: str) -> str:
        # 缺失ID明确报错，不能用标题猜测或返回不相关文书。
        return self._names[document_id]

    def find(self, name: str) -> list[dict[str, str]]:
        require(bool(name.strip()), "empty_name_query")
        return [
            {"document_id": key, "document_name": value}
            for key, value in self._names.items()
            if name.strip() in value
        ]

    def enrich(self, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """保留原命中的所有字段，为rerank或回答上下文附加文书显示名称。"""
        output = []
        for hit in hits:
            name = self.lookup(hit["document_id"])
            require(hit.get("document_name") in (None, name), "document_name_conflict")
            output.append({**hit, "document_name": name})
        return output


def build(report_path: Path) -> Path:
    report_path = report_path.resolve()
    root = report_path.parent
    require(not root.is_relative_to(READ_ONLY_SOURCE.resolve()), "output_is_readonly_source")
    report_raw = read_bounded(report_path)
    report = decode(report_raw)
    require(
        report.get("status") == "ready"
        and report.get("scope") == "full"
        and report.get("error_count") == 0,
        "source_not_full_ready",
    )
    count = report["expected_documents"]
    require(type(count) is int and 0 < count <= 100_000, "invalid_document_count")
    require(report["validated_documents"] == count, "document_coverage_mismatch")
    require(report["validated_rows"] == report["exported_rows"] > 0, "chunk_count_mismatch")
    state = decode(read_bounded(root / "milvus-blog-lawyer_db/state.json"))
    require(
        state.get("status") == "data_verified"
        and state["acknowledged_rows"] == state["final_count"] == report["exported_rows"],
        "import_not_verified",
    )
    require(
        state["binding"]["source_report_sha256"] == digest(report_raw), "import_binding_mismatch"
    )

    manifest_path = Path(report["chunks_directory"]) / "manifest.jsonl"
    manifest_raw = read_bounded(manifest_path)
    require(digest(manifest_raw) == report["manifest_sha256"], "manifest_hash_mismatch")
    manifest = {}
    for line in manifest_raw.splitlines():
        row = decode(line)
        key = row["document_id"]
        require(valid_id(key), "invalid_document_id")
        require(key not in manifest, "duplicate_manifest_document_id")
        require(row.get("status") == "completed", "manifest_not_completed")
        manifest[key] = row
    require(len(manifest) == count, "manifest_coverage_mismatch")

    source = root / "provenance/documents.jsonl"
    before = source.stat()
    source_digest = hashlib.sha256()
    names: dict[str, str] = {}
    chunks = size = 0
    # 单行有界读取，不把包含全部文书元数据的JSONL加载进内存。
    with source.open("rb") as handle:
        while raw := handle.readline(1024 * 1024 + 1):
            size += len(raw)
            require(len(raw) <= 1024 * 1024 and size <= 2 * MAX_BYTES, "provenance_too_large")
            source_digest.update(raw)
            doc = decode(raw)["document"]
            key = doc["document_id"]
            require(key not in names, "duplicate_provenance_document_id")
            require(key in manifest, "unexpected_document_identity")
            require(doc.get("id_namespace") == "offline-export-v1", "invalid_id_namespace")
            require(
                all(doc.get(field) == manifest[key].get(field) for field in SOURCE_KEYS),
                "document_source_mismatch",
            )
            path = PureWindowsPath(doc["source_path"])
            relative = PureWindowsPath(doc["source_relative_path"])
            require(path.name == relative.name and bool(path.suffix), "invalid_source_filename")
            name = path.stem
            require(
                bool(name.strip()) and len(name.encode("utf-8")) <= 4096, "invalid_document_name"
            )
            names[key] = name
            require(
                type(doc["chunk_count"]) is int and doc["chunk_count"] > 0, "invalid_chunk_count"
            )
            chunks += doc["chunk_count"]
    after = source.stat()
    require(
        (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino)
        == (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino),
        "provenance_changed_during_build",
    )
    require(set(names) == set(manifest), "document_coverage_mismatch")
    require(chunks == report["exported_rows"], "chunk_count_mismatch")
    require(read_bounded(report_path) == report_raw, "report_changed_during_build")
    require(
        digest(read_bounded(manifest_path)) == report["manifest_sha256"],
        "manifest_changed_during_build",
    )
    binding = {
        "source_report_sha256": digest(report_raw),
        "source_manifest_sha256": report["manifest_sha256"],
        "source_provenance_sha256": source_digest.hexdigest(),
        "import_binding": state["binding"],
    }
    output = root / "document-names"
    if output.exists():
        existing = DocumentNameMap(output)
        require(existing.report["binding"] == binding, "existing_map_source_conflict")
        require(existing._names == names, "existing_map_content_conflict")
        return output

    # 全部输入通过后才写派生目录。先实际读回，再改名发布，不覆盖已有输出。
    stage = root / (".document-names-staging-" + uuid4().hex)
    stage.mkdir()
    raw = write_json(stage / "document_names.json", names)
    frequencies = Counter(names.values())
    write_json(
        stage / "report.json",
        {
            "format_version": "document-name-map-v1",
            "status": "ready",
            "created_at": datetime.now(UTC).isoformat(),
            "binding": binding,
            "name_source": "source_filename_stem",
            "documents": len(names),
            "source_chunk_count": chunks,
            "distinct_names": len(frequencies),
            "ambiguous_names": sum(value > 1 for value in frequencies.values()),
            "map_sha256": digest(raw),
            "map_bytes": len(raw),
            "max_name_utf8_bytes": max(len(name.encode("utf-8")) for name in names.values()),
        },
    )
    require(DocumentNameMap(stage)._names == names, "map_readback_mismatch")
    stage.rename(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("build", help="从已导入批次生成名称映射")
    create.add_argument("--report", type=Path, required=True)
    for action in ("lookup", "find"):
        query = commands.add_parser(action)
        query.add_argument("--directory", type=Path, required=True)
        query.add_argument("--id" if action == "lookup" else "--name", required=True)
    args = parser.parse_args()
    if args.command == "build":
        print(build(args.report))
    else:
        names = DocumentNameMap(args.directory)
        result = names.lookup(args.id) if args.command == "lookup" else names.find(args.name)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
