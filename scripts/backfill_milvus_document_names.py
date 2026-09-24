"""给本机 blog.lawyer_db 增加名称列，按已校验映射局部更新。

从仓库根目录执行 python -X utf8 -m scripts.backfill_milvus_document_names。
默认只预检；--apply 才写入。--max-groups 可用于小批验证。
每组1024条，只提交主键与名称；全组回读通过才推进断点。
中断后同命令恢复，已一致的名称跳过；冲突、缺记录、来源不符均停止。
在证据目录创建 STOP 文件可在当前组完成后暂停，恢复前移除该文件。
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.build_document_name_map import DocumentNameMap
from scripts.import_attu_jsonl import (
    BASE,
    CONTEXT,
    FIELDS,
    LOCK_ROOT,
    api,
    count_rows,
    lock_path,
    read_batch,
    require,
    save,
    sha,
    validate_ack,
    validate_readback,
    validate_report,
    validate_schema,
)

DEFAULT_REPORT = Path(__file__).resolve().parents[1] / (
    "artifacts/legal-corpus/attu-import/20260914T142608Z-7c17c736/report.json"
)
NAME_FIELD = {
    "fieldName": "document_name",
    "dataType": "VarChar",
    "nullable": True,
    "elementTypeParams": {"max_length": 1024},
}
GROUP_SIZE = 1024


def make_updates(
    source: list[dict[str, Any]], actual: list[dict[str, Any]], names: dict[str, str]
) -> list[dict[str, Any]]:
    """只接受既有主键，防止局部upsert意外插入或覆盖人工修改。"""
    expected = {row["chunk_id"]: row for row in source}
    found = {row["chunk_id"]: row for row in actual}
    require(len(expected) == len(source), "duplicate_source_id")
    require(len(found) == len(actual) and set(found) == set(expected), "existing_ids_mismatch")
    result = []
    for key, row in expected.items():
        current = found[key]
        require(current["document_id"] == row["document_id"], "document_id_mismatch")
        name = names.get(row["document_id"])
        require(isinstance(name, str) and 0 < len(name.encode("utf-8")) <= 1024, "invalid_name")
        require(current.get("document_name") in (None, name), "conflicting_document_name")
        if current.get("document_name") != name:
            result.append({"chunk_id": key, "document_name": name})
    return result


def write_updates(rows: list[dict[str, Any]]) -> None:
    if rows:
        # partialUpdate不可省略，否则会变成整行替换，丢失未提交的字段。
        validate_ack(api("entities/upsert", {"partialUpdate": True, "data": rows}), rows)


def query(rows: list[dict[str, Any]], *, full: bool = False) -> list[dict[str, Any]]:
    return list(
        api(
            "entities/query",
            {
                "filter": "chunk_id in " + json.dumps([r["chunk_id"] for r in rows]),
                "outputFields": [*FIELDS, "document_name"]
                if full
                else ["chunk_id", "document_id", "document_name"],
                "limit": len(rows),
                "consistencyLevel": "Strong",
            },
        )
    )


def check_schema(schema: dict[str, Any]) -> bool:
    fields = schema["fields"]
    old = {**schema, "fields": [f for f in fields if f["name"] != "document_name"]}
    validate_schema(old)
    named = [f for f in fields if f["name"] == "document_name"]
    if not named:
        return False
    require(len(named) == 1, "duplicate_name_field")
    field = named[0]
    params = {p["key"]: p["value"] for p in field["params"]}
    require(field["type"] == "VarChar" and field.get("nullable") is True, "name_type_mismatch")
    require(int(params.get("max_length", 0)) >= 1024, "name_capacity_mismatch")
    return True


def run(report_path: Path, *, apply: bool, max_groups: int | None) -> dict[str, Any]:
    raw = report_path.read_bytes()
    report = json.loads(raw)
    validate_report(report)
    root = report_path.parent
    mapping = DocumentNameMap(root / "document-names")
    names = json.loads((root / "document-names/document_names.json").read_bytes())
    require(
        sha((root / "document-names/document_names.json").read_bytes())
        == mapping.report["map_sha256"],
        "map_changed",
    )
    imported = json.loads((root / "milvus-blog-lawyer_db/state.json").read_bytes())
    schema = api("collections/describe", {})
    has_field = check_schema(schema)
    require(imported["status"] == "data_verified", "import_not_verified")
    require(
        imported["binding"] == mapping.report["binding"]["import_binding"], "map_binding_mismatch"
    )
    require(imported["binding"]["source_report_sha256"] == sha(raw), "source_changed")
    require(imported["binding"]["collection_id"] == schema["collectionID"], "collection_changed")
    require(
        imported["binding"]["endpoint"] == BASE and imported["binding"]["target"] == CONTEXT,
        "target_changed",
    )
    require(count_rows() == report["exported_rows"], "target_count_mismatch")
    binding = {
        "source_sha256": sha(raw),
        "map_sha256": mapping.report["map_sha256"],
        "collection_id": schema["collectionID"],
        "group_size": GROUP_SIZE,
    }
    if not apply:
        return {"status": "preflight", "has_name_field": has_field, **binding}
    target = root / "milvus-blog-lawyer_db/document-name-backfill"
    target.mkdir(exist_ok=True)
    LOCK_ROOT.mkdir(parents=True, exist_ok=True)
    with lock_path().open("a+b") as lock:
        import msvcrt

        lock.seek(0)
        lock.write(b"0")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        state_path = target / "state.json"
        if state_path.exists():
            state: dict[str, Any] = json.loads(state_path.read_bytes())
            require(state["binding"] == binding, "resume_binding_mismatch")
        else:
            state = {
                "binding": binding,
                "file_index": 0,
                "offset": 0,
                "verified_rows": 0,
                "groups": 0,
                "status": "running",
            }
            save(target / "schema-before.json", schema)
            save(state_path, state)
        if not has_field:
            require(state["verified_rows"] == 0, "name_field_disappeared")
            api("collections/fields/add", {"schema": NAME_FIELD})
        schema = api("collections/describe", {})
        require(check_schema(schema), "name_field_missing")
        save(target / "schema-after.json", schema)
        groups_this_run = 0
        batches = report["batches"]
        for file_index in range(state["file_index"], len(batches)):
            rows = read_batch(root / "import", batches[file_index])
            offset = state["offset"] if file_index == state["file_index"] else 0
            while offset < len(rows):
                if (target / "STOP").exists() or (
                    max_groups is not None and groups_this_run >= max_groups
                ):
                    return {**state, "status": "paused"}
                group = rows[offset : offset + GROUP_SIZE]
                actual = query(group)
                updates = make_updates(group, actual, names)
                # 每组首尾均核对原文、ID、模型、条号、父块和1024维向量。
                samples = [group[0], group[-1]] if len(group) > 1 else group
                before = query(samples, full=True)
                before_by_id = {r["chunk_id"]: r for r in before}
                for sample in samples:
                    validate_readback(sample, before_by_id[sample["chunk_id"]])
                write_updates(updates)
                readback = query(group)
                require(not make_updates(group, readback, names), "name_readback_incomplete")
                after = query(samples, full=True)
                after_by_id = {r["chunk_id"]: r for r in after}
                for sample in samples:
                    validate_readback(sample, after_by_id[sample["chunk_id"]])
                offset += len(group)
                state.update(
                    file_index=file_index,
                    offset=offset,
                    verified_rows=state["verified_rows"] + len(group),
                    groups=state["groups"] + 1,
                    status="running",
                    updated_at=datetime.now(UTC).isoformat(),
                )
                save(state_path, state)
                if state["groups"] == 1:
                    save(
                        target / "smoke.json",
                        {"before": before, "after": after, "verified_rows": len(group)},
                    )
                groups_this_run += 1
            state.update(file_index=file_index + 1, offset=0)
            save(state_path, state)
            print(
                json.dumps(
                    {
                        "file": file_index + 1,
                        "files": len(batches),
                        "verified_rows": state["verified_rows"],
                    }
                ),
                flush=True,
            )
        require(state["verified_rows"] == report["exported_rows"], "verified_count_mismatch")
        require(count_rows() == report["exported_rows"], "final_count_mismatch")
        for expression in ("document_name is null", 'document_name == ""'):
            empty = api(
                "entities/query",
                {"filter": expression, "outputFields": ["count(*)"], "consistencyLevel": "Strong"},
            )
            require(empty[0]["count(*)"] == 0, "missing_names")
        api("collections/flush", {})
        state.update(status="verified", finished_at=datetime.now(UTC).isoformat())
        save(state_path, state)
        return state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-groups", type=int)
    args = parser.parse_args()
    require(args.max_groups is None or args.max_groups > 0, "invalid_max_groups")
    print(
        json.dumps(
            run(args.report, apply=args.apply, max_groups=args.max_groups),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
