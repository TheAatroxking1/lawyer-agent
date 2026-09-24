"""通过 Milvus REST v2 导入经过 prepare_attu_import 核验的完整 JSONL。

只连接本机已有 blog.lawyer_db；需要 --apply 才写入。新任务要求集合为空。
按256条分批 upsert，保存已确认位置；超时或重启只重放同一输入的未确认批次。
不会删除集合、清空数据、调用 embedding，BM25 由已有 Function 自动计算。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MODEL = "qwen3.7-text-embedding"
BASE = "http://localhost:19530/v2/vectordb/"
CONTEXT = {"dbName": "blog", "collectionName": "lawyer_db"}
LOCK_ROOT = Path(__file__).resolve().parents[1] / "artifacts/legal-corpus/milvus-import-locks"
FIELDS = (
    "vector",
    "model",
    "chunk_id",
    "text",
    "document_id",
    "chunk_type",
    "article_no",
    "parent_chunk_id",
)


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def save(path: Path, value: Any) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def api(route: str, body: dict[str, Any], timeout: int = 120) -> Any:
    payload = json.dumps(
        {**CONTEXT, **body}, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    require(len(payload) <= 32 * 1024 * 1024, "request_too_large")
    # 固定连接已核对的本机 Milvus，不接受来自输入文件的 URL。
    request = urllib.request.Request(  # noqa: S310
        BASE + route,
        data=payload,
        headers={"Content-Type": "application/json", "Request-Timeout": str(timeout)},
    )
    with urllib.request.urlopen(request, timeout=timeout + 5) as response:  # noqa: S310
        raw = response.read(32 * 1024 * 1024 + 1)
    require(len(raw) <= 32 * 1024 * 1024, "response_too_large")
    data = json.loads(raw)
    require(data.get("code") == 0, f"milvus_error_code:{data.get('code')}")
    return data.get("data")


def count_rows() -> int:
    rows = api(
        "entities/query", {"filter": "", "outputFields": ["count(*)"], "consistencyLevel": "Strong"}
    )
    return int(rows[0]["count(*)"])


def validate_report(report: dict[str, Any]) -> None:
    require(
        report.get("status") == "ready" and report.get("scope") == "full", "source_not_full_ready"
    )
    require(report.get("error_count") == 0, "source_has_errors")
    require(report.get("model") == MODEL and report.get("dimensions") == 1024, "model_mismatch")
    require(
        report.get("expected_documents", 0) > 0
        and report.get("validated_documents") == report["expected_documents"],
        "source_document_count_mismatch",
    )
    require(
        report.get("exported_rows", 0) > 0
        and report.get("validated_rows") == report["exported_rows"],
        "source_row_count_mismatch",
    )
    batches = report.get("batches", [])
    require(
        bool(batches) and sum(b["rows"] for b in batches) == report["exported_rows"],
        "batch_count_mismatch",
    )
    require(len({b["file"] for b in batches}) == len(batches), "duplicate_batch_file")


def validate_schema(schema: dict[str, Any]) -> None:
    require(
        schema["autoId"] is False and schema["enableDynamicField"] is False,
        "unexpected_collection_mode",
    )
    fields = {field["name"]: field for field in schema["fields"]}
    require(set(fields) == set(FIELDS) | {"sparse_vector"}, "schema_fields_mismatch")
    require(fields["chunk_id"].get("primaryKey") is True, "primary_key_mismatch")
    require(fields["vector"]["type"] == "FloatVector", "vector_type_mismatch")
    params = {p["key"]: p["value"] for p in fields["vector"]["params"]}
    require(params.get("dim") == "1024", "vector_dimension_mismatch")
    for name in FIELDS:
        if name == "vector":
            continue
        field = fields[name]
        require(field["type"] == "VarChar", "scalar_type_mismatch:" + name)
        options = {p["key"]: p["value"] for p in field["params"]}
        require(
            int(options.get("max_length", 0)) >= (65535 if name == "text" else 256),
            "varchar_capacity_mismatch:" + name,
        )
    require(
        all(fields[name].get("nullable") for name in ("article_no", "parent_chunk_id")),
        "nullable_fields_mismatch",
    )
    require(fields["sparse_vector"].get("isFunctionOutput") is True, "bm25_output_missing")
    require(
        any(
            f["inputFieldNames"] == ["text"]
            and f["outputFieldNames"] == ["sparse_vector"]
            and f["type"] == 1
            for f in schema["functions"]
        ),
        "bm25_function_mismatch",
    )


def read_batch(folder: Path, proof: dict[str, Any]) -> list[dict[str, Any]]:
    require(
        re.fullmatch(r"import_[0-9]{5}\.jsonl", proof["file"]) is not None, "invalid_batch_name"
    )
    p = folder / proof["file"]
    require(not p.is_symlink() and p.is_file(), "batch_file_missing")
    require(p.stat().st_size == proof["bytes"] <= 100_000_000, "batch_file_size_mismatch")
    raw = p.read_bytes()
    require(len(raw) == proof["bytes"] and sha(raw) == proof["sha256"], "batch_hash_mismatch")
    rows = [json.loads(line) for line in raw.splitlines()]
    require(len(rows) == proof["rows"], "batch_row_count_mismatch")
    return rows


def validate_ack(result: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    require(result.get("upsertCount") == len(rows), "upsert_count_mismatch")
    ids = result.get("upsertIds", result.get("insertIds", []))
    require(
        len(ids) == len(rows) and set(ids) == {r["chunk_id"] for r in rows}, "upsert_ids_mismatch"
    )


def validate_readback(source: dict[str, Any], actual: dict[str, Any]) -> None:
    for key, value in source.items():
        if key == "vector":
            vector = actual.get(key, [])
            require(len(vector) == 1024, "readback_dimension_mismatch")
            require(
                all(
                    math.isclose(a, b, rel_tol=2e-6, abs_tol=1e-8)
                    for a, b in zip(value, vector, strict=True)
                ),
                "readback_vector_mismatch",
            )
        else:
            require(actual.get(key) == value, "readback_field_mismatch:" + key)


def verify_samples(samples: list[dict[str, Any]]) -> None:
    ids = [r["chunk_id"] for r in samples]
    actual = api(
        "entities/query",
        {
            "filter": "chunk_id in " + json.dumps(ids),
            "outputFields": list(FIELDS),
            "limit": len(ids),
            "consistencyLevel": "Strong",
        },
    )
    by_id = {r["chunk_id"]: r for r in actual}
    require(set(by_id) == set(ids), "readback_missing_ids")
    for row in samples:
        validate_readback(row, by_id[row["chunk_id"]])


def validate_existing(expected: list[dict[str, Any]], actual: list[dict[str, Any]]) -> None:
    by_id = {row["chunk_id"]: row for row in expected}
    require(len({row["chunk_id"] for row in actual}) == len(actual), "duplicate_existing_id")
    for row in actual:
        require(row["chunk_id"] in by_id, "unowned_existing_id")
        validate_readback(by_id[row["chunk_id"]], row)


def validate_resume_count(state: dict[str, Any], current: int, pending_existing: int) -> None:
    pending = state.get("pending")
    require(pending_existing == 0 or pending is not None, "unexpected_rows_without_pending")
    if pending:
        require(pending_existing <= pending["count"], "pending_count_overflow")
    require(current == state["acknowledged_rows"] + pending_existing, "unexpected_target_row_count")


def existing_rows(group: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(
        api(
            "entities/query",
            {
                "filter": "chunk_id in " + json.dumps([row["chunk_id"] for row in group]),
                "outputFields": list(FIELDS),
                "limit": len(group),
                "consistencyLevel": "Strong",
            },
        )
    )


def pending_identity(
    file_index: int, row_offset: int, group: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "file_index": file_index,
        "row_offset": row_offset,
        "count": len(group),
        "ids_sha256": sha(json.dumps([row["chunk_id"] for row in group]).encode()),
    }


def lock_path() -> Path:
    return LOCK_ROOT / (sha((BASE + json.dumps(CONTEXT, sort_keys=True)).encode()) + ".lock")


def run(report_path: Path, apply: bool, max_requests: int | None) -> dict[str, Any]:
    raw = report_path.read_bytes()
    report = json.loads(raw)
    validate_report(report)
    folder = report_path.parent / "import"
    require(folder.is_dir(), "import_directory_missing")
    schema = api("collections/describe", {})
    validate_schema(schema)
    state_dir = report_path.parent / "milvus-blog-lawyer_db"
    if not apply:
        return {
            "status": "preflight",
            "source_rows": report["exported_rows"],
            "current_rows": count_rows(),
            "collection_id": schema["collectionID"],
        }
    state_dir.mkdir(exist_ok=True)
    # 同一集合的不同输入报告也必须共用锁，不能同时通过空集合检查。
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    with lock_path().open("a+b") as lock:
        import msvcrt

        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        state_file = state_dir / "state.json"
        binding = {
            "source_report_sha256": sha(raw),
            "collection_id": schema["collectionID"],
            "target": CONTEXT,
            "endpoint": BASE,
            "request_rows": 256,
        }
        state: dict[str, Any]
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            require(state["binding"] == binding, "resume_identity_mismatch")
            if state.get("pending"):
                pending = state["pending"]
                require(
                    pending["file_index"] == state["file_index"]
                    and pending["row_offset"] == state["row_offset"],
                    "pending_cursor_mismatch",
                )
                pending_rows = read_batch(folder, report["batches"][state["file_index"]])
                group = pending_rows[state["row_offset"] : state["row_offset"] + 256]
                require(
                    pending == pending_identity(state["file_index"], state["row_offset"], group),
                    "pending_identity_mismatch",
                )
                existing = existing_rows(group)
                validate_existing(group, existing)
                validate_resume_count(state, count_rows(), len(existing))
                del pending_rows, group, existing
            else:
                validate_resume_count(state, count_rows(), 0)
        else:
            require(count_rows() == 0, "new_import_requires_empty_collection")
            state = {
                "binding": binding,
                "status": "created",
                "file_index": 0,
                "row_offset": 0,
                "acknowledged_rows": 0,
                "verified_files": 0,
                "requests": 0,
                "pending": None,
                "started_at": datetime.now(UTC).isoformat(),
            }
            save(state_file, state)
            save(state_dir / "schema-before.json", schema)
        state["status"] = "running"
        save(state_file, state)
        requests_this_run = 0
        try:
            for file_index in range(state["file_index"], len(report["batches"])):
                require(
                    api("collections/describe", {})["collectionID"] == binding["collection_id"],
                    "collection_replaced_during_import",
                )
                proof = report["batches"][file_index]
                rows = read_batch(folder, proof)
                for row_offset in range(state["row_offset"], len(rows), 256):
                    group = rows[row_offset : row_offset + 256]
                    identity = pending_identity(file_index, row_offset, group)
                    if state.get("pending") is None:
                        require(not existing_rows(group), "unowned_existing_rows_before_write")
                        validate_resume_count(state, count_rows(), 0)
                        state["pending"] = identity
                        save(state_file, state)
                    else:
                        require(state["pending"] == identity, "pending_identity_mismatch")
                    # 每次只重放与清单摘要一致的当前批次，避免不确定响应导致重复insert。
                    for attempt in range(3):
                        try:
                            # 未确认写入只允许与当前输入一致的记录，不能覆盖外部新增内容。
                            existing = existing_rows(group)
                            validate_existing(group, existing)
                            validate_resume_count(state, count_rows(), len(existing))
                            result = api("entities/upsert", {"data": group})
                            break
                        except urllib.error.HTTPError as exc:
                            if exc.code not in (429, 502, 503, 504) or attempt == 2:
                                raise
                            time.sleep(2**attempt)
                        except (urllib.error.URLError, TimeoutError):
                            if attempt == 2:
                                raise
                            time.sleep(2**attempt)
                    validate_ack(result, group)
                    state.update(
                        row_offset=row_offset + len(group),
                        file_index=file_index,
                        acknowledged_rows=state["acknowledged_rows"] + len(group),
                        requests=state["requests"] + 1,
                        pending=None,
                        updated_at=datetime.now(UTC).isoformat(),
                    )
                    save(state_file, state)
                    requests_this_run += 1
                    if requests_this_run == 1:
                        verify_samples([group[0], group[-1]] if len(group) > 1 else group)
                    if max_requests and requests_this_run >= max_requests:
                        state["status"] = "paused"
                        save(state_file, state)
                        return state
                verify_samples([rows[0], rows[-1]] if len(rows) > 1 else rows)
                state.update(
                    file_index=file_index + 1,
                    row_offset=0,
                    verified_files=state["verified_files"] + 1,
                )
                save(state_file, state)
                print(
                    f"文件 {file_index + 1}/{len(report['batches'])}；已确认 "
                    f"{state['acknowledged_rows']}/{report['exported_rows']} 条；首尾回读通过。",
                    flush=True,
                )
            require(
                count_rows() == state["acknowledged_rows"] == report["exported_rows"],
                "final_row_count_mismatch",
            )
            require(
                sha(report_path.read_bytes()) == binding["source_report_sha256"],
                "source_report_changed",
            )
            state.update(
                status="data_verified",
                finished_at=datetime.now(UTC).isoformat(),
                final_count=count_rows(),
            )
            save(state_file, state)
        except (Exception, KeyboardInterrupt) as exc:
            state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            state["last_error"] = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            save(state_file, state)
            print(f"导入暂停：{state['last_error']}；检查 {state_file}", flush=True)
            raise
        return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-requests", type=int, help="仅用于小批验证，之后同一命令去除此项接续")
    args = parser.parse_args()
    print(json.dumps(run(args.report.resolve(), args.apply, args.max_requests), ensure_ascii=False))


if __name__ == "__main__":
    main()
