"""Immutable, tenant-scoped PDF objects over bounded async S3 HTTP requests."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from botocore.auth import SigV4Auth  # type: ignore[import-untyped]
from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]
from botocore.credentials import Credentials  # type: ignore[import-untyped]
from pydantic import SecretStr

from lawyer_agent.application.contract_review.contracts import ReviewError, ReviewScope

MAX_PDF_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class StoredPDF:
    key: str
    sha256: str
    size: int


class S3ContractObjects:
    def __init__(
        self, *, endpoint: str, bucket: str, access_key: SecretStr, secret_key: SecretStr,
        client: httpx.AsyncClient, region: str = "us-east-1", timeout_seconds: float = 30,
    ) -> None:
        url = urlsplit(endpoint)
        if (
            url.scheme not in {"http", "https"} or not url.hostname
            or url.username or url.password or url.query or url.fragment
            or url.path not in {"", "/"}
            or re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is None
            or not 0 < timeout_seconds <= 60 or client.follow_redirects
            or not access_key.get_secret_value() or not secret_key.get_secret_value()
        ):
            raise ValueError("invalid contract object store configuration")
        self._endpoint, self._bucket, self._region = endpoint.rstrip("/"), bucket, region
        self._credentials = Credentials(
            access_key.get_secret_value(), secret_key.get_secret_value(),
        )
        self._client, self._timeout = client, timeout_seconds

    @staticmethod
    def _key(scope: ReviewScope, digest: str) -> str:
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise ReviewError("resource_unavailable")
        return f"tenants/{scope.tenant_id}/contract-reviews/{scope.run_id}/{digest}.pdf"

    async def put(self, scope: ReviewScope, payload: bytes) -> StoredPDF:
        if not payload.startswith(b"%PDF-") or not 5 < len(payload) <= MAX_PDF_BYTES:
            raise ReviewError("document_invalid")
        digest = hashlib.sha256(payload).hexdigest()
        key = self._key(scope, digest)
        status, _body = await self._request("PUT", key, payload, maximum=4096)
        result = StoredPDF(key=key, sha256=digest, size=len(payload))
        if status == 412:
            await self.get(scope, key, digest, len(payload))
        elif status not in {200, 201, 204}:
            raise ReviewError("object_store_unavailable")
        return result

    async def get(self, scope: ReviewScope, key: str, digest: str, size: int) -> bytes:
        if key != self._key(scope, digest) or not 5 < size <= MAX_PDF_BYTES:
            raise ReviewError("resource_unavailable")
        status, body = await self._request("GET", key, b"", maximum=MAX_PDF_BYTES)
        if status != 200:
            raise ReviewError("object_store_unavailable")
        if (
            len(body) != size or not body.startswith(b"%PDF-")
            or not hmac.compare_digest(hashlib.sha256(body).hexdigest(), digest)
        ):
            raise ReviewError("object_integrity_failed")
        return body

    async def _request(
        self, method: str, key: str, payload: bytes, *, maximum: int,
    ) -> tuple[int, bytes]:
        url = f"{self._endpoint}/{self._bucket}/{key}"
        headers = {"x-amz-content-sha256": hashlib.sha256(payload).hexdigest()}
        if method == "PUT":
            headers.update({"Content-Type": "application/pdf", "If-None-Match": "*"})
        request = AWSRequest(method=method, url=url, data=payload, headers=headers)
        SigV4Auth(self._credentials, "s3", self._region).add_auth(request)
        try:
            async with asyncio.timeout(self._timeout):
                async with self._client.stream(
                    method, url, headers=dict(request.headers), content=payload,
                    timeout=self._timeout, follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        raise ReviewError("object_store_unavailable")
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(body) + len(chunk) > maximum:
                            raise ReviewError("object_response_too_large")
                        body.extend(chunk)
                    return response.status_code, bytes(body)
        except (httpx.HTTPError, TimeoutError):
            raise ReviewError("object_store_unavailable") from None
