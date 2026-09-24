"""Versioned offline Docling/window/embedding batch; never publishes an index."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import io
import json
import math
import os
import re
import sys
import time
import uuid
from collections import Counter
from concurrent import futures
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import httpx

VERSION = "legal-char-window-bulk-v2"
SOURCE = Path("F:/ai律师数据库")
OLD = SOURCE / "切块第一版/chunks"
OUTPUT_BASE = Path("F:/律师Agent派生数据")
CONVERTER = None
CONTEXT = None
HTTP = None


def packed(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def digest(value):
    return hashlib.sha256(
        value.encode("utf-8") if isinstance(value, str) else packed(value)
    ).hexdigest()


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic_new(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part-" + uuid.uuid4().hex)
    try:
        with tmp.open("xb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.link(tmp, path)  # atomic creation; never overwrite an existing fact
    finally:
        tmp.unlink(missing_ok=True)


def store_cache(path, identity, data):
    atomic_new(
        path, packed({"identity": identity, "data_sha256": digest(data), "data": data})
    )


def read_cache(path, identity):
    raw = load(path)
    if raw["identity"] != identity or digest(raw["data"]) != raw["data_sha256"]:
        raise ValueError("cache_identity_or_digest_mismatch")
    return raw["data"]


def read_sources(root, config):
    if sha(root / "sources.jsonl") != config["sources_sha256"]:
        raise ValueError("source_manifest_digest_mismatch")
    entries = [
        json.loads(x)
        for x in (root / "sources.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    if len(entries) != config["documents"] or len({e["id"] for e in entries}) != len(
        entries
    ):
        raise ValueError("source_manifest_count_or_identity_mismatch")
    return entries


def ensure_metadata(folder, metadata):
    path = folder / "document.json"
    data = packed(metadata)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError("document_metadata_changed")
    else:
        atomic_new(path, data)


def attempt_event(folder, event):
    with (folder / "api-attempts.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def windows(document_id, title, text, size=1000, overlap=200):
    if size <= 0 or not 0 <= overlap < size or not text.strip() or not title.strip():
        raise ValueError("invalid_window_or_empty_document")
    rows = []
    start = 0
    fulltext_sha256 = digest(text)
    while start < len(text):
        end = min(start + size, len(text))
        body = text[start:end]
        if not body.strip():
            raise ValueError("empty_window")
        embedding_text = f"法律名称：{title}\n正文：{body}"
        rows.append(
            {
                "chunk_id": digest([VERSION, document_id, size, overlap, start, end]),
                "document_id": document_id,
                "document_name": title,
                "chunk_index": len(rows),
                "start_char": start,
                "end_char": end,
                "text": body,
                "embedding_text": embedding_text,
                "embedding_text_sha256": digest(embedding_text),
                "fulltext_sha256": fulltext_sha256,
            }
        )
        if end == len(text):
            break
        start += size - overlap
    return rows


def validate_vectors(payload, rows, dimensions):
    data = sorted(payload["data"], key=lambda x: x["index"])
    if [x["index"] for x in data] != list(range(len(rows))):
        raise ValueError("vector_indexes_invalid")
    result = []
    for row, item in zip(rows, data, strict=True):
        vector = item["embedding"]
        if (
            len(vector) != dimensions
            or not all(isinstance(x, (float, int)) and math.isfinite(x) for x in vector)
            or not any(vector)
        ):
            raise ValueError("vector_invalid")
        result.append(
            {
                "chunk_id": row["chunk_id"],
                "embedding_text_sha256": row["embedding_text_sha256"],
                "vector": vector,
            }
        )
    return result


def normalized(text):
    return re.sub(r"[\s\u200b\ufeff]", "", text)


def parse_document(path):
    global CONVERTER
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.document_converter import DocumentConverter
    from docling_core.types.doc import ContentLayer

    if CONVERTER is None:
        CONVERTER = DocumentConverter(allowed_formats=[InputFormat.DOCX])
    source = (
        DocumentStream(name=path.stem + ".docx", stream=io.BytesIO(path.read_bytes()))
        if path.suffix.lower() == ".docm"
        else path
    )
    result = CONVERTER.convert(source)
    if result.status.value != "success":
        raise ValueError("docling_conversion_not_success")
    doc = result.document
    # Official serializer includes table cells and list markers, unlike texts-only traversal.
    text = doc.export_to_text(
        included_content_layers={ContentLayer.BODY}, traverse_pictures=True
    )
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not text:
        raise ValueError("docling_empty_body")
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    with ZipFile(path) as archive:
        xml = ET.fromstring(archive.read("word/document.xml"))
        body = xml.find("w:body", ns)
        paragraphs = [
            "".join(x.text or "" for x in p.findall(".//w:t", ns))
            for p in body.findall(".//w:p", ns)
        ]
    source_pars = [normalized(x) for x in paragraphs if normalized(x)]
    target = normalized(text)
    missing = [i for i, x in enumerate(source_pars) if x not in target]
    quality = {
        "conversion_status": result.status.value,
        "source_xml_nonempty_paragraphs": len(source_pars),
        "source_xml_paragraphs_not_verbatim_contained": len(missing),
        "unmatched_source_paragraph_indexes": missing,
        "tables": len(doc.tables),
        "pictures": len(doc.pictures),
        "needs_review": bool(missing or doc.pictures),
        "scope": "Plain text extraction; no image OCR, visual/table fidelity or legal validity certification",
        "footer_limit": "Docling 2.129 may omit even-page footer parts; original remains authoritative",
    }
    return doc.export_to_dict(), text, quality


def provider_config(workspace):
    tree = ast.parse(
        (Path(workspace) / ".sdd/embedding/embedding.py").read_text(
            encoding="utf-8-sig"
        )
    )
    model = None
    dimensions = None
    kwargs = None
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Name) and target.id == "MODEL":
                    model = ast.literal_eval(n.value)
                if isinstance(target, ast.Name) and target.id == "DIMENSIONS":
                    dimensions = ast.literal_eval(n.value)
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "OpenAI"
    ]
    if len(calls) != 1:
        raise ValueError("provider_config_ambiguous")
    kwargs = {
        k.arg: ast.literal_eval(k.value)
        for k in calls[0].keywords
        if k.arg in ["api_key", "base_url"]
    }
    if (
        model != "qwen3.7-text-embedding"
        or dimensions != 1024
        or not kwargs["base_url"].startswith("https://")
    ):
        raise ValueError("provider_config_unexpected")
    return {
        "model": model,
        "dimensions": dimensions,
        "base_url": kwargs["base_url"],
    }, kwargs["api_key"]


def select_input(source, source_sha256, input_sha256, conversions):
    """Resolve the frozen input identity, regardless of the source extension."""
    if sha(source) != source_sha256:
        raise ValueError("source_hash_changed")
    if input_sha256 == source_sha256:
        return source, None
    matches = [
        row
        for row in conversions
        if row["source_sha256"] == source_sha256
        and Path(row["source_path"]).resolve() == source.resolve()
        and row["converted_sha256"] == input_sha256
    ]
    if len(matches) != 1:
        raise ValueError("exact_conversion_missing_or_ambiguous")
    conversion = matches[0]
    path = Path(conversion["converted_path"])
    if sha(path) != input_sha256:
        raise ValueError("conversion_hash_changed")
    return path, conversion


def prepare(root, workspace):
    root = root.resolve()
    workspace = workspace.resolve()
    if (
        not root.is_relative_to(OUTPUT_BASE.resolve())
        or root == OUTPUT_BASE.resolve()
        or root.is_relative_to(SOURCE.resolve())
    ):
        raise ValueError("unsafe_output_root")
    if root.exists():
        raise FileExistsError("run_root_already_exists")
    for p in [root, *root.parents]:
        if p.exists() and (p.is_symlink() or p.is_junction()):
            raise ValueError("redirected_output_path")
    root.mkdir(parents=True)
    conversions = {}
    with (workspace / "artifacts/legal-corpus/converted/manifest.jsonl").open(
        encoding="utf-8"
    ) as f:
        for line in f:
            r = json.loads(line)
            if r["status"] == "converted":
                conversions.setdefault(r["source_sha256"], []).append(r)
    entries = []
    with (OLD / "manifest.jsonl").open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            source = Path(r["source_path"]).resolve()
            if r["status"] != "completed" or not source.is_relative_to(
                (SOURCE / "法律法规数据库").resolve()
            ):
                raise ValueError("invalid_source_manifest")
            folder = r["output_directory"]
            if not re.fullmatch("[a-f0-9]{24}", folder):
                raise ValueError("invalid_source_folder")
            metadata = load(OLD / folder / "document.json")
            inp, conversion = select_input(
                source,
                r["source_sha256"],
                metadata["input_sha256"],
                conversions.get(r["source_sha256"], []),
            )
            title = re.sub(r"_[0-9]{8}$", "", source.stem)
            entries.append(
                {
                    "id": folder,
                    "source_path": str(source),
                    "input_path": str(inp),
                    "title": title,
                    "source_sha256": r["source_sha256"],
                    "input_sha256": metadata["input_sha256"],
                    "old_document_sha256": sha(OLD / folder / "document.json"),
                    "source_relative_path": r["source_relative_path"],
                    "old_quality_flags": r["quality_flags"],
                    "conversion_provenance": conversion,
                }
            )
    if len({e["id"] for e in entries}) != len(entries):
        raise ValueError("duplicate_source_identity")
    provider, _ = provider_config(workspace)
    entrybytes = b"".join(packed(e) + b"\n" for e in entries)
    atomic_new(root / "sources.jsonl", entrybytes)
    atomic_new(root / "runner.py", Path(__file__).read_bytes())
    packages = {
        d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
    }
    settings = {
        "version": VERSION,
        "workspace": str(workspace),
        "model": provider,
        "size": 1000,
        "overlap": 200,
        "python": sys.version,
        "packages": packages,
        "script_sha256": sha(root / "runner.py"),
        "sources_sha256": sha(root / "sources.jsonl"),
        "documents": len(entries),
        "original_manifest_sha256": sha(OLD / "manifest.jsonl"),
        "serializer": "Docling export_to_text BODY, traverse_pictures; strip blank lines, LF join",
        "embedding_template": "法律名称：{document_name}\n正文：{text}",
    }
    atomic_new(root / "config.json", packed(settings))
    print(
        json.dumps(
            {"prepared": str(root), "documents": len(entries)}, ensure_ascii=False
        ),
        flush=True,
    )


def init_worker(root):
    global CONTEXT, HTTP
    root = Path(root)
    config = load(root / "config.json")
    public, key = provider_config(config["workspace"])
    if public != config["model"]:
        raise ValueError("provider_changed_since_prepare")
    CONTEXT = (root, config)
    HTTP = httpx.Client(
        base_url=public["base_url"].rstrip("/") + "/",
        headers={"Authorization": "Bearer " + key},
        timeout=120,
    )


def process(entry):
    root, config = CONTEXT
    folder = root / "documents" / entry["id"]
    folder.mkdir(parents=True, exist_ok=True)
    identity = digest([config, entry])
    done = folder / "completed.json"
    try:
        if (root / "STOP").exists():
            return {"id": entry["id"], "status": "interrupted"}
        for key in ["source", "input"]:
            if sha(entry[key + "_path"]) != entry[key + "_sha256"]:
                raise ValueError("source_hash_changed")
        if done.exists():
            receipt = read_cache(done, identity)
            for name, h in receipt["output_hashes"].items():
                if sha(folder / name) != h:
                    raise ValueError("completed_output_changed")
            return receipt
        parsed = folder / "parsed.json"
        if parsed.exists():
            metadata = read_cache(parsed, identity)
            for name, h in metadata["output_hashes"].items():
                if sha(folder / name) != h:
                    raise ValueError("parsed_output_changed")
            text = (folder / "fulltext.txt").read_text(encoding="utf-8")
            rows = load(folder / "chunks.json")
        else:
            raw, text, quality = parse_document(Path(entry["input_path"]))
            document_id = digest(
                [
                    VERSION,
                    entry["id"],
                    entry["source_sha256"],
                    config["serializer"],
                    digest(text),
                ]
            )
            rows = windows(
                document_id, entry["title"], text, config["size"], config["overlap"]
            )
            contents = {
                "docling.json": packed(raw),
                "fulltext.txt": text.encode("utf-8"),
                "chunks.json": packed(rows),
                "chunks.jsonl": b"".join(packed(r) + b"\n" for r in rows),
            }
            for name, data in contents.items():
                path = folder / name
                if path.exists():
                    if path.read_bytes() != data:
                        raise ValueError("partial_output_changed")
                else:
                    atomic_new(path, data)
            metadata = {
                "document_id": document_id,
                "document_name": entry["title"],
                "source": entry,
                "fulltext_sha256": digest(text),
                "characters": len(text),
                "chunks": len(rows),
                "quality": quality,
                "legal_metadata_status": "unknown",
                "output_hashes": {n: sha(folder / n) for n in contents},
            }
            store_cache(parsed, identity, metadata)
        ensure_metadata(folder, metadata)
        tokens = 0
        unknown_usage = 0
        vector_rows = []
        for batch_index, start in enumerate(range(0, len(rows), 10)):
            if (root / "STOP").exists():
                return {"id": entry["id"], "status": "interrupted"}
            batch = rows[start : start + 10]
            request = {
                "model": config["model"]["model"],
                "input": [r["embedding_text"] for r in batch],
                "dimensions": 1024,
                "encoding_format": "float",
            }
            batchid = digest([identity, request])
            path = folder / "batches" / f"{batch_index:05d}.json"
            if path.exists():
                saved = read_cache(path, batchid)
            else:
                payload = None
                for attempt in range(4):
                    if (root / "STOP").exists():
                        return {"id": entry["id"], "status": "interrupted"}
                    attempt_id = uuid.uuid4().hex
                    attempt_event(
                        folder,
                        {
                            "event": "sent",
                            "id": attempt_id,
                            "request_sha256": digest(request),
                            "batch": batch_index,
                            "time": time.time(),
                        },
                    )
                    response = HTTP.post("embeddings", json=request)
                    attempt_event(
                        folder,
                        {
                            "event": "http_response",
                            "id": attempt_id,
                            "status": response.status_code,
                            "time": time.time(),
                        },
                    )
                    if response.status_code in [401, 402, 403]:
                        (root / "STOP").touch(exist_ok=True)
                        raise ValueError(f"provider_access_{response.status_code}")
                    if (
                        response.status_code == 429 or response.status_code >= 500
                    ) and attempt < 3:
                        time.sleep(min(2**attempt, 8))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    attempt_event(
                        folder,
                        {
                            "event": "payload_received",
                            "id": attempt_id,
                            "usage": payload.get("usage"),
                            "time": time.time(),
                        },
                    )
                    break
                if payload is None:
                    raise ValueError("embedding_retry_exhausted")
                values = validate_vectors(payload, batch, 1024)
                saved = {
                    "request_sha256": digest(request),
                    "model": config["model"]["model"],
                    "dimensions": 1024,
                    "input_chunk_ids": [r["chunk_id"] for r in batch],
                    "rows": values,
                    "usage": payload.get("usage"),
                }
                store_cache(path, batchid, saved)
            if saved["request_sha256"] != digest(request) or saved[
                "input_chunk_ids"
            ] != [r["chunk_id"] for r in batch]:
                raise ValueError("batch_input_mismatch")
            for row, v in zip(batch, saved["rows"], strict=True):
                if (
                    row["chunk_id"] != v["chunk_id"]
                    or row["embedding_text_sha256"] != v["embedding_text_sha256"]
                ):
                    raise ValueError("batch_row_mismatch")
            vector_rows.extend(saved["rows"])
            usage = saved.get("usage") or {}
            value = usage.get("total_tokens")
            if isinstance(value, int):
                tokens += value
            else:
                unknown_usage += 1
        if len(vector_rows) != len(rows):
            raise ValueError("vector_count_mismatch")
        vectorfile = folder / "vectors.jsonl"
        vectorbytes = b"".join(packed(v) + b"\n" for v in vector_rows)
        if vectorfile.exists():
            if vectorfile.read_bytes() != vectorbytes:
                raise ValueError("vector_output_changed")
        else:
            atomic_new(vectorfile, vectorbytes)
        if (
            sha(entry["source_path"]) != entry["source_sha256"]
            or sha(entry["input_path"]) != entry["input_sha256"]
        ):
            raise ValueError("source_changed_during_processing")
        receipt = {
            "id": entry["id"],
            "status": "completed",
            "document_id": metadata["document_id"],
            "title": entry["title"],
            "chunks": len(rows),
            "characters": len(text),
            "reported_tokens": tokens,
            "unknown_usage_batches": unknown_usage,
            "quality": metadata["quality"],
            "output_hashes": {
                **metadata["output_hashes"],
                "document.json": sha(folder / "document.json"),
                "parsed.json": sha(folder / "parsed.json"),
                "vectors.jsonl": sha(vectorfile),
            },
            "source_sha256": entry["source_sha256"],
            "input_sha256": entry["input_sha256"],
        }
        store_cache(done, identity, receipt)
        return receipt
    except Exception as exc:  # noqa: BLE001 -- per-document failure is a durable result, never success
        error = {
            "id": entry["id"],
            "status": "failed",
            "type": type(exc).__name__,
            "code": str(exc) if isinstance(exc, ValueError) else None,
            "http_status": getattr(getattr(exc, "response", None), "status_code", None),
        }
        # No provider response body, headers, credential or source text in logs.
        atomic_new(folder / ("failure-" + uuid.uuid4().hex + ".json"), packed(error))
        return error


def run(root, workers, limit, selected):
    config = load(root / "config.json")
    if (
        sha(__file__) != config["script_sha256"]
        or sha(root / "sources.jsonl") != config["sources_sha256"]
    ):
        raise ValueError("frozen_run_identity_mismatch")
    installed = {
        d.metadata["Name"]: d.version for d in importlib.metadata.distributions()
    }
    if installed != config["packages"] or sys.version != config["python"]:
        raise ValueError("dependency_identity_changed")
    if (root / "STOP").exists():
        raise ValueError("stop_marker_present")
    import msvcrt

    lock = (root / "coordinator.lock").open("a+b")
    lock.seek(0)
    try:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError as exc:
        raise ValueError("another_batch_coordinator_running") from exc
    entries = read_sources(root, config)
    if selected:
        entries = [e for e in entries if e["id"] in selected.split(",")]
    if limit:
        entries = entries[:limit]
    started = time.time()
    counts = Counter()
    chunks = 0
    tokens = 0
    runid = time.strftime("%Y%m%d-%H%M%S")
    journal = (root / f"journal-{runid}.jsonl").open("x", encoding="utf-8")

    def report(last=None):
        status = {
            "run": runid,
            "pid": os.getpid(),
            "target_documents": len(entries),
            "counts": dict(counts),
            "chunks": chunks,
            "reported_tokens": tokens,
            "seconds": round(time.time() - started, 1),
            "last": last,
        }
        tmp = root / "progress.tmp"
        tmp.write_bytes(packed(status))
        os.replace(tmp, root / "progress.json")
        print(json.dumps(status, ensure_ascii=False), flush=True)

    try:
        # Recreate whole pools between bounded epochs; avoid in-pool worker recycling.
        for offset in range(0, len(entries), workers * 100):
            if (root / "STOP").exists():
                break
            with futures.ProcessPoolExecutor(
                max_workers=workers, initializer=init_worker, initargs=(str(root),)
            ) as pool:
                iterator = iter(entries[offset : offset + workers * 100])
                pending = {}
                for _ in range(workers * 2):
                    e = next(iterator, None)
                    if e is not None:
                        pending[pool.submit(process, e)] = e["id"]
                last_report = time.time()
                while pending:
                    finished, _ = futures.wait(
                        pending, timeout=15, return_when=futures.FIRST_COMPLETED
                    )
                    for task in finished:
                        ident = pending.pop(task)
                        try:
                            r = task.result()
                        except Exception as exc:  # noqa: BLE001 -- preserve worker failure identity
                            r = {
                                "id": ident,
                                "status": "worker_failed",
                                "type": type(exc).__name__,
                            }
                        journal.write(json.dumps(r, ensure_ascii=False) + "\n")
                        journal.flush()
                        counts[r["status"]] += 1
                        chunks += r.get("chunks", 0)
                        tokens += r.get("reported_tokens", 0)
                        if not (root / "STOP").exists():
                            e = next(iterator, None)
                            if e is not None:
                                pending[pool.submit(process, e)] = e["id"]
                    if time.time() - last_report >= 30 or not pending:
                        report()
                        last_report = time.time()
        report("finished")
    finally:
        journal.close()
        lock.close()


def verify(root):
    config = load(root / "config.json")
    entries = read_sources(root, config)
    counts = Counter()
    errors = []
    total_chunks = 0
    tokens = 0
    review = 0
    for entry in entries:
        folder = root / "documents" / entry["id"]
        done = folder / "completed.json"
        if not done.exists():
            counts["not_completed"] += 1
            errors.append({"id": entry["id"], "error": "not_completed"})
            continue
        try:
            receipt = read_cache(done, digest([config, entry]))
            for name, h in receipt["output_hashes"].items():
                if sha(folder / name) != h:
                    raise ValueError("output_hash_mismatch")
            text = (folder / "fulltext.txt").read_text(encoding="utf-8")
            rows = load(folder / "chunks.json")
            vectors = [
                json.loads(x)
                for x in (folder / "vectors.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            if len(rows) != len(vectors) or len(rows) != receipt["chunks"]:
                raise ValueError("counts_mismatch")
            cursor = 0
            rebuild = ""
            for row, v in zip(rows, vectors, strict=True):
                start, end = row["start_char"], row["end_char"]
                if (
                    row["text"] != text[start:end]
                    or row["embedding_text"]
                    != f"法律名称：{entry['title']}\n正文：{row['text']}"
                ):
                    raise ValueError("slice_or_input_mismatch")
                if row["fulltext_sha256"] != digest(text) or row[
                    "embedding_text_sha256"
                ] != digest(row["embedding_text"]):
                    raise ValueError("text_digest_mismatch")
                if start > cursor or (cursor and cursor - start != 200):
                    raise ValueError("window_coverage_gap")
                rebuild += row["text"][cursor - start :]
                cursor = end
                if (
                    row["chunk_id"] != v["chunk_id"]
                    or row["embedding_text_sha256"] != v["embedding_text_sha256"]
                ):
                    raise ValueError("vector_identity_mismatch")
                if (
                    len(v["vector"]) != 1024
                    or not all(math.isfinite(n) for n in v["vector"])
                    or not any(v["vector"])
                ):
                    raise ValueError("invalid_vector")
            if rebuild != text:
                raise ValueError("fulltext_not_reconstructed")
            if (
                sha(entry["source_path"]) != entry["source_sha256"]
                or sha(entry["input_path"]) != entry["input_sha256"]
            ):
                raise ValueError("source_changed")
            counts["verified"] += 1
            total_chunks += len(rows)
            tokens += receipt["reported_tokens"]
            review += bool(receipt["quality"]["needs_review"])
        except (ValueError, KeyError, OSError) as exc:
            counts["invalid"] += 1
            errors.append({"id": entry["id"], "type": type(exc).__name__})
    result = {
        "status": "passed"
        if counts["verified"] == config["documents"]
        else "incomplete",
        "counts": dict(counts),
        "chunks": total_chunks,
        "reported_tokens": tokens,
        "documents_needing_quality_review": review,
        "errors": errors,
        "scope": "Data integrity only; no RAGAS or legal quality acceptance",
    }
    atomic_new(
        root / ("verification-" + time.strftime("%Y%m%d-%H%M%S") + ".json"),
        packed(result),
    )
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "run", "status", "verify"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--ids")
    args = parser.parse_args()
    if not 1 <= args.workers <= 4:
        raise ValueError("workers_must_be_1_to_4")
    if args.command == "prepare":
        prepare(args.root, args.workspace)
    elif args.command == "run":
        run(args.root, args.workers, args.limit, args.ids)
    elif args.command == "verify":
        verify(args.root)
    else:
        print(json.dumps(load(args.root / "progress.json"), ensure_ascii=False))


if __name__ == "__main__":
    main()
