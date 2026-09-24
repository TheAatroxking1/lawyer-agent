"""Bounded legal-document retrieval for contract research.

The local store reads the verified paragraph export.  It never reconstructs a
document from retrieval chunks, because those intentionally contain parent and
child overlap.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from mcp.server import MCPServer
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import Field, SecretStr, ValidationError

from lawyer_agent.application.contract_review.contracts import (
    Identifier,
    ReviewError,
    ReviewScope,
    WireModel,
)
from lawyer_agent.application.contract_review.ports import Authorizer, ScopeProvider
from lawyer_agent.application.contract_review.research import (
    LegalCandidate,
    LegalDocumentPage,
    ResearchRegion,
)

_IDENTITY = re.compile(r"^[0-9a-f]{64}$")
_DIRECTORY = re.compile(r"^[0-9a-f]{24}$")
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 1024 * 1024
_MAX_PARAGRAPHS_BYTES = 32 * 1024 * 1024
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_REVIEW_FLAGS = frozenset(
    {
        "images_or_textboxes_not_ocr",
        "structure_review_required",
        "table_layout_not_preserved",
        "automatic_numbering_not_resolved",
        "legacy_binary_static_text_recovery",
    }
)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _regular_file(path: Path, *, maximum: int) -> bytes:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or attributes & reparse or not stat.S_ISREG(info.st_mode):
        raise ValueError("unsafe_corpus_path")
    if info.st_size > maximum:
        raise ValueError("corpus_file_too_large")
    with path.open("rb") as handle:
        payload = handle.read(maximum + 1)
    if len(payload) > maximum:
        raise ValueError("corpus_file_too_large")
    after = path.lstat()
    if (
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        getattr(info, "st_ino", None),
    ) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
        getattr(after, "st_ino", None),
    ):
        raise ValueError("corpus_changed_during_read")
    return payload


class LocalLegalDocumentStore:
    """Read a manifest-bound export with signed, document-bound cursors."""

    def __init__(
        self,
        *,
        root: Path,
        cursor_secret: SecretStr,
        expected_manifest_sha256: str | None = None,
        max_paragraphs: int = 100,
        max_chars: int = 64_000,
        max_concurrency: int = 2,
    ) -> None:
        if not 1 <= max_paragraphs <= 1000 or not 1 <= max_chars <= 262_144:
            raise ValueError("invalid_page_limits")
        if not 1 <= max_concurrency <= 4:
            raise ValueError("invalid_read_capacity")
        self._capacity = asyncio.Semaphore(max_concurrency)
        self._active: set[asyncio.Task[LegalDocumentPage]] = set()
        self._closed = False
        secret = cursor_secret.get_secret_value().encode()
        if len(secret) < 16:
            raise ValueError("cursor_secret_too_short")
        self._root = root.resolve(strict=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise ValueError("unsafe_corpus_root")
        self._secret = secret
        self._max_paragraphs = max_paragraphs
        self._max_chars = max_chars
        manifest_path = self._root / "manifest.jsonl"
        manifest = _regular_file(manifest_path, maximum=_MAX_MANIFEST_BYTES)
        digest = _sha(manifest)
        if expected_manifest_sha256 is not None and not hmac.compare_digest(
            digest, expected_manifest_sha256
        ):
            raise ValueError("manifest_hash_mismatch")
        self.manifest_sha256 = digest
        self._manifest_signature = self._signature(manifest_path)
        self._documents = self._parse_manifest(manifest)

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int, int | None]:
        info = path.lstat()
        return info.st_size, info.st_mtime_ns, info.st_ctime_ns, getattr(info, "st_ino", None)

    @staticmethod
    def _parse_manifest(payload: bytes) -> dict[str, dict[str, Any]]:
        documents: dict[str, dict[str, Any]] = {}
        for raw in payload.splitlines():
            row = json.loads(raw)
            document_id = row.get("document_id")
            directory = row.get("output_directory")
            if (
                not isinstance(document_id, str)
                or _IDENTITY.fullmatch(document_id) is None
                or not isinstance(directory, str)
                or _DIRECTORY.fullmatch(directory) is None
                or row.get("status") != "completed"
                or row.get("id_namespace") != "offline-export-v1"
                or document_id in documents
            ):
                raise ValueError("invalid_manifest")
            documents[document_id] = row
        if not documents:
            raise ValueError("empty_manifest")
        return documents

    async def read(self, document_id: str, cursor: str | None) -> LegalDocumentPage:
        if self._closed:
            raise ValueError("legal_document_store_closed")
        await self._capacity.acquire()
        if self._closed:
            self._capacity.release()
            raise ValueError("legal_document_store_closed")
        task = asyncio.create_task(asyncio.to_thread(self._read_sync, document_id, cursor))
        self._active.add(task)
        task.add_done_callback(self._read_finished)
        return await asyncio.shield(task)

    def _read_finished(self, task: asyncio.Task[LegalDocumentPage]) -> None:
        self._active.discard(task)
        self._capacity.release()
        if not task.cancelled():
            task.exception()

    async def aclose(self, *, timeout_seconds: float = 2) -> None:
        self._closed = True
        if self._active:
            _, pending = await asyncio.wait(tuple(self._active), timeout=timeout_seconds)
            if pending:
                raise TimeoutError("legal_document_close_timeout")

    def _read_sync(self, document_id: str, cursor: str | None) -> LegalDocumentPage:
        if _IDENTITY.fullmatch(document_id) is None or document_id not in self._documents:
            raise ValueError("document_unavailable")
        manifest_path = self._root / "manifest.jsonl"
        manifest_raw = _regular_file(manifest_path, maximum=_MAX_MANIFEST_BYTES)
        if (
            self._signature(manifest_path) != self._manifest_signature
            or not hmac.compare_digest(_sha(manifest_raw), self.manifest_sha256)
        ):
            raise ValueError("manifest_changed")
        manifest = self._documents[document_id]
        directory = self._safe_directory(str(manifest["output_directory"]))
        document_raw = _regular_file(directory / "document.json", maximum=_MAX_DOCUMENT_BYTES)
        document = json.loads(document_raw)
        self._validate_document(document_id, manifest, document)
        paragraphs_raw = _regular_file(
            directory / "paragraphs.jsonl", maximum=_MAX_PARAGRAPHS_BYTES
        )
        content_hash = _sha(paragraphs_raw)
        expected_hash = document.get("output_hashes", {}).get("paragraphs.jsonl")
        if not isinstance(expected_hash, str) or not hmac.compare_digest(
            content_hash, expected_hash
        ):
            raise ValueError("paragraphs_hash_mismatch")
        paragraphs = self._parse_paragraphs(paragraphs_raw, document)
        version = str(document["source_sha256"])
        offset = self._decode_cursor(cursor, document_id, version, content_hash)
        if offset >= len(paragraphs):
            raise ValueError("invalid_cursor")
        end = offset
        size = 0
        pieces: list[str] = []
        while end < len(paragraphs) and end - offset < self._max_paragraphs:
            piece = str(paragraphs[end]["text"]) + "\n"
            piece_size = len(piece.encode())
            if pieces and size + piece_size > self._max_chars:
                break
            if not pieces and piece_size > self._max_chars:
                raise ValueError("paragraph_too_large")
            pieces.append(piece)
            size += piece_size
            end += 1
        complete = end == len(paragraphs)
        next_cursor = None
        if not complete:
            next_cursor = self._encode_cursor(document_id, version, content_hash, end)
        flags = tuple(str(item) for item in document["quality_flags"])
        quality: Literal["verified", "needs_review"] = (
            "needs_review" if _REVIEW_FLAGS.intersection(flags) else "verified"
        )
        relative = PurePosixPath(str(document["source_relative_path"]))
        return LegalDocumentPage(
            document_id=document_id,
            title=relative.stem,
            version=version,
            content_hash=content_hash,
            cursor=cursor,
            next_cursor=next_cursor,
            text="".join(pieces),
            complete=complete,
            quality=quality,
            quality_flags=flags,
            source_ref=f"offline-export-v1:{document_id}",
            dataset_version=self.manifest_sha256,
            parser_version=document.get("parser_version", "unknown"),
        )

    def _safe_directory(self, name: str) -> Path:
        candidate = self._root / name
        info = candidate.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(info.st_mode)
            or attributes & reparse
            or not stat.S_ISDIR(info.st_mode)
            or candidate.parent.resolve(strict=True) != self._root
        ):
            raise ValueError("unsafe_corpus_path")
        return candidate

    @staticmethod
    def _validate_document(
        document_id: str, manifest: dict[str, Any], document: dict[str, Any]
    ) -> None:
        if (
            document.get("document_id") != document_id
            or document.get("id_namespace") != "offline-export-v1"
            or document.get("schema_version") != "legal-corpus-export-v2"
            or document.get("source_relative_path") != manifest.get("source_relative_path")
            or document.get("source_sha256") != manifest.get("source_sha256")
            or document.get("quality_flags") != manifest.get("quality_flags")
            or not isinstance(document.get("paragraph_count"), int)
            or document["paragraph_count"] <= 0
            or _IDENTITY.fullmatch(str(document.get("source_sha256"))) is None
        ):
            raise ValueError("document_metadata_mismatch")

    @staticmethod
    def _parse_paragraphs(payload: bytes, document: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ordinal, raw in enumerate(payload.splitlines()):
            row = json.loads(raw)
            if (
                row.get("ordinal") != ordinal
                or not isinstance(row.get("text"), str)
                or not row["text"]
                or row.get("location") not in {"body", "table", "auxiliary"}
                or not isinstance(row.get("part"), str)
            ):
                raise ValueError("invalid_paragraphs")
            rows.append(row)
        if len(rows) != document["paragraph_count"]:
            raise ValueError("paragraph_count_mismatch")
        return rows

    def _cursor_message(
        self, document_id: str, version: str, content_hash: str, offset: int
    ) -> bytes:
        return (
            f"{self.manifest_sha256}:{document_id}:{version}:{content_hash}:{offset}"
        ).encode()

    def _encode_cursor(
        self, document_id: str, version: str, content_hash: str, offset: int
    ) -> str:
        signature = hmac.new(
            self._secret,
            self._cursor_message(document_id, version, content_hash, offset),
            hashlib.sha256,
        ).hexdigest()
        return f"{offset}.{signature}"

    def _decode_cursor(
        self, cursor: str | None, document_id: str, version: str, content_hash: str
    ) -> int:
        if cursor is None:
            return 0
        try:
            raw_offset, supplied = cursor.split(".", 1)
            offset = int(raw_offset)
        except (AttributeError, TypeError, ValueError):
            raise ValueError("invalid_cursor") from None
        if offset <= 0:
            raise ValueError("invalid_cursor")
        expected = self._encode_cursor(document_id, version, content_hash, offset).split(".", 1)[1]
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid_cursor")
        return offset


class _Documents(Protocol):
    async def read(self, document_id: str, cursor: str | None) -> LegalDocumentPage: ...


@dataclass
class _RunState:
    candidates: dict[str, LegalCandidate] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)
    first_seen: dict[str, int] = field(default_factory=dict)
    selected: set[str] = field(default_factory=set)
    frozen_ids: frozenset[str] | None = None
    query_count: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RagLegalResearchTools:
    """HTTP RAG search plus manifest-backed full text, frozen per review run."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: SecretStr,
        documents: _Documents,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 20.0,
        max_response_bytes: int = _MAX_HTTP_RESPONSE_BYTES,
        max_run_states: int = 1024,
        max_selected_documents: int = 3,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("invalid_rag_url")
        if (
            not 0 < timeout_seconds <= 60
            or not 1024 <= max_response_bytes <= 4 * 1024 * 1024
            or not 1 <= max_run_states <= 100_000
        ):
            raise ValueError("invalid_http_limits")
        key = api_key.get_secret_value()
        if not key:
            raise ValueError("missing_rag_key")
        if client is not None and client.follow_redirects:
            raise ValueError("redirects_must_be_disabled")
        self._url = base_url.rstrip("/") + "/search"
        self._key = key
        self._documents = documents
        self._timeout = timeout_seconds
        self._max_response_bytes = max_response_bytes
        self._max_run_states = max_run_states
        if type(max_selected_documents) is not int or not 1 <= max_selected_documents <= 7:
            raise ValueError("invalid_document_read_limit")
        self._max_selected_documents = max_selected_documents
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._runs: dict[tuple[str, str, str, str], _RunState] = {}
        self._runs_lock = asyncio.Lock()

    async def aclose(self) -> None:
        async with self._runs_lock:
            self._runs.clear()
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> RagLegalResearchTools:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.aclose()

    @staticmethod
    def _scope_key(scope: ReviewScope) -> tuple[str, str, str, str]:
        return (
            str(scope.tenant_id),
            str(scope.actor_id),
            str(scope.run_id),
            scope.document_version_id,
        )

    async def _state(self, scope: ReviewScope) -> _RunState:
        key = self._scope_key(scope)
        async with self._runs_lock:
            if key not in self._runs and len(self._runs) >= self._max_run_states:
                raise ValueError("research_scope_capacity_exceeded")
            return self._runs.setdefault(key, _RunState())

    async def search(
        self, scope: ReviewScope, query: str, *, region: str = "",
    ) -> Sequence[LegalCandidate]:
        if not isinstance(query, str) or not 0 < len(query.strip()) <= 2000:
            raise ValueError("invalid_query")
        state = await self._state(scope)
        async with state.lock:
            if state.frozen_ids is not None:
                raise ValueError("research_scope_frozen")
            if state.query_count >= 7:
                raise ValueError("research_query_limit_exceeded")
            state.query_count += 1
            payload = await self._post_json(self._search_body(query.strip(), region))
            candidates = self._candidates(payload)
            for rank, candidate in enumerate(candidates, start=1):
                existing = state.candidates.get(candidate.document_id)
                if existing is not None and existing.title != candidate.title:
                    raise ValueError("rag_document_identity_mismatch")
                if existing is None:
                    if len(state.candidates) >= 49:
                        raise ValueError("legal_candidate_limit_exceeded")
                    state.candidates[candidate.document_id] = candidate
                    state.first_seen[candidate.document_id] = len(state.first_seen)
                    state.scores[candidate.document_id] = 0.0
                state.scores[candidate.document_id] += 1.0 / (60 + rank)
            return candidates

    def _search_body(self, query: str, region: str) -> dict[str, Any]:
        if region:
            raise ValueError("rag_region_filter_unsupported")
        return {"query": query, "mode": "hybrid", "top_k": 20,
                "document_ids": [], "raw_bm25": False, "max_chars": 40000}

    def _candidates(self, payload: dict[str, Any]) -> tuple[LegalCandidate, ...]:
        if payload.get("mode") != "hybrid" or payload.get("ranker") != "rrf":
            raise ValueError("rag_ranker_mismatch")
        evidence = payload.get("evidence")
        if not isinstance(evidence, list) or len(evidence) > 20:
            raise ValueError("invalid_rag_response")
        grouped: dict[str, dict[str, Any]] = {}
        for row in evidence:
            if not isinstance(row, dict):
                raise ValueError("invalid_rag_response")
            document_id = row.get("document_id")
            title = row.get("document_name")
            snippet = row.get("text")
            citation = row.get("evidence_id")
            if (
                not isinstance(document_id, str)
                or _IDENTITY.fullmatch(document_id) is None
                or not isinstance(title, str)
                or not 0 < len(title) <= 4000
                or not isinstance(snippet, str)
                or not snippet
                or not isinstance(citation, str)
                or not 0 < len(citation) <= 4000
            ):
                raise ValueError("invalid_rag_response")
            snippet = snippet[:4000]
            if document_id not in grouped and len(grouped) < 7:
                grouped[document_id] = {"title": title, "snippets": [], "citations": []}
            target = grouped.get(document_id)
            if target is None:
                continue
            if target["title"] != title:
                raise ValueError("rag_document_identity_mismatch")
            if snippet not in target["snippets"] and len(target["snippets"]) < 10:
                target["snippets"].append(snippet)
            if citation not in target["citations"] and len(target["citations"]) < 20:
                target["citations"].append(citation)
        return tuple(
            LegalCandidate(
                document_id=document_id,
                title=value["title"],
                snippets=tuple(value["snippets"]),
                citations=tuple(value["citations"]),
            )
            for document_id, value in grouped.items()
        )

    async def read(
        self, scope: ReviewScope, document_id: str, cursor: str | None
    ) -> LegalDocumentPage:
        state = await self._state(scope)
        async with state.lock:
            if state.frozen_ids is None:
                ordered = sorted(
                    state.candidates,
                    key=lambda item: (-state.scores[item], state.first_seen[item]),
                )
                state.frozen_ids = frozenset(ordered[:7])
            if document_id not in state.frozen_ids:
                raise ValueError("document_not_in_candidates")
            if document_id not in state.selected:
                if len(state.selected) >= self._max_selected_documents:
                    raise ValueError("document_read_limit_exceeded")
                state.selected.add(document_id)
        page = await self._documents.read(document_id, cursor)
        candidate = state.candidates[document_id]
        if page.document_id != document_id or page.title != candidate.title:
            raise ValueError("legal_document_identity_mismatch")
        return page

    async def _post_json(
        self, body: dict[str, Any], *, url: str | None = None,
    ) -> dict[str, Any]:
        try:
            async with asyncio.timeout(self._timeout):
                return await self._post_json_within_timeout(body, url=url)
        except TimeoutError:
            raise ValueError("rag_timeout") from None

    async def _post_json_within_timeout(
        self, body: dict[str, Any], *, url: str | None = None,
    ) -> dict[str, Any]:
        request = self._client.build_request(
            "POST",
            url or self._url,
            headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"},
            json=body,
        )
        response = await self._client.send(request, stream=True)
        try:
            if response.is_redirect:
                raise ValueError("rag_redirect_rejected")
            if response.status_code != 200:
                raise ValueError("rag_http_error")
            parts: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > self._max_response_bytes:
                    raise ValueError("rag_response_too_large")
                parts.append(chunk)
            parsed = json.loads(b"".join(parts))
            if not isinstance(parsed, dict):
                raise ValueError("invalid_rag_response")
            return parsed
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ValueError("invalid_rag_response") from None
        finally:
            await response.aclose()


class _SearchDocumentsInput(WireModel):
    query: Annotated[str, Field(min_length=1, max_length=2000)]
    region: ResearchRegion = ""


class _ReadDocumentInput(WireModel):
    document_id: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    cursor: Identifier | None = None


class _CandidateResult(WireModel):
    items: Annotated[tuple[LegalCandidate, ...], Field(max_length=7)]


class _LegalResearch(Protocol):
    async def search(
        self, scope: ReviewScope, query: str, *, region: str = "",
    ) -> Sequence[LegalCandidate]: ...

    async def read(
        self, scope: ReviewScope, document_id: str, cursor: str | None
    ) -> LegalDocumentPage: ...


async def _guard_arguments(
    ctx: ServerRequestContext[Any], call_next: CallNext
) -> HandlerResult:
    if ctx.method == "tools/call":
        params = ctx.params or {}
        name, arguments = params.get("name"), params.get("arguments")
        try:
            if (
                not isinstance(name, str)
                or not isinstance(arguments, dict)
                or set(arguments) != {"request"}
                or len(json.dumps(arguments, ensure_ascii=False).encode()) > 65_536
            ):
                raise ValueError("invalid envelope")
        except (ValueError, TypeError):
            return CallToolResult(
                is_error=True,
                content=[TextContent(type="text", text="invalid_tool_arguments")],
            )
    result = await call_next(ctx)
    failed = (
        isinstance(result, CallToolResult) and result.is_error
    ) or (
        isinstance(result, dict) and result.get("isError") is True
    )
    if ctx.method == "tools/call" and failed:
        return CallToolResult(
            is_error=True,
            content=[TextContent(type="text", text="tool_failure")],
        )
    return result


async def _authorized_call[ResultT: WireModel](
    *,
    scopes: ScopeProvider,
    authorizer: Authorizer,
    action: str,
    resource: str,
    call: Any,
    result_type: type[ResultT],
) -> ResultT:
    try:
        scope = await scopes.current()
        await authorizer.require(scope, action, resource)
        raw = await call(scope)
        serializable = raw.model_dump(mode="json") if isinstance(raw, WireModel) else raw
        if len(json.dumps(serializable, ensure_ascii=False).encode()) > 262_144:
            raise ReviewError("tool_result_too_large")
        result = result_type.model_validate(raw)
        if len(result.model_dump_json().encode()) > 262_144:
            raise ReviewError("tool_result_too_large")
        return result
    except asyncio.CancelledError:
        raise
    except ReviewError as exc:
        code = (
            exc.code
            if exc.code in {"resource_unavailable", "tool_not_allowed"}
            else "tool_failure"
        )
        raise ToolError(code) from None
    except (ValidationError, ValueError, TypeError, OSError, httpx.HTTPError):
        raise ToolError("tool_failure") from None
    except Exception:
        raise ToolError("tool_failure") from None


def build_legal_research_mcp(
    *,
    service: _LegalResearch,
    scopes: ScopeProvider,
    authorizer: Authorizer,
) -> MCPServer:
    """Expose only request envelopes; identity always comes from trusted scope."""
    server = MCPServer("contract-legal-documents", middleware=[_guard_arguments])

    @server.tool(meta={
        "lawyer_agent/capability": "legal.search_documents",
        "lawyer_agent/agent_visible": True,
        "lawyer_agent/resource_constant": "public-law",
    })
    async def legal_search_documents(request: _SearchDocumentsInput) -> _CandidateResult:
        """RRF检索并去重为最多七份法律文书候选。"""
        return await _authorized_call(
            scopes=scopes,
            authorizer=authorizer,
            action="legal_search_documents",
            resource="public-law",
            call=lambda scope: _search_result(service, scope, request.query, request.region),
            result_type=_CandidateResult,
        )

    @server.tool(meta={
        "lawyer_agent/capability": "legal.read_document",
        "lawyer_agent/agent_visible": True,
        "lawyer_agent/resource_path": "/request/document_id",
    })
    async def legal_read_document(request: _ReadDocumentInput) -> LegalDocumentPage:
        """分页读取本轮候选中已选法律文书，最多三份。"""
        return await _authorized_call(
            scopes=scopes,
            authorizer=authorizer,
            action="legal_read_document",
            resource=request.document_id,
            call=lambda scope: service.read(scope, request.document_id, request.cursor),
            result_type=LegalDocumentPage,
        )

    return server


build_legal_document_mcp = build_legal_research_mcp


async def _search_result(
    service: _LegalResearch, scope: ReviewScope, query: str, region: str = "",
) -> _CandidateResult:
    items = (await service.search(scope, query, region=region)
             if region else await service.search(scope, query))
    return _CandidateResult(items=tuple(items))
