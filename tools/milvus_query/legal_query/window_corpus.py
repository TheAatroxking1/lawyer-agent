"""已验收窗口语料的独立Milvus集合；只写本版本集合，逐批核验续传。"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import struct
import time
from itertools import zip_longest
from pathlib import Path
from typing import Any

from pymilvus import DataType, Function, FunctionType

from legal_query.config import MODEL, QueryError, require, validate_vector

COLLECTION = "lawyer_windows_v2_20260920"
VERSION = "window-milvus-v1"
REGIONS = (
    "北京",
    "天津",
    "河北",
    "山西",
    "内蒙古",
    "辽宁",
    "吉林",
    "黑龙江",
    "上海",
    "江苏",
    "浙江",
    "安徽",
    "福建",
    "江西",
    "山东",
    "河南",
    "湖北",
    "湖南",
    "广东",
    "广西",
    "海南",
    "重庆",
    "四川",
    "贵州",
    "云南",
    "西藏",
    "陕西",
    "甘肃",
    "青海",
    "宁夏",
    "新疆",
)
NATIONAL = ("法律", "行政法规", "司法解释", "宪法", "监察法规")
STRINGS = {
    "chunk_id": 64,
    "document_id": 64,
    "source_id": 24,
    "document_name": 2048,
    "text": 8192,
    "embedding_text": 16384,
    "model": 128,
    "source_relative_path": 4096,
    "source_sha256": 64,
    "fulltext_sha256": 64,
    "embedding_text_sha256": 64,
    "release_sha256": 64,
    "scope": 32,
    "region": 128,
    "row_sha256": 64,
}
INTS = ("chunk_index", "start_char", "end_char")
FIELDS = [*STRINGS, *INTS, "needs_review", "quality"]


def packed(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(
        value.encode("utf-8") if isinstance(value, str) else packed(value)
    ).hexdigest()


def sha(path: Path) -> str:
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("wb") as f:
        f.write(packed(value))
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


def floats(vector: list[float]) -> bytes:
    return struct.pack("<1024f", *vector)


def row_digest(row: dict) -> str:
    return digest(
        [
            {k: v for k, v in row.items() if k not in ("vector", "row_sha256")},
            hashlib.sha256(floats(row["vector"])).hexdigest(),
        ]
    )


def build_row(chunk: dict, vector: dict, entry: dict, release: str) -> dict:
    text = chunk["text"]
    embedding = chunk["embedding_text"]
    require(
        bool(text.strip()) and embedding == f"法律名称：{entry['title']}\n正文：{text}",
        "window_embedding_text_mismatch",
    )
    require(chunk["document_name"] == entry["title"], "window_title_mismatch")
    require(
        chunk["chunk_id"] == vector["chunk_id"]
        and chunk["embedding_text_sha256"]
        == vector["embedding_text_sha256"]
        == digest(embedding),
        "window_vector_identity_mismatch",
    )
    require(
        chunk["end_char"] - chunk["start_char"] == len(text), "window_span_mismatch"
    )
    parts = entry["source_relative_path"].replace("\\", "/").split("/")
    scope = (
        "national"
        if parts[0] in NATIONAL
        else "local"
        if parts[0] == "地方法规"
        else "unknown"
    )
    region = parts[1] if scope == "local" and len(parts) > 2 else ""
    q = entry["quality"]
    row = {
        **{
            k: chunk[k]
            for k in [
                "chunk_id",
                "document_id",
                "document_name",
                "text",
                "embedding_text",
                "fulltext_sha256",
                "embedding_text_sha256",
                *INTS,
            ]
        },
        "source_id": entry["id"],
        "source_relative_path": entry["source_relative_path"],
        "source_sha256": entry["source_sha256"],
        "model": MODEL,
        "release_sha256": release,
        "scope": scope,
        "region": region,
        "needs_review": entry["needs_review"],
        "quality": {
            "flags": entry["old_quality_flags"],
            "pictures": q["pictures"],
            "coverage_warnings": q["source_xml_paragraphs_not_verbatim_contained"],
            "legal_validity": "unknown",
        },
        "vector": validate_vector(vector["vector"]),
    }
    row["row_sha256"] = row_digest(row)
    for name, size in STRINGS.items():
        require(
            isinstance(row[name], str) and len(row[name].encode("utf-8")) <= size,
            "window_field_too_long",
        )
    return row


class Release:
    def __init__(self, report: Path):
        self.path = report.resolve()
        self.sha256 = sha(self.path)
        self.report = load(self.path)
        r = self.report
        require(
            r["status"] == "passed"
            and not r["errors"]
            and r["verified_documents"] == r["expected_documents"] == len(r["rows"]),
            "release_not_verified",
        )
        require(
            len({x["id"] for x in r["rows"]}) == len(r["rows"]),
            "release_duplicate_sources",
        )
        require(
            sum(x["chunks"] for x in r["rows"]) == r["chunks"], "release_counts_invalid"
        )

    def freeze(self, state_root: Path):
        snapshot = {
            "release_sha256": self.sha256,
            "completions": {
                entry["id"]: sha(Path(entry["directory"]) / "completed.json")
                for entry in self.report["rows"]
            },
        }
        path = state_root / "source-artifacts.json"
        if path.exists():
            require(load(path) == snapshot, "release_snapshot_changed")
        else:
            state_root.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as f:
                f.write(packed(snapshot))
                f.flush()
                os.fsync(f.fileno())
        self.completions = snapshot["completions"]
        return sha(path)

    def rows(self):
        seen = set()
        count = 0
        configs = {}
        for entry in self.report["rows"]:
            folder = Path(entry["directory"])
            if hasattr(self, "completions"):
                require(
                    sha(folder / "completed.json") == self.completions[entry["id"]],
                    "release_snapshot_changed",
                )
            root = folder.parent.parent
            if root not in configs:
                config = load(root / "config.json")
                require(
                    config["model"]["model"] == MODEL
                    and config["model"]["dimensions"] == 1024
                    and config["size"] == 1000
                    and config["overlap"] == 200
                    and config["embedding_template"]
                    == "法律名称：{document_name}\n正文：{text}",
                    "release_model_mismatch",
                )
                require(
                    sha(root / "sources.jsonl") == config["sources_sha256"]
                    and sha(root / "runner.py") == config["script_sha256"],
                    "release_config_changed",
                )
                configs[root] = config
            done = load(folder / "completed.json")
            data = done["data"]
            require(digest(data) == done["data_sha256"], "completion_hash_invalid")
            for name, h in data["output_hashes"].items():
                require(
                    Path(name).name == name and sha(folder / name) == h,
                    "release_artifact_changed",
                )
            meta = load(folder / "document.json")
            require(
                done["identity"] == digest([configs[root], meta["source"]])
                and data["status"] == "completed"
                and data["document_id"] == meta["document_id"],
                "release_completion_identity_invalid",
            )
            require(
                meta["source"]["id"] == entry["id"]
                and meta["source"]["source_sha256"] == entry["source_sha256"],
                "release_source_mismatch",
            )
            text = (folder / "fulltext.txt").read_text(encoding="utf-8")
            require(digest(text) == meta["fulltext_sha256"], "fulltext_hash_changed")
            cursor = 0
            doc_count = 0
            with (
                (folder / "chunks.jsonl").open(encoding="utf-8") as chunks,
                (folder / "vectors.jsonl").open(encoding="utf-8") as vectors,
            ):
                for c, v in zip_longest(chunks, vectors):
                    require(
                        c is not None and v is not None, "release_vector_count_mismatch"
                    )
                    chunk = json.loads(c)
                    vector = json.loads(v)
                    start, end = chunk["start_char"], chunk["end_char"]
                    require(
                        chunk["text"] == text[start:end]
                        and chunk["fulltext_sha256"] == meta["fulltext_sha256"]
                        and chunk["document_id"] == meta["document_id"]
                        and chunk["chunk_index"] == doc_count
                        and 0 <= start < end <= len(text)
                        and end - start <= 1000,
                        "release_slice_changed",
                    )
                    require(
                        start == 0 if doc_count == 0 else cursor - start == 200,
                        "release_window_gap",
                    )
                    require(chunk["chunk_id"] not in seen, "release_duplicate_chunks")
                    seen.add(chunk["chunk_id"])
                    cursor = end
                    doc_count += 1
                    count += 1
                    yield build_row(chunk, vector, entry, self.sha256)
            require(
                cursor == len(text) and doc_count == entry["chunks"],
                "release_document_incomplete",
            )
        require(count == self.report["chunks"], "release_incomplete")

    def batches(self, size=128):
        batch = []
        for row in self.rows():
            batch.append(row)
            if len(batch) == size:
                yield batch
                batch = []
        if batch:
            yield batch


def ensure_collection(client, release: Release):
    description = VERSION + ":" + release.sha256
    if client.has_collection(COLLECTION):
        info = client.describe_collection(COLLECTION)
        require(info["description"] == description, "refuse_unrelated_collection")
        fields = {f["name"]: f for f in info["fields"]}
        require(
            set(FIELDS) | {"vector", "sparse_vector"} == set(fields),
            "window_schema_mismatch",
        )
        require(
            int(fields["vector"]["params"]["dim"]) == 1024
            and fields["chunk_id"].get("is_primary"),
            "window_schema_mismatch",
        )
        return
    schema = client.create_schema(
        auto_id=False, enable_dynamic_field=False, description=description
    )
    for name, size in STRINGS.items():
        extra = (
            {"enable_analyzer": True, "analyzer_params": {"type": "chinese"}}
            if name == "embedding_text"
            else {}
        )
        schema.add_field(
            name,
            DataType.VARCHAR,
            max_length=size,
            is_primary=name == "chunk_id",
            **extra,
        )
    for name in INTS:
        schema.add_field(name, DataType.INT64)
    schema.add_field("needs_review", DataType.BOOL)
    schema.add_field("quality", DataType.JSON)
    schema.add_field("vector", DataType.FLOAT_VECTOR, dim=1024)
    schema.add_field("sparse_vector", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(
        Function(
            name="window_bm25",
            input_field_names=["embedding_text"],
            output_field_names=["sparse_vector"],
            function_type=FunctionType.BM25,
        )
    )
    client.create_collection(
        COLLECTION, schema=schema, consistency_level="Strong", timeout=60
    )


def indexes(client):
    present = set(client.list_indexes(COLLECTION))
    spec = client.prepare_index_params()
    if "dense" not in present:
        spec.add_index(
            field_name="vector",
            index_name="dense",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 128},
        )
    if "sparse" not in present:
        spec.add_index(
            field_name="sparse_vector",
            index_name="sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="BM25",
            params={"inverted_index_algo": "DAAT_MAXSCORE"},
        )
    if not {"dense", "sparse"} <= present:
        client.create_index(COLLECTION, index_params=spec, timeout=600)
    client.load_collection(COLLECTION, timeout=300)


def validate_ready(client, path: Path, *, target=None):
    receipt = load(path)
    require(
        receipt["status"] == "ready" and receipt["collection"] == COLLECTION,
        "window_index_not_ready",
    )
    require(
        receipt["identity"]["target"]
        == (target or {"uri": "http://localhost:19530", "database": "blog"}),
        "window_target_changed",
    )
    info = client.describe_collection(COLLECTION)
    require(
        info["description"] == VERSION + ":" + receipt["release_sha256"]
        and info["collection_id"] == receipt["collection_id"],
        "window_collection_changed",
    )
    return receipt


def verify_rows(client, rows):
    actual = client.query(
        COLLECTION,
        filter="chunk_id in " + json.dumps([r["chunk_id"] for r in rows]),
        output_fields=[*FIELDS, "vector"],
        limit=len(rows),
        consistency_level="Strong",
        timeout=60,
    )
    actual = {r["chunk_id"]: r for r in actual}
    require(set(actual) == {r["chunk_id"] for r in rows}, "milvus_missing_rows")
    for row in rows:
        found = actual[row["chunk_id"]]
        require(all(found[k] == row[k] for k in FIELDS), "milvus_metadata_mismatch")
        require(
            floats(found["vector"]) == floats(row["vector"]), "milvus_vector_mismatch"
        )


def import_release(client, release: Release, state_root: Path, *, target=None):
    import msvcrt

    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / "import.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise QueryError("another_window_import_running") from None
        identity = {
            "source_snapshot_sha256": release.freeze(state_root),
            "target": target or {"uri": "http://localhost:19530", "database": "blog"},
            "release_sha256": release.sha256,
            "collection": COLLECTION,
            "code_sha256": sha(Path(__file__)),
            "pymilvus": importlib.metadata.version("pymilvus"),
            "batch_size": 128,
        }
        state_path = state_root / "progress.json"
        state = (
            load(state_path)
            if state_path.exists()
            else {"identity": identity, "batches": []}
        )
        require(state["identity"] == identity, "import_checkpoint_mismatch")
        # 重新验证期间不能继续把上一次回执当作当前可用证明。
        ready_path = state_root / "ready.json"
        if ready_path.exists():
            ready_path.replace(state_root / "ready.previous.json")
        ensure_collection(client, release)
        collection_id = client.describe_collection(COLLECTION)["collection_id"]
        require(
            "collection_id" not in state or state["collection_id"] == collection_id,
            "import_collection_changed",
        )
        state["collection_id"] = collection_id
        save(state_path, state)
        for i, rows in enumerate(release.batches()):
            batch_hash = digest([r["row_sha256"] for r in rows])
            if i < len(state["batches"]):
                require(state["batches"][i] == batch_hash, "import_batch_changed")
                continue
            result = client.upsert(COLLECTION, data=rows, timeout=120)
            require(result["upsert_count"] == len(rows), "milvus_upsert_incomplete")
            state["batches"].append(batch_hash)
            state.update(
                {
                    "rows": min((i + 1) * 128, release.report["chunks"]),
                    "status": "importing",
                }
            )
            save(state_path, state)
            if i % 25 == 0:
                print(
                    json.dumps(
                        {
                            "stage": "import",
                            "rows": state["rows"],
                            "total": release.report["chunks"],
                        }
                    ),
                    flush=True,
                )
        client.flush(COLLECTION, timeout=300)
        print(json.dumps({"stage": "index_build"}), flush=True)
        indexes(client)
        state["status"] = "verifying"
        save(state_path, state)
        count = 0
        for rows in release.batches():
            verify_rows(client, rows)
            count += len(rows)
            if count % 3200 == 0:
                print(json.dumps({"stage": "verify", "rows": count}), flush=True)
        actual = client.query(
            COLLECTION,
            filter="",
            output_fields=["count(*)"],
            consistency_level="Strong",
            timeout=60,
        )[0]["count(*)"]
        require(actual == count == release.report["chunks"], "milvus_total_mismatch")
        receipt = {
            "collection_id": collection_id,
            "status": "ready",
            "collection": COLLECTION,
            "release_sha256": release.sha256,
            "rows": count,
            "documents": len(release.report["rows"]),
            "model": MODEL,
            "dimensions": 1024,
            "verified_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_report": str(release.path),
            "identity": identity,
        }
        save(state_root / "ready.json", receipt)
        state["status"] = "ready"
        save(state_path, state)
        print(json.dumps(receipt, ensure_ascii=False), flush=True)
        return receipt
