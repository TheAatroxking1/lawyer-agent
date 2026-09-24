"""将百炼向量结果转换为 Attu 可导入的 JSONL（仅使用 Python 标准库）。

本脚本不调用模型、不连接数据库、不修改原文，不改变已有向量的含义。
默认检查完整清单；只有全部通过才生成 import 目录。先运行 --limit 3
检查几份样本；--check-only 只校验，不生成导入文件。详细用法见
docs/attu-import-preparation.md。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import uuid4

# 与用户已经创建的 Collection 对齐；不要在批量处理中随意改模型或字段。
MODEL = "qwen3.7-text-embedding"
DIMENSIONS = 1024
IMPORT_FIELDS = (
    "vector",
    "model",
    "chunk_id",
    "text",
    "document_id",
    "chunk_type",
    "article_no",
    "parent_chunk_id",
)
ROOT = Path(__file__).resolve().parents[1]
MAX_FLOAT32 = 3.4028234663852886e38
FORMAT_VERSION = "attu-legal-eight-fields-v1"


@dataclass(frozen=True)
class Config:
    input_dir: Path
    chunks_dir: Path
    output_root: Path
    batch_bytes: int = 50_000_000  # 50 MB，按实际 UTF-8 字节切分，不拆开一条记录。
    max_file_bytes: int = 64 * 1024 * 1024  # 限制单份 JSON；不会一次加载整个语料库。
    limit: int | None = None
    check_only: bool = False


class InvalidData(ValueError):
    """只携带稳定错误码/字段名，避免把正文或完整向量打印进日志。"""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise InvalidData(code)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_json(value: Any) -> bytes:
    # JSONL 必须一条记录一行；正文内的换行由 JSON 转义，正文内容不变。
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def reject_constant(_value: str) -> Any:
    raise InvalidData("non_finite_json_number")


def decode_json(raw: bytes) -> Any:
    try:
        return json.loads(
            raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs, parse_constant=reject_constant
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise InvalidData("invalid_json_or_utf8") from exc


def fingerprint(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


def is_link(path: Path) -> bool:
    # Windows junction 同样属于重定向入口。拒绝输入/输出中的这些入口以限制路径。
    return path.is_symlink() or path.is_junction()


def safe_path(path: Path) -> Path:
    for part in (path, *path.parents):
        require(not is_link(part), "symlink_or_junction_not_allowed")
    return path.resolve()


class Reader:
    """有界读取并登记快照；结束时确认本次处理期间输入没有被改写。"""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.observed: dict[Path, tuple[int, int, int, int]] = {}

    def read(self, path: Path) -> bytes:
        safe_path(path)
        require(path.is_file(), "missing_input_file")
        before = fingerprint(path)
        require(before[0] <= self.max_bytes, "input_file_too_large")
        with path.open("rb") as handle:
            raw = handle.read(self.max_bytes + 1)
        require(len(raw) <= self.max_bytes, "input_file_too_large")
        require(before == fingerprint(path), "input_changed_during_read")
        self.observed[path] = before
        return raw

    def verify_unchanged(self) -> None:
        for path, previous in self.observed.items():
            safe_path(path)
            require(path.is_file() and fingerprint(path) == previous, "input_changed_during_run")


def string_field(value: Any, name: str, maximum: int = 256, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    require(isinstance(value, str) and bool(value.strip()), f"invalid_string:{name}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError as exc:
        raise InvalidData(f"invalid_utf8:{name}") from exc
    require(size <= maximum, f"string_too_long:{name}")


def check_vector(vector: Any) -> None:
    require(isinstance(vector, list) and len(vector) == DIMENSIONS, "invalid_vector_dimension")
    # Milvus FloatVector 最终按 float32 存储；不接受 bool、NaN、Inf 或溢出数字。
    nonzero = False
    for value in vector:
        require(type(value) in (int, float), "invalid_vector_element_type")
        require(
            -MAX_FLOAT32 <= value <= MAX_FLOAT32 and math.isfinite(value), "invalid_vector_number"
        )
        nonzero = nonzero or abs(value) >= 2**-149
    require(nonzero, "zero_vector")


def validate_document(
    entry: dict[str, Any], data: Any, document: Any, chunk_bytes: bytes
) -> list[dict[str, Any]]:
    """核对现有向量确实对应这份离线切块，不用导入时间/文件名代替来源身份。"""
    require(isinstance(data, dict) and isinstance(document, dict), "invalid_document_object")
    require(data.get("model") == MODEL, "model_mismatch")
    require(
        type(data.get("dimensions")) is int and data["dimensions"] == DIMENSIONS,
        "dimension_mismatch",
    )
    require(data.get("document") == document, "document_metadata_mismatch")
    for key in ("document_id", "source_sha256"):
        require(
            document.get(key) == entry.get(key) and isinstance(document.get(key), str),
            f"manifest_identity_mismatch:{key}",
        )
    require(document.get("id_namespace") == "offline-export-v1", "id_namespace_mismatch")
    actual_hash = sha256(chunk_bytes)
    require(data.get("chunks_sha256") == actual_hash, "chunks_hash_mismatch")
    hashes = document.get("output_hashes")
    require(
        isinstance(hashes, dict) and hashes.get("chunks.jsonl") == actual_hash,
        "document_chunks_hash_mismatch",
    )
    source_rows = [decode_json(line) for line in chunk_bytes.splitlines() if line.strip()]
    rows = data.get("rows")
    require(isinstance(rows, list) and len(rows) > 0, "empty_or_invalid_rows")
    require(
        type(document.get("chunk_count")) is int
        and len(rows) == len(source_rows) == document["chunk_count"],
        "chunk_count_mismatch",
    )
    string_field(document["document_id"], "document_id")
    ids: dict[str, dict[str, Any]] = {}
    for index, (row, original) in enumerate(zip(rows, source_rows, strict=True)):
        require(isinstance(row, dict) and isinstance(original, dict), f"invalid_row:{index}")
        # 允许保存额外 embedding_text 等审计字段，但原切块的每个字段必须保持原样。
        require(
            all(key in row and row[key] == value for key, value in original.items()),
            f"source_row_mismatch:{index}",
        )
        require(
            "document_id" not in row or row["document_id"] == document["document_id"],
            f"row_document_id_mismatch:{index}",
        )
        for name in ("chunk_id", "chunk_type"):
            string_field(row.get(name), name)
        string_field(row.get("text"), "text", 65535)
        for name in ("article_no", "parent_chunk_id"):
            string_field(row.get(name), name, nullable=True)
        require(
            row.get("content_sha256") == sha256(row["text"].encode("utf-8")),
            f"content_hash_mismatch:{index}",
        )
        require(row["chunk_id"] not in ids, f"duplicate_chunk_id_in_document:{index}")
        ids[row["chunk_id"]] = row
        check_vector(row.get("vector"))
    for index, row in enumerate(rows):
        parent = row.get("parent_chunk_id")
        if parent is not None:
            require(parent != row["chunk_id"] and parent in ids, f"parent_not_found:{index}")
            require(ids[parent].get("parent_chunk_id") is None, f"invalid_parent_chain:{index}")
    return cast(list[dict[str, Any]], rows)


class BatchWriter:
    """在不可导入的暂存目录中分批写文件，最多只保留当前一条序列化记录。"""

    def __init__(self, directory: Path, max_bytes: int) -> None:
        self.directory = directory
        self.max_bytes = max_bytes
        self.handle: BinaryIO | None = None
        self.size = 0
        self.count = 0
        self.digest = hashlib.sha256()
        self.batches: list[dict[str, Any]] = []
        directory.mkdir()

    def add(self, record: dict[str, Any]) -> None:
        raw = encode_json(record)
        require(len(raw) <= self.max_bytes, "record_exceeds_batch_size")
        if self.handle is not None and self.size + len(raw) > self.max_bytes:
            self.close()
        if self.handle is None:
            name = f"import_{len(self.batches) + 1:05d}.jsonl"
            self.handle = (self.directory / name).open("xb")
        self.handle.write(raw)
        self.digest.update(raw)
        self.size += len(raw)
        self.count += 1

    def close(self) -> None:
        if self.handle is None:
            return
        handle = self.handle
        self.handle = None
        name = Path(handle.name).name
        try:
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            # fsync 失败也必须关闭句柄；失败的批次不加入成功列表。
            handle.close()
        self.batches.append(
            {
                "file": name,
                "rows": self.count,
                "bytes": self.size,
                "sha256": self.digest.hexdigest(),
            }
        )
        self.size, self.count, self.digest = 0, 0, hashlib.sha256()

    def verify(self) -> None:
        # 实际逐行读回磁盘文件并核对数量、摘要和 Schema，而非只相信写入计数。
        for batch in self.batches:
            digest = hashlib.sha256()
            count = size = 0
            with (self.directory / batch["file"]).open("rb") as handle:
                for raw in handle:
                    record = decode_json(raw)
                    require(
                        isinstance(record, dict) and set(record) == set(IMPORT_FIELDS),
                        "output_schema_mismatch",
                    )
                    check_vector(record["vector"])
                    digest.update(raw)
                    count += 1
                    size += len(raw)
            require(
                (count, size, digest.hexdigest())
                == (batch["rows"], batch["bytes"], batch["sha256"]),
                "output_readback_failed",
            )


def read_inventory(config: Config, reader: Reader) -> tuple[list[dict[str, Any]], bytes]:
    raw = reader.read(config.chunks_dir / "manifest.jsonl")
    entries = [decode_json(line) for line in raw.splitlines() if line.strip()]
    require(0 < len(entries) <= 100_000, "invalid_manifest_size")
    folders: set[str] = set()
    documents: set[str] = set()
    for entry in entries:
        require(isinstance(entry, dict), "invalid_manifest_entry")
        require(entry.get("status") == "completed", "manifest_has_unfinished_source")
        folder = entry.get("output_directory")
        require(
            isinstance(folder, str) and re.fullmatch(r"[0-9a-f]{24}", folder) is not None,
            "invalid_source_directory",
        )
        require(folder not in folders, "duplicate_manifest_directory")
        doc_id = entry.get("document_id")
        require(
            isinstance(doc_id, str) and doc_id not in documents, "duplicate_or_invalid_document_id"
        )
        folders.add(folder)
        documents.add(doc_id)
    return entries, raw


def prepare(config: Config) -> dict[str, Any]:
    """主流程。返回报告；失败也写 report.json，终端据此返回非零退出码。"""
    require(config.batch_bytes > 0 and config.max_file_bytes > 0, "invalid_size_limit")
    require(config.limit is None or config.limit > 0, "invalid_sample_limit")
    source = safe_path(config.input_dir)
    chunks = safe_path(config.chunks_dir)
    output = safe_path(config.output_root)
    require(source.is_dir() and chunks.is_dir(), "input_directory_missing")
    # 不允许输出覆盖输入，也不允许把输入所在父目录当作输出根目录。
    for original in (source, chunks):
        require(
            not output.is_relative_to(original) and not original.is_relative_to(output),
            "output_overlaps_input",
        )
    # 外部原始法律资料目录始终只读，连转换结果也不能写入它。
    protected = Path("F:/ai律师数据库").resolve()
    require(not output.is_relative_to(protected), "output_is_readonly_source")
    output.mkdir(parents=True, exist_ok=True)
    run = output / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8])
    run.mkdir()  # 不覆盖旧运行；并发启动各有自己的目录。
    report: dict[str, Any] = {
        "format_version": FORMAT_VERSION,
        "status": "running",
        "scope": "sample" if config.limit else "full",
        "run_directory": str(run),
        "input_directory": str(source),
        "chunks_directory": str(chunks),
        "started_at": datetime.now(UTC).isoformat(),
        "script_sha256": sha256(Path(__file__).read_bytes()),
        "python_version": sys.version.split()[0],
        "model": MODEL,
        "dimensions": DIMENSIONS,
        "check_only": config.check_only,
        "batch_bytes": config.batch_bytes,
        "expected_documents": 0,
        "selected_documents": 0,
        "validated_documents": 0,
        "validated_rows": 0,
        "exported_rows": 0,
        "missing_files": 0,
        "unexpected_files": 0,
        "error_count": 0,
        "batches": [],
        "import_directory": None,
        "notice": "格式校验不等于法律质量验收或数据库发布；离线ID不冒充数据库ID。",
    }
    (run / "report.json").write_bytes(encode_json(report))
    errors = (run / "errors.jsonl").open("xb")
    sources_report = (run / "sources.jsonl").open("xb")
    provenance_dir = run / "provenance"
    provenance_dir.mkdir()
    docs_file = (provenance_dir / "documents.jsonl").open("xb")
    positions_file = (provenance_dir / "chunks.jsonl").open("xb")
    # 全局去重表放磁盘，避免为近百万条块建立巨大 Python set。
    database = sqlite3.connect(run / "validation.sqlite3")
    database.execute("CREATE TABLE seen (chunk_id TEXT PRIMARY KEY, source_file TEXT NOT NULL)")
    writer = (
        None
        if config.check_only
        else BatchWriter(run / "_staging_do_not_import", config.batch_bytes)
    )
    reader = Reader(config.max_file_bytes)
    types: Counter[str] = Counter()

    def error(code: str, filename: str | None = None) -> None:
        report["error_count"] += 1
        report["last_error"] = {"code": code, "file": filename}
        try:
            if errors.closed:
                with (run / "errors.jsonl").open("ab") as handle:
                    handle.write(encode_json(report["last_error"]))
            else:
                errors.write(encode_json(report["last_error"]))
                errors.flush()
        except OSError:
            # 磁盘故障可能同时影响错误日志；仍返回失败，不递归写日志。
            report["error_log_write_failed"] = True

    try:
        entries, manifest_bytes = read_inventory(config, reader)
        report["manifest_sha256"] = sha256(manifest_bytes)
        report["expected_documents"] = len(entries)
        expected = {entry["output_directory"] + ".json" for entry in entries}
        actual = {path.name for path in source.iterdir() if path.suffix.lower() == ".json"}
        missing, unexpected = expected - actual, actual - expected
        report["missing_files"], report["unexpected_files"] = len(missing), len(unexpected)
        # 样本模式也报告全集盘点，但只因本次选中的缺失文档阻断样本验证。
        if config.limit is None:
            for name in sorted(unexpected):
                error("unexpected_vector_file", name)
        report["inventory_missing_names"] = sorted(missing)
        report["inventory_unexpected_names"] = sorted(unexpected)
        selected = entries if config.limit is None else entries[: config.limit]
        report["selected_documents"] = len(selected)
        print(
            f"清单 {len(entries)} 份；向量文件 {len(actual)} 份；缺少 {len(missing)}；"
            f"额外 {len(unexpected)}；本次检查 {len(selected)} 份。",
            flush=True,
        )
        for number, entry in enumerate(selected, 1):
            folder = entry["output_directory"]
            name = folder + ".json"
            try:
                vector_bytes = reader.read(source / name)
                data = decode_json(vector_bytes)
                vector_hash = sha256(vector_bytes)
                del vector_bytes  # 解码后及时释放原始字节，减少每份文档的峰值内存。
                document = decode_json(reader.read(chunks / folder / "document.json"))
                rows = validate_document(
                    entry, data, document, reader.read(chunks / folder / "chunks.jsonl")
                )
                try:
                    with database:
                        database.executemany(
                            "INSERT INTO seen VALUES (?, ?)",
                            ((row["chunk_id"], name) for row in rows),
                        )
                except sqlite3.IntegrityError as exc:
                    raise InvalidData("duplicate_chunk_id_across_documents") from exc
                for row in rows:
                    if writer is not None:
                        record = {key: row.get(key) for key in IMPORT_FIELDS}
                        record.update(model=MODEL, document_id=document["document_id"])
                        writer.add(record)
                        positions_file.write(
                            encode_json(
                                {
                                    **{
                                        key: value
                                        for key, value in row.items()
                                        if key not in ("vector", "text")
                                    },
                                    "document_id": document["document_id"],
                                }
                            )
                        )
                    types[row["chunk_type"]] += 1
                if writer is not None:
                    docs_file.write(
                        encode_json(
                            {
                                "vector_file": name,
                                "vector_file_sha256": vector_hash,
                                "model": MODEL,
                                "dimensions": DIMENSIONS,
                                "chunks_sha256": data["chunks_sha256"],
                                "document": document,
                            }
                        )
                    )
                report["validated_documents"] += 1
                report["validated_rows"] += len(rows)
                sources_report.write(
                    encode_json(
                        {
                            "file": name,
                            "status": "validated",
                            "rows": len(rows),
                            "vector_file_sha256": vector_hash,
                        }
                    )
                )
                del data, rows, document
            except (InvalidData, OSError) as exc:
                code = (
                    str(exc)
                    if isinstance(exc, InvalidData)
                    else f"input_io_error:{type(exc).__name__}"
                )
                error(code, name)
                sources_report.write(encode_json({"file": name, "status": "failed", "code": code}))
            if number % 100 == 0 or number == len(selected):
                print(
                    f"已检查 {number}/{len(selected)} 份；通过 {report['validated_documents']}；"
                    f"有效块 {report['validated_rows']}；错误 {report['error_count']}。",
                    flush=True,
                )
        if writer is not None:
            writer.close()
            writer.verify()
            report["batches"] = writer.batches
        reader.verify_unchanged()
        require(
            actual == {path.name for path in source.iterdir() if path.suffix.lower() == ".json"},
            "vector_inventory_changed_during_run",
        )
        require(
            report["script_sha256"] == sha256(Path(__file__).read_bytes()),
            "script_changed_during_run",
        )
        report["status"] = (
            "failed"
            if report["error_count"]
            else (
                ("sample_" if config.limit else "") + ("checked" if config.check_only else "ready")
            )
        )
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        error("operator_interrupted")
    except (InvalidData, OSError, sqlite3.Error) as exc:
        report["status"] = "failed"
        error(str(exc) if isinstance(exc, InvalidData) else f"run_error:{type(exc).__name__}")
    finally:
        # 任何一个 close/flush 失败都不能跳过其余句柄的释放或把任务标为 ready。
        resources: list[Any] = [sources_report, docs_file, positions_file, database, errors]
        if writer is not None:
            resources.insert(0, writer)
        for resource in resources:
            try:
                resource.close()
            except (OSError, sqlite3.Error, KeyboardInterrupt) as exc:
                report["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
                error(f"resource_close_failed:{type(exc).__name__}")
        report["chunk_types"] = dict(types)
        report["finished_at"] = datetime.now(UTC).isoformat()

    def save_report() -> None:
        temp = run / "report.json.tmp"
        temp.write_bytes(encode_json(report))
        temp.replace(run / "report.json")

    # 先确认最终报告能够落盘，再以改名作为发布的最后一步。
    # 上传要求 report 为 ready/sample_ready 且 import 目录存在，两者缺一不可。
    publish = report["status"] in ("ready", "sample_ready") and writer is not None
    if publish:
        report["import_directory"] = str(run / "import")
        report["exported_rows"] = report["validated_rows"]
    phase = "final_report_write_failed"
    try:
        save_report()
        if publish and writer is not None:
            phase = "publish_failed"
            writer.directory.rename(run / "import")
    except (OSError, KeyboardInterrupt) as exc:
        report["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        report["import_directory"], report["exported_rows"] = None, 0
        error(phase)
        # 中断可能紧邻 rename 返回；若目录已移动，退回不可导入的暂存位置。
        if (run / "import").exists() and writer is not None:
            try:
                (run / "import").rename(writer.directory)
            except OSError:
                error("publish_rollback_failed_do_not_import")
        try:
            save_report()
        except (OSError, KeyboardInterrupt):
            report["report_write_failed"] = True
    print(f"结果：{report['status']}；报告：{run / 'report.json'}", flush=True)
    if report.get("report_write_failed"):
        print("最终报告写入失败；本次结果不可导入，磁盘上的旧报告不是完成证明。", flush=True)
    if report["import_directory"]:
        print(f"Attu 只上传此目录中的 JSONL：{report['import_directory']}", flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=ROOT / "artifacts/legal-corpus/api-embeddings/qwen3.7-1024",
        help="已有 API 向量文件目录（只读）",
    )
    parser.add_argument(
        "--chunks-dir",
        type=Path,
        default=ROOT / "artifacts/legal-corpus/chunks",
        help="切块目录，必须含完整 manifest.jsonl（只读）",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=ROOT / "artifacts/legal-corpus/attu-import",
        help="输出根目录；每次在其中新建独立运行目录",
    )
    parser.add_argument("--batch-mb", type=int, default=50, help="每个导入文件最大 MB，默认50")
    parser.add_argument("--max-file-mib", type=int, default=64, help="单份输入最大 MiB，默认64")
    parser.add_argument("--limit", type=int, help="只检查清单前N份，用于样本；不代表全集完成")
    parser.add_argument("--check-only", action="store_true", help="只做校验，不生成导入批次")
    args = parser.parse_args()
    try:
        report = prepare(
            Config(
                args.input_dir,
                args.chunks_dir,
                args.output_root,
                args.batch_mb * 1_000_000,
                args.max_file_mib * 1024 * 1024,
                args.limit,
                args.check_only,
            )
        )
    except (ValueError, OSError) as exc:
        message = str(exc) if isinstance(exc, InvalidData) else type(exc).__name__
        print(f"无法开始：{message}", file=sys.stderr)
        return 2
    return 130 if report["status"] == "interrupted" else (1 if report["status"] == "failed" else 0)


if __name__ == "__main__":
    raise SystemExit(main())
