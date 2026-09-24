"""Optional disk cache for fixed-snapshot public-corpus embedding results.

Files contain no input text. Float64 preserves the provider's post-normalization
Python values exactly; float32 would silently change the represented vectors.
Local bounded file IO is synchronous; the delegate retains model execution and
cancellation ownership. Cache counters distinguish requests from model work.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from lawyer_agent.application.model_gateway import ModelProviderPort, ModelProviderUnavailable
from lawyer_agent.domain.model_gateway import (
    ChatMessage,
    EmbeddingVector,
    RankedDocument,
    TokenUsage,
)
from lawyer_agent.infrastructure.providers.embedding_windows import parse_embedding_model_ref

_MAGIC = b"LAEMB001"


class _Provider(ModelProviderPort, Protocol):
    async def aclose(self, *, timeout_seconds: float = 5.0) -> bool: ...


def reject_reparse_path(path: Path) -> None:
    """Reject existing links/junctions at every component, including the entry."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 1024:
            raise ValueError("embedding_cache_reparse_path")


class LocalEmbeddingCacheProvider:
    def __init__(
        self,
        provider: _Provider,
        *,
        cache_directory: Path,
        model_ref: str,
        normalization: str = "l2",
    ) -> None:
        model_path, profiled = parse_embedding_model_ref(model_ref)
        snapshot = Path(model_path)
        if (
            not isinstance(cache_directory, Path)
            or not cache_directory.is_absolute()
            or not profiled
            or not snapshot.is_absolute()
            or snapshot.parent.name != "snapshots"
            or re.fullmatch(r"[0-9a-f]{40}", snapshot.name) is None
            or not snapshot.is_dir()
            or normalization != "l2"
        ):
            raise ValueError("embedding_cache_configuration_invalid")
        reject_reparse_path(cache_directory)
        self._root = cache_directory.resolve()
        self._provider = provider
        self._identity = {
            "format": "embedding-cache-f64-v1",
            "model_ref": model_ref,
            "snapshot": snapshot.name,
            "normalization": normalization,
        }
        self._closed = False
        self._counts = dict.fromkeys(
            (
                "cache_hit_rows",
                "cache_miss_rows",
                "provider_call_attempts",
                "provider_submitted_rows",
                "provider_completed_calls",
                "provider_completed_rows",
                "corrupt_entries",
                "read_failures",
                "write_failures",
            ),
            0,
        )

    def stats(self) -> dict[str, int]:
        return dict(self._counts)

    def _key(self, text: str, dimension: int) -> bytes:
        encoded = text.encode("utf-8")
        value = {
            **self._identity,
            "dimension": dimension,
            "text_sha256": hashlib.sha256(encoded).hexdigest(),
            "text_bytes": len(encoded),
        }
        return hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("ascii")
        ).digest()

    def _path(self, key: bytes) -> Path:
        name = key.hex()
        path = self._root / name[:2] / f"{name}.emb"
        reject_reparse_path(path)
        return path

    @staticmethod
    def _validate(vector: EmbeddingVector, dimension: int) -> None:
        if (
            not isinstance(vector, EmbeddingVector)
            or type(vector.dimension) is not int
            or vector.dimension != dimension
            or len(vector.values) != dimension
            or any(type(value) is not float or not math.isfinite(value) for value in vector.values)
            or not math.isclose(math.hypot(*vector.values), 1.0, abs_tol=1e-6)
        ):
            raise ModelProviderUnavailable("embedding_cache_invalid_vector")

    def _read(self, key: bytes, dimension: int) -> EmbeddingVector | None:
        path = self._path(key)
        size = len(_MAGIC) + 32 + dimension * 8 + 32
        try:
            with path.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size != size:
                    raise ValueError("cache size mismatch")
                data = stream.read(size + 1)
            if (
                len(data) != size
                or data[:8] != _MAGIC
                or data[8:40] != key
                or hashlib.sha256(data[:-32]).digest() != data[-32:]
            ):
                raise ValueError("cache integrity mismatch")
            vector = EmbeddingVector(
                tuple(struct.unpack(f"<{dimension}d", data[40:-32])), dimension
            )
            self._validate(vector, dimension)
            return vector
        except FileNotFoundError:
            return None
        except (ValueError, struct.error, ModelProviderUnavailable):
            self._counts["corrupt_entries"] += 1
            return None
        except OSError:
            self._counts["read_failures"] += 1
            return None

    def _write(self, key: bytes, vector: EmbeddingVector) -> None:
        path = self._path(key)
        temporary = path.with_name(f".{key.hex()}.{uuid4().hex}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            reject_reparse_path(path)
            data = _MAGIC + key + struct.pack(f"<{vector.dimension}d", *vector.values)
            data += hashlib.sha256(data).digest()
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            reject_reparse_path(path)
            os.replace(temporary, path)
        except OSError:
            self._counts["write_failures"] += 1
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                self._counts["write_failures"] += 1

    async def embed(
        self,
        *,
        texts: Sequence[str],
        dimension: int,
        timeout_seconds: float,
    ) -> tuple[EmbeddingVector, ...]:
        if self._closed:
            raise ModelProviderUnavailable("embedding cache is closed")
        if (
            not isinstance(texts, Sequence)
            or isinstance(texts, (str, bytes, bytearray))
            or not texts
            or any(not isinstance(text, str) or not text for text in texts)
            or type(dimension) is not int
            or dimension <= 0
            or type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("embedding_cache_input_invalid")
        snapshot = tuple(texts)
        keys = [self._key(text, dimension) for text in snapshot]
        resolved: dict[bytes, EmbeddingVector] = {}
        missing: dict[bytes, str] = {}
        for key, text in zip(keys, snapshot, strict=True):
            if key in missing:
                self._counts["cache_miss_rows"] += 1
                continue
            vector = resolved.get(key)
            if vector is None:
                vector = self._read(key, dimension)
            if vector is None:
                missing[key] = text
                self._counts["cache_miss_rows"] += 1
            else:
                resolved[key] = vector
                self._counts["cache_hit_rows"] += 1
        if missing:
            self._counts["provider_call_attempts"] += 1
            self._counts["provider_submitted_rows"] += len(missing)
            vectors = await self._provider.embed(
                texts=tuple(missing.values()),
                dimension=dimension,
                timeout_seconds=timeout_seconds,
            )
            if len(vectors) != len(missing):
                raise ModelProviderUnavailable("embedding_cache_provider_row_mismatch")
            for vector in vectors:
                self._validate(vector, dimension)
            self._counts["provider_completed_calls"] += 1
            self._counts["provider_completed_rows"] += len(vectors)
            for key, vector in zip(missing, vectors, strict=True):
                self._write(key, vector)
                resolved[key] = vector
        return tuple(resolved[key] for key in keys)

    async def aclose(self, *, timeout_seconds: float = 5.0) -> bool:
        self._closed = True
        return await self._provider.aclose(timeout_seconds=timeout_seconds)

    async def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        timeout_seconds: float,
    ) -> tuple[RankedDocument, ...]:
        return await self._provider.rerank(
            query=query,
            documents=documents,
            timeout_seconds=timeout_seconds,
        )

    async def chat(
        self,
        *,
        messages: Sequence[ChatMessage],
        timeout_seconds: float,
    ) -> tuple[str, TokenUsage]:
        return await self._provider.chat(messages=messages, timeout_seconds=timeout_seconds)
