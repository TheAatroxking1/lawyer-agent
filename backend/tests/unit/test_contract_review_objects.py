import hashlib
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from lawyer_agent.application.contract_review.contracts import ReviewError, ReviewScope
from lawyer_agent.infrastructure.contract_review.objects import S3ContractObjects


def scope():
    return ReviewScope(tenant_id=uuid4(), actor_id=uuid4(), run_id=uuid4(),
                       document_version_id="document")


@pytest.mark.asyncio
async def test_signed_immutable_object_and_tenant_bound_read():
    payload = b"%PDF-1.7\nsynthetic test"
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "PUT":
            assert request.headers["if-none-match"] == "*"
            assert request.headers["authorization"].startswith("AWS4-HMAC-SHA256 ")
            assert request.headers["x-amz-content-sha256"] == hashlib.sha256(payload).hexdigest()
            return httpx.Response(200)
        return httpx.Response(200, content=payload)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        objects = S3ContractObjects(endpoint="http://localhost:9000", bucket="contracts",
            access_key=SecretStr("test-user"), secret_key=SecretStr("test-secret"), client=client)
        owner = scope()
        stored = await objects.put(owner, payload)
        assert await objects.get(owner, stored.key, stored.sha256, stored.size) == payload
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await objects.get(scope(), stored.key, stored.sha256, stored.size)
        assert len(calls) == 2


@pytest.mark.asyncio
async def test_corrupt_or_redirected_object_is_never_accepted():
    owner = scope()
    payload = b"%PDF-synthetic"
    digest = hashlib.sha256(payload).hexdigest()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"%PDF-corrupted")
    )) as client:
        objects = S3ContractObjects(endpoint="http://localhost:9000", bucket="contracts",
            access_key=SecretStr("test-user"), secret_key=SecretStr("test-secret"), client=client)
        key = f"tenants/{owner.tenant_id}/contract-reviews/{owner.run_id}/{digest}.pdf"
        with pytest.raises(ReviewError, match="object_integrity_failed"):
            await objects.get(owner, key, digest, len(payload))
