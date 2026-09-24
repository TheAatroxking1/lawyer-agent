"""第二版窗口检索内网桥接；只读已冻结发布产物，不回退第一版。"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymilvus import MilvusClient

from legal_query.config import QueryError, require
from legal_query.http_runtime import WorkGate
from legal_query.http_service import Guard, problem
from legal_query.window_corpus import (
    COLLECTION,
    Release,
    digest,
    load,
    sha,
    validate_ready,
)
from legal_query.window_retrieval import Options, search_windows


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=2000)
    region: str = Field(default="", max_length=32)
    top_k: int = Field(default=7, strict=True, ge=7, le=7)

    @field_validator("query")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty_query")
        return value.strip()

    @field_validator("region")
    @classmethod
    def known_region(cls, value: str) -> str:
        try:
            Options(region=value).expression("validation")
        except QueryError:
            raise ValueError("invalid_region") from None
        return value


class ReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    cursor: str | None = Field(
        default=None, max_length=160, pattern=r"^[0-9a-f]+\.[0-9a-f]{64}$"
    )


class ExcerptsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    chunk_ids: list[Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(
        min_length=1, max_length=7
    )

    @field_validator("chunk_ids")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("duplicate_chunks")
        return value


def excerpt_ranges(text: str, chunks: list[dict]) -> tuple[list[dict], bool]:
    # Complete a cut article only within 1000 extra characters per edge.
    # Huge/uncertain boundaries remain explicit partial context, never trigger full-law reads.
    articles = [m.start() for m in re.finditer(
        r"(?m)^(?:#{1,6}[ \t]*)?第[零〇一二三四五六七八九十百千万两0-9]+条[ \t　]", text
    )]
    paragraphs = [0, *[m.end() for m in re.finditer(r"\n+", text)], len(text)]
    spans, incomplete = [], False
    for chunk in chunks:
        start, end = chunk["start_char"], chunk["end_char"]
        require(type(start) is int and type(end) is int
                and 0 <= start < end <= len(text) and end - start <= 1000
                and text[start:end] == chunk["text"], "window_span_mismatch")
        starts = [p for p in articles if start - 1000 <= p <= start]
        ends = [p for p in articles if end <= p <= end + 1000]
        incomplete |= bool(articles) and ((not starts and start > 0) or (not ends and end < len(text)))
        if not starts:
            starts = [p for p in paragraphs if start - 1000 <= p <= start]
        if not ends:
            ends = [p for p in paragraphs if end <= p <= end + 1000]
        incomplete |= not starts or not ends
        spans.append({"start_char": max(starts) if starts else start,
                      "end_char": min(ends) if ends else end,
                      "chunk_ids": [chunk["chunk_id"]]})
    merged = []
    for span in sorted(spans, key=lambda item: item["start_char"]):
        if merged and span["start_char"] <= merged[-1]["end_char"]:
            merged[-1]["end_char"] = max(merged[-1]["end_char"], span["end_char"])
            merged[-1]["chunk_ids"].extend(span["chunk_ids"])
        else:
            merged.append(span)
    return merged, incomplete


class DocumentStore:
    """启动仅加载清单；按source_id定位，按请求核验所读产物。"""

    def __init__(self, state: Path, key: str, report: Path | None = None):
        self.key = key.encode("ascii")
        self.ready = state / "ready.json"
        receipt = load(self.ready)
        require(
            receipt["status"] == "ready" and receipt["collection"] == COLLECTION,
            "window_index_not_ready",
        )
        self.release = Release(report or Path(receipt["source_report"]))
        self.release_hash = self.release.sha256
        require(
            self.release_hash
            == receipt["release_sha256"]
            == receipt["identity"]["release_sha256"],
            "window_release_changed",
        )
        snapshot_path = state / "source-artifacts.json"
        require(
            sha(snapshot_path) == receipt["identity"]["source_snapshot_sha256"],
            "window_snapshot_changed",
        )
        snapshot = load(snapshot_path)
        require(
            snapshot["release_sha256"] == self.release_hash, "window_snapshot_changed"
        )
        self.completions = snapshot["completions"]
        self.entries = {r["id"]: r for r in self.release.report["rows"]}
        require(
            set(self.entries) == set(self.completions), "window_snapshot_incomplete"
        )

    def _cursor(self, identity: list[str], offset: int) -> str:
        position = format(offset, "x")
        signature = hmac.new(
            self.key, digest([identity, position]).encode(), hashlib.sha256
        ).hexdigest()
        return position + "." + signature

    def excerpts(self, row: dict[str, Any], chunk_ids: list[str]) -> dict[str, Any]:
        require(1 <= len(chunk_ids) <= 7 and len(set(chunk_ids)) == len(chunk_ids),
                "window_chunks_invalid")
        return self.read(row, None, chunk_ids=chunk_ids)

    def read(self, row: dict[str, Any], cursor: str | None, *,
             chunk_ids: list[str] | None = None) -> dict[str, Any]:
        require(
            row.get("release_sha256") == self.release_hash, "window_release_changed"
        )
        entry = self.entries.get(row.get("source_id"))
        require(entry is not None, "window_document_unavailable")
        folder = Path(entry["directory"])
        require(
            sha(folder / "completed.json") == self.completions[entry["id"]],
            "window_artifact_changed",
        )
        done = load(folder / "completed.json")
        data = done["data"]
        require(
            digest(data) == done["data_sha256"] and data["status"] == "completed",
            "window_completion_invalid",
        )
        for name in ("document.json", "fulltext.txt"):
            require(
                sha(folder / name) == data["output_hashes"][name],
                "window_artifact_changed",
            )
        meta = load(folder / "document.json")
        source = meta["source"]
        config = load(folder.parent.parent / "config.json")
        require(done["identity"] == digest([config, source]), "window_identity_changed")
        require(
            source["id"] == entry["id"]
            and source["source_sha256"]
            == entry["source_sha256"]
            == row["source_sha256"]
            and source["source_relative_path"] == entry["source_relative_path"]
            and meta["document_id"] == data["document_id"] == row["document_id"]
            and meta["document_name"] == entry["title"] == row["document_name"],
            "window_identity_changed",
        )
        text = (folder / "fulltext.txt").read_text(encoding="utf-8")
        require(
            bool(text)
            and digest(text) == meta["fulltext_sha256"] == row["fulltext_sha256"],
            "window_fulltext_changed",
        )
        identity = [
            self.release_hash,
            meta["document_id"],
            source["source_sha256"],
            meta["fulltext_sha256"],
        ]
        offset = 0
        if cursor is not None:
            try:
                offset = int(cursor.split(".", 1)[0], 16)
            except (ValueError, AttributeError):
                raise QueryError("invalid_cursor") from None
            require(
                0 < offset < len(text)
                and hmac.compare_digest(cursor, self._cursor(identity, offset)),
                "invalid_cursor",
            )
        end = min(offset + 20000, len(text))
        flags = list(
            dict.fromkeys(
                [
                    *entry.get("old_quality_flags", []),
                    "legal_validity_unknown",
                    "jurisdiction_not_verified",
                    "text_artifact_verified_only",
                ]
            )
        )
        quality = entry.get("quality", {})
        if entry.get("needs_review"):
            flags.append("parser_review_required")
        if quality.get("source_xml_paragraphs_not_verbatim_contained", 0):
            flags.append("source_text_coverage_warning")
        if quality.get("pictures", 0):
            flags.append("source_images_not_verified")
        flags = list(dict.fromkeys(flags))
        require(len(flags) <= 50, "window_quality_flags_exceeded")
        result = {
            "document_id": meta["document_id"],
            "title": meta["document_name"],
            "version": source["source_sha256"],
            "content_hash": meta["fulltext_sha256"],
            "cursor": cursor,
            "next_cursor": self._cursor(identity, end) if end < len(text) else None,
            "text": text[offset:end],
            "complete": end == len(text),
            "quality": "verified",
            "quality_flags": flags,
            "source_ref": source.get("source_path", source["source_relative_path"]),
            "jurisdiction": "unknown",
            "validity": "unknown",
            "issuing_authority": None,
            "effective_from": None,
            "effective_until": None,
            "dataset_version": self.release_hash,
            "parser_version": config["version"] + ":" + config["script_sha256"],
        }
        if chunk_ids is not None:
            chunk_path = folder / "chunks.jsonl"
            require(sha(chunk_path) == data["output_hashes"]["chunks.jsonl"],
                    "window_artifact_changed")
            selected = {}
            with chunk_path.open(encoding="utf-8") as stream:
                for line in stream:
                    chunk = json.loads(line)
                    if chunk["chunk_id"] in chunk_ids:
                        require(chunk["document_id"] == row["document_id"]
                                and chunk["chunk_id"] not in selected, "window_chunk_mismatch")
                        selected[chunk["chunk_id"]] = chunk
            require(set(selected) == set(chunk_ids), "window_chunk_unavailable")
            ranges, incomplete = excerpt_ranges(text, [selected[key] for key in chunk_ids])
            context = "\n\n[…未选入段落…]\n\n".join(
                text[span["start_char"]:span["end_char"]] for span in ranges
            )
            result.update(text=context, cursor=None, next_cursor=None, complete=True,
                          content_scope="retrieved_excerpts", source_ranges=ranges,
                          context_hash=digest(context))
            if incomplete:
                result["quality_flags"] = [*flags, "context_boundary_incomplete"]
        return result


class Runtime:
    def __init__(
        self,
        key: str,
        store: DocumentStore | None,
        config: Path,
        *,
        client_factory=None,
        target=None,
    ):
        require(
            32 <= len(key) <= 256
            and key.isascii()
            and not any(c.isspace() for c in key),
            "rag_key_invalid",
        )
        self.key, self.store, self.config = key, store, config
        self.target = target or {"uri": "http://localhost:19530", "database": "blog"}
        self.client_factory = client_factory or (
            lambda: MilvusClient(
                uri=self.target["uri"],
                db_name=self.target["database"],
                token=os.getenv("MILVUS_TOKEN", ""),
                timeout=15,
            )
        )
        self.gate = WorkGate(2)
        self.condition = threading.Condition()
        self.active = 0
        self.closed = False

    @classmethod
    def from_env(cls):
        try:
            with Path(os.environ["RAG_API_KEY_FILE"]).open("rb") as stream:
                key = stream.read(258).decode("ascii").strip()
            state = Path(os.environ["WINDOW_RAG_STATE"])
            config = Path(os.environ["WINDOW_RAG_CONFIG"])
            report = (
                Path(os.environ["WINDOW_RELEASE_REPORT"])
                if os.getenv("WINDOW_RELEASE_REPORT")
                else None
            )
            return cls(
                key,
                DocumentStore(state, key, report),
                config,
                target={
                    "uri": os.getenv("MILVUS_URI", "http://localhost:19530"),
                    "database": os.getenv("MILVUS_DB", "blog"),
                },
            )
        except (OSError, UnicodeError, KeyError):
            raise QueryError("window_configuration_invalid") from None

    def run(self, operation, payload=None, **kwargs):
        def work():
            with self.condition:
                require(not self.closed, "service_closed")
                self.active += 1
            try:
                return self._execute(operation, payload)
            finally:
                with self.condition:
                    self.active -= 1
                    self.condition.notify_all()

        return self.gate.run(work)

    async def close(self, timeout: float = 5) -> bool:
        def wait():
            with self.condition:
                self.closed = True
                return self.condition.wait_for(
                    lambda: self.active == 0, timeout=timeout
                )

        stopped = await asyncio.to_thread(wait)
        if not stopped:
            logging.getLogger(__name__).warning("window_workers_still_running")
        return stopped

    def _execute(self, operation, payload):
        require(self.store is not None, "window_store_unavailable")
        client = self.client_factory()
        try:
            receipt = validate_ready(client, self.store.ready, target=self.target)
            require(
                receipt["release_sha256"] == self.store.release_hash,
                "window_release_changed",
            )
            if operation == "ready":
                return {
                    "status": "ready",
                    "collection": COLLECTION,
                    "release_sha256": self.store.release_hash,
                }
            if operation == "search":
                require(
                    load(self.config)["rerank"]["model"] == "qwen3-rerank",
                    "window_rerank_model_mismatch",
                )
                return search_windows(
                    client,
                    payload.query,
                    self.store.ready,
                    Options(region=payload.region, top_k=7),
                    self.config,
                    target=self.target,
                )
            require(operation in {"read", "excerpts"}, "invalid_operation")

            rows = client.query(
                COLLECTION,
                filter="document_id == "
                + json.dumps(payload.document_id)
                + " and release_sha256 == "
                + json.dumps(self.store.release_hash),
                output_fields=[
                    "source_id",
                    "document_id",
                    "document_name",
                    "source_sha256",
                    "fulltext_sha256",
                    "release_sha256",
                ],
                limit=1,
                consistency_level="Strong",
                timeout=15,
            )
            require(
                len(rows) == 1 and rows[0]["document_id"] == payload.document_id,
                "window_document_unavailable",
            )
            if operation == "excerpts":
                return self.store.excerpts(rows[0], payload.chunk_ids)
            return self.store.read(rows[0], payload.cursor)
        finally:
            client.close()


def create_app(runtime: Runtime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
        app.state.runtime = runtime or Runtime.from_env()
        try:
            yield
        finally:
            await app.state.runtime.close()

    app = FastAPI(
        title="第二版窗口RAG内网服务", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    if runtime is not None:
        app.state.runtime = runtime
    app.add_middleware(Guard, owner=app)

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return problem(422, "invalid_request", request.state.request_id)

    async def execute(request, operation, payload=None):
        try:
            result = await asyncio.to_thread(app.state.runtime.run, operation, payload)
            return {**result, "request_id": request.state.request_id}
        except QueryError as error:
            code = str(error)
            if code == "service_busy":
                return problem(429, code, request.state.request_id)
            if code == "invalid_cursor":
                return problem(422, code, request.state.request_id)
            return problem(503, "dependency_unavailable", request.state.request_id)
        except Exception:  # noqa: BLE001 -- SDK errors must not expose credentials or text.
            return problem(503, "dependency_unavailable", request.state.request_id)

    @app.get("/healthz")
    async def health():
        return {"status": "alive"}

    @app.get("/readyz")
    async def ready(request: Request):
        return await execute(request, "ready")

    @app.post("/search")
    async def search(body: SearchRequest, request: Request):
        return await execute(request, "search", body)

    @app.post("/documents/read")
    async def read(body: ReadRequest, request: Request):
        return await execute(request, "read", body)

    @app.post("/documents/excerpts")
    async def excerpts(body: ExcerptsRequest, request: Request):
        return await execute(request, "excerpts", body)

    return app


app = create_app()
