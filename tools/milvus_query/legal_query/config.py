"""查询Embedding配置与HTTP边界；凭据只在运行时读取。"""

from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MODEL = "qwen3.7-text-embedding"
DIMENSIONS = 1024


class QueryError(Exception):
    """只携带稳定错误码，避免供应商Payload或凭据进入CLI输出。"""


def require(condition: bool, code: str) -> None:
    if not condition:
        raise QueryError(code)


def validate_vector(value: Any) -> list[float]:
    require(
        isinstance(value, list) and len(value) == DIMENSIONS, "invalid_vector_dimension"
    )
    require(
        all(type(x) in (int, float) and math.isfinite(x) for x in value),
        "invalid_vector",
    )
    require(any(x != 0 for x in value), "zero_vector")
    require(all(abs(x) <= 3.4028234e38 for x in value), "vector_float32_overflow")
    return [float(x) for x in value]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        # 不向重定向目的地址传递Authorization。
        return None


@dataclass(frozen=True)
class EmbeddingConfig:
    base_url: str
    api_key: str = field(repr=False)
    model: str = MODEL
    dimensions: int = DIMENSIONS

    @classmethod
    def load(cls) -> EmbeddingConfig:
        path = Path(
            os.environ.get("EMBEDDING_CONFIG_FILE", "/run/secrets/embedding.json")
        )
        try:
            with path.open("rb") as handle:
                raw = handle.read(65537)
            require(len(raw) <= 65536, "embedding_config_too_large")
            value = json.loads(raw)
            config = cls(**value)
        except (OSError, ValueError, TypeError):
            raise QueryError("embedding_config_invalid") from None
        config.validate()
        return config

    def validate(self) -> None:
        require(
            self.model == MODEL
            and type(self.dimensions) is int
            and self.dimensions == DIMENSIONS,
            "embedding_model_mismatch",
        )
        require(
            isinstance(self.api_key, str) and bool(self.api_key.strip()),
            "embedding_key_missing",
        )
        require(isinstance(self.base_url, str), "embedding_endpoint_invalid")
        url = urlsplit(self.base_url)
        require(
            url.scheme == "https"
            and bool(url.hostname)
            and not url.username
            and not url.password
            and not url.query
            and not url.fragment,
            "embedding_endpoint_invalid",
        )

    def embed(self, query: str) -> list[float]:
        return self.embed_with_usage(query)[0]

    def embed_with_usage(self, query: str) -> tuple[list[float], dict[str, Any]]:
        self.validate()
        require(
            isinstance(query, str) and 0 < len(query.strip()) <= 8000, "invalid_query"
        )
        body = {
            "model": self.model,
            "dimensions": self.dimensions,
            "input": [query],
            "encoding_format": "float",
        }
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/embeddings",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.api_key,
            },
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=30
            ) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            require(len(raw) <= 4 * 1024 * 1024, "embedding_response_too_large")
            payload = json.loads(raw)
            require(payload.get("model") == MODEL, "embedding_response_model_mismatch")
            records = payload["data"]
            require(
                len(records) == 1 and records[0]["index"] == 0,
                "embedding_response_invalid",
            )
            return validate_vector(records[0]["embedding"]), {
                "model": self.model,
                "usage": payload.get("usage"),
                "request_id": payload.get("request_id", payload.get("id")),
            }
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                raise QueryError("embedding_access_denied") from None
            if error.code == 429:
                raise QueryError("embedding_rate_limited") from None
            raise QueryError("embedding_http_error") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise QueryError("embedding_unavailable") from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise QueryError("embedding_response_invalid") from None
