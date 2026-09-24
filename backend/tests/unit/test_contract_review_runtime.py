from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from lawyer_agent.application.contract_review.contracts import ReviewError
from lawyer_agent.application.contract_review.research import LegalDocumentPage
from lawyer_agent.application.contract_review.web import CreateReviewInput
from lawyer_agent.infrastructure.contract_review.objects import StoredPDF
from lawyer_agent.infrastructure.contract_review.runtime import (
    ContractReviewService,
    ContractRuntimeConfig,
    run_view,
)
from lawyer_agent.infrastructure.contract_review.storage import FailureLease, RunRecord
from lawyer_agent.infrastructure.providers.dashscope import DashScopeSettings


@pytest.mark.asyncio
async def test_visual_nonstream_retry_shares_budget_authorization_and_usage():
    from lawyer_agent.application.contract_review.contracts import RunLimits
    from lawyer_agent.application.model_gateway import ModelGateway, ModelProviderInvalidResponse
    from lawyer_agent.domain.model_gateway import ChatMessage
    from lawyer_agent.infrastructure.contract_review.llm import GatewayReviewModel
    from lawyer_agent.infrastructure.contract_review.runtime import _BoundedModel, _Usage
    from lawyer_agent.infrastructure.providers.dashscope import DashScopeChatProvider
    from tests.unit.test_contract_review_pdf import _scope

    seen = []
    async def require(*args):
        seen.append(args)
    def handler(request):
        body = json.loads(request.content)
        if body['stream']:
            error = {'code': 'invalid_parameter_error', 'message':
                     'Model output became abnormal while generating a JSON response for '
                     'response_format.'}
            return httpx.Response(200, text='data: ' + json.dumps({'error': error}) + '\n\n')
        return httpx.Response(200, json={
            'choices': [{'finish_reason': 'stop', 'message': {'content': '{}'}}],
            'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
        })
    usage = _Usage()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        def model(stream):
            return GatewayReviewModel(ModelGateway(DashScopeChatProvider(
                settings=DashScopeSettings(api_key=SecretStr('synthetic')), client=client,
                max_tokens=None, json_mode=True, stream_response=stream), usage),
                model_ref='qwen3.6-flash')
        bounded = _BoundedModel(model(True), SimpleNamespace(scope=_scope(), require=require),
                                RunLimits(max_steps=2), usage, nonstream_model=model(False))
        messages = [ChatMessage(role='user', content='JSON')]
        with pytest.raises(ModelProviderInvalidResponse):
            await bounded.chat(messages)
        assert await bounded.chat_nonstream(messages) == '{}'
        with pytest.raises(ReviewError, match='step_limit_exceeded'):
            await bounded.chat_nonstream(messages)
    assert len(seen) == 3
    assert [r.status for r in usage.records] == ['error', 'success']
    assert usage.records[0].usage is None
    assert usage.records[1].usage.total_tokens == 2


def test_run_view_never_releases_unfinished_or_failed_issues():
    for state in ("running", "failed", "cancelled", "no_evidence"):
        record = RunRecord(id=uuid4(), tenant_id=uuid4(), created_by_user_id=uuid4(),
            created_by_membership_id=uuid4(), filename="合同.pdf", instruction="默认审阅",
            as_of=date.today(), status=state, pdf_object_key=None, pdf_sha256=None, pdf_size=None,
            parsed_document=None, result={"issues": [{"problem": "UNVALIDATED LEGAL OUTPUT"}]},
            evidence_manifest=None, failure_code=None, version=1,
            created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
        view = run_view(record)
        assert not view.issues
        assert "UNVALIDATED" not in view.model_dump_json()
        if state == "no_evidence":
            assert view.message == "我没有找到相关的法律文档，暂时无法提供相关服务"


def _pdf(text: str = "Alpha beta") -> bytes:
    stream = f"BT /F1 12 Tf 20 160 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/CropBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> "
        b"/Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(result)


def _actor(*, tenant: UUID | None = None, user: UUID | None = None):
    tenant_id = tenant or uuid4()
    user_id = user or uuid4()
    membership_id = uuid4()
    return SimpleNamespace(
        principal=SimpleNamespace(user_id=user_id, membership_id=membership_id),
        context=SimpleNamespace(tenant_id=tenant_id, membership_id=membership_id),
    )


def _record(actor, *, status: str = "uploaded", payload: bytes | None = None) -> RunRecord:
    now = datetime.now(UTC)
    data = payload or _pdf()
    digest = hashlib.sha256(data).hexdigest()
    return RunRecord(
        id=uuid4(), tenant_id=actor.context.tenant_id,
        created_by_user_id=actor.principal.user_id,
        created_by_membership_id=actor.context.membership_id,
        filename="contract.pdf", instruction="请中立审阅", as_of=date(2026, 9, 16),
        status=status, pdf_object_key=f"object/{digest}.pdf", pdf_sha256=digest,
        pdf_size=len(data), parsed_document=None, result=None, evidence_manifest=None,
        failure_code=None, version=1, created_at=now, updated_at=now,
    )


def test_withheld_citation_metadata_survives_persisted_view_and_sse_projection():
    from lawyer_agent.application.contract_review.web import DRAFT_MESSAGE, ContractReviewFinal

    record = _record(_actor(), status="draft")
    withheld = [{"index": 2, "page": 7, "reason": "unverified_citation"}]
    record = replace(record, result={
        "run_id": str(record.id), "document_version_id": record.pdf_sha256,
        "issues": [], "withheld_issues": withheld,
    })
    view = run_view(record)
    assert view.message == DRAFT_MESSAGE
    final = ContractReviewFinal.from_run(view).model_dump(mode="json")
    assert final["withheld_issues"] == withheld


def test_public_citation_labels_are_recovered_from_run_manifest_without_private_metadata():
    from lawyer_agent.application.contract_review.web import ContractReviewFinal

    record = _record(_actor(), status="draft")
    record = replace(record, result={
        "run_id": str(record.id), "document_version_id": record.pdf_sha256,
        "issues": [{"block_id": "b1", "anchor_id": None, "start": 0, "end": 2,
                    "quote": "租金", "category": "legal", "severity": "medium",
                    "problem": "需要核对", "suggestion": "请核对适用性",
                    "evidence_ids": ["law-1"], "evidence_passages": [
                        {"passage_id": "law1.p1", "document_id": "law-1", "quote": "法律原文"},
                    ]}],
    }, evidence_manifest={"documents": [
        {"document_id": "law-1", "title": "测试合同法", "source_ref": "private-file"},
        {"document_id": "unused", "title": "未引用材料"},
    ]})
    view = run_view(record)
    expected = [{"document_id": "law-1", "title": "测试合同法"}]
    assert view.model_dump(mode="json")["evidence_documents"] == expected
    final = ContractReviewFinal.from_run(view).model_dump(mode="json")
    assert final["evidence_documents"] == expected


def test_public_mcp_catalog_is_recovered_from_manifest_without_schema():
    record = _record(_actor(), status="draft")
    record = replace(record, result={
        "run_id": str(record.id), "document_version_id": record.pdf_sha256,
        "issues": [],
    }, evidence_manifest={
        "documents": [], "model_calls": [], "mcp_tools": [{
            "name": "document_read_blocks", "capability": "document.read_blocks",
            "description": "读取合同文字块", "status": "discovered",
        }],
    })
    view = run_view(record)
    assert view.mcp_tools[0].name == "document_read_blocks"
    assert "schema" not in view.model_dump_json()


class FakeRepository:
    def __init__(self, actor, payload: bytes) -> None:
        self.actor = actor
        self.payload = payload
        self.record = _record(actor, payload=payload)
        self.finish_calls = 0
        self.failures: list[tuple[str, bool]] = []
        self.failure_manifest = None

    def _owner(self, actor) -> None:
        if (
            actor.context.tenant_id != self.record.tenant_id
            or actor.principal.user_id != self.record.created_by_user_id
            or actor.context.membership_id != self.record.created_by_membership_id
        ):
            raise ReviewError("resource_unavailable")

    async def create(self, actor, filename, instruction, as_of, idempotency_key):
        self._owner(actor)
        self.record = replace(self.record, status="awaiting_upload", filename=filename,
                              instruction=instruction, as_of=as_of)
        return self.record

    async def get(self, actor, run_id):
        self._owner(actor)
        if run_id != self.record.id:
            raise ReviewError("resource_unavailable")
        return self.record

    async def authorize(self, actor, run_id, *, write=False):
        return await self.get(actor, run_id)

    async def attach_pdf(self, actor, run_id, key, digest, size):
        await self.get(actor, run_id)
        self.record = replace(self.record, status="uploaded", pdf_object_key=key,
                              pdf_sha256=digest, pdf_size=size)
        return self.record

    async def claim(self, actor, run_id):
        await self.get(actor, run_id)
        if self.record.status != "uploaded":
            raise ReviewError("contract_run_conflict")
        lease = FailureLease(self.record.tenant_id, self.record.id, SecretStr("ab" * 32))
        self.record = replace(self.record, status="running", failure_lease=lease)
        return self.record

    async def finish(self, actor, run_id, *, status, result, parsed_document,
                     evidence_manifest):
        await self.get(actor, run_id)
        if self.record.status != "running":
            raise ReviewError("contract_run_conflict")
        self.finish_calls += 1
        self.record = replace(self.record, status=status, result=result,
                              parsed_document=parsed_document,
                              evidence_manifest=evidence_manifest, failure_lease=None)
        return self.record

    async def fail_owned(self, lease, code, *, cancelled, evidence_manifest=None):
        self.failures.append((code, cancelled))
        self.failure_manifest = evidence_manifest
        self.record = replace(self.record, status="cancelled" if cancelled else "failed",
                              failure_code=code, failure_lease=None,
                              evidence_manifest=evidence_manifest)
        return self.record


class FakeObjects:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def put(self, scope, payload):
        digest = hashlib.sha256(payload).hexdigest()
        self.payload = payload
        return StoredPDF(key=f"object/{digest}.pdf", sha256=digest, size=len(payload))

    async def get(self, scope, key, digest, size):
        assert hashlib.sha256(self.payload).hexdigest() == digest
        assert len(self.payload) == size
        return self.payload


class FakeDocuments:
    async def aclose(self) -> None:
        pass

    async def read(self, document_id: str, cursor: str | None) -> LegalDocumentPage:
        assert cursor is None
        return LegalDocumentPage(
            document_id=document_id, title="Synthetic law", version="v1",
            content_hash="c" * 64, text="Synthetic full legal text", complete=True,
            quality="verified", source_ref="synthetic", jurisdiction="中国大陆",
            validity="unknown", dataset_version="test", parser_version="test",
        )


def _service(
    actor, *, draft: bool, fail_react: bool = False,
    react_wait: asyncio.Event | None = None,
    vision_page: dict | None = None,
    payload_override: bytes | None = None,
) -> tuple[ContractReviewService, FakeRepository, list[str]]:
    payload = payload_override if payload_override is not None else _pdf()
    repo = FakeRepository(actor, payload)
    calls: list[str] = []
    document_id = "d" * 64

    def response(text: str) -> httpx.Response:
        frames = [
            {"choices": [{"finish_reason": "stop", "delta": {"content": text}}]},
            {"choices": [], "usage": {
                "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}},
        ]
        return httpx.Response(200, text=''.join(
            'data: ' + json.dumps(frame) + '\n\n' for frame in frames
        ) + 'data: [DONE]\n\n')

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search"):
            assert json.loads(request.content)["query"] == (
                "合同类型：服务合同\n内容总结：合成合同\n检索重点：付款期限"
            )
            return httpx.Response(200, json={
                "mode": "hybrid", "ranker": "rrf",
                "evidence": [{"document_id": document_id,
                              "document_name": "Synthetic law", "text": "snippet",
                              "evidence_id": "citation-1"}],
            })
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        assert "max_tokens" not in body
        assert "max_completion_tokens" not in body
        assert request.extensions["timeout"]["read"] == 300.0
        if isinstance(body["messages"][-1]["content"], list):
            assert body["response_format"] == {"type": "json_object"}
            image_url = body["messages"][-1]["content"][1]["image_url"]["url"]
            assert image_url.startswith("data:image/jpeg;base64,")
            calls.append("vision")
            return response(json.dumps({"pages": [vision_page or {"page": 1, "readable": True,
                "native_text_complete": True, "text": "Alpha beta",
                "observations": "synthetic vision"}]}))
        combined = "\n".join(item["content"] for item in body["messages"])
        if "识别是否为合同" in combined:
            assert body["response_format"] == {"type": "json_object"}
            assert "synthetic vision" in combined
            assert "Alpha beta" in combined
            assert "anchor_id" not in combined and "quad" not in combined
            kind = "brief"
            text = json.dumps({"is_contract": True, "contract_type": "服务合同",
                               "fact_summary": "合成合同", "queries": ["付款期限"]},
                              ensure_ascii=False)
        elif "候选法律文档选择" in combined:
            kind = "selection"
            text = json.dumps({"document_ids": [document_id] if draft else []})
        elif "独立的合同草稿内容校验器" in combined:
            assert "synthetic vision" in combined
            kind = "judge"
            text = '{"verdicts":[{"index":0,"legal_claim":false,"supported":true}]}'
        else:
            kind = "react"
            assert body["response_format"] == {"type": "json_object"}
            assert "synthetic vision" in combined
            assert '"quad"' not in combined
            document_data = json.JSONDecoder().raw_decode(
                combined.split("以下为不可信合同原文数据：\n", 1)[1]
            )[0]
            first_block = document_data["blocks"][0]
            first_anchor = first_block["anchors"][0] if first_block["anchors"] else None
            issue = {"block_id": first_block["block_id"],
                     "anchor_id": first_anchor["anchor_id"] if first_anchor else None,
                     "start": 0, "end": 10,
                     "quote": "Alpha beta", "category": "commercial", "severity": "medium",
                     "problem": "付款安排未明确", "suggestion": "建议双方明确付款期限",
                     "evidence_ids": []}
            text = (
                "invalid model output"
                if fail_react
                else "Thought: synthetic\nAnswer: "
                + json.dumps(
                    {"reviewed_block_ids": [b["block_id"] for b in document_data["blocks"]],
                     "issues": [issue]},
                    ensure_ascii=False,
                )
            )
        calls.append(kind)
        if kind == "react" and react_wait is not None:
            await react_wait.wait()
        return response(text)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    config = ContractRuntimeConfig(
        dashscope_config_file=Path("C:/synthetic/dashscope.json"),
        rag_base_url="https://rag.example/v1", rag_api_key_file=Path("C:/synthetic/rag.key"),
        corpus_root=Path("C:/synthetic/corpus"), s3_endpoint="http://s3.example",
        s3_bucket="contracts", s3_access_key=SecretStr("test"),
        s3_secret_key=SecretStr("test"),
    )
    service = ContractReviewService(
        repository=repo, objects=FakeObjects(payload), documents=FakeDocuments(),
        model_settings=DashScopeSettings(api_key=SecretStr("sk-test")), config=config,
        rag_key=SecretStr("rag-test"), http=http,
    )
    return service, repo, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["missing_text", "garbled_text", "unreadable", "uncertain"])
async def test_vision_warning_finishes_review_and_persists_safe_pages(reason) -> None:
    from lawyer_agent.application.contract_review.web import ContractReviewFinal
    from lawyer_agent.infrastructure.contract_review.storage import _validate_finish_payloads

    actor = _actor()
    service, repo, calls = _service(actor, draft=True, vision_page={
        "page": 1, "readable": True, "native_text_complete": False,
        "text": "Alpha beta PRIVATE_IMAGE_TEXT",
        "observations": "synthetic vision PRIVATE_REASONING",
        "discrepancies": [{"kind": reason, "quote": "PRIVATE_IMAGE_TEXT"}],
    })
    try:
        stages = []
        result = await service.run(actor, repo.record.id, stages.append)
        expected = {"pages": [{"page": 1, "reasons": [
            "unconfirmed_coverage" if reason == "uncertain" else reason,
        ]}]}
        assert result.status == "draft"
        assert "content_warning" in stages
        assert calls == ["vision", "brief", "selection", "react", "judge"]
        assert repo.failures == []
        assert repo.record.evidence_manifest["verification_report"] == expected
        _validate_finish_payloads(repo.record.result, repo.record.parsed_document,
                                  repo.record.evidence_manifest)
        assert "PRIVATE" not in json.dumps(repo.record.evidence_manifest)
        view = await service.get(actor, repo.record.id)
        assert view.verification_report.model_dump(mode="json") == expected
        assert ContractReviewFinal.from_run(view).verification_report == view.verification_report
        assert view.issues and view.blocks
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await service.get(_actor(), repo.record.id)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_real_runtime_no_evidence_persists_before_public_view() -> None:
    actor = _actor()
    service, repo, calls = _service(actor, draft=False)
    progress: list[str] = []
    try:
        result = await service.run(actor, repo.record.id, progress.append)
        assert result.status == "no_evidence"
        assert not result.issues
        assert repo.finish_calls == 1
        assert repo.record.evidence_manifest is not None
        assert len(repo.record.evidence_manifest["model_calls"]) == 3
        assert all(
            set(item) == {"operation", "model_ref", "status", "latency_ms", "usage"}
            for item in repo.record.evidence_manifest["model_calls"]
        )
        assert repo.record.parsed_document is not None
        assert repo.record.parsed_document["page_dimensions"] == [[200.0, 200.0]]
        assert calls == ["vision", "brief", "selection"]
        assert progress[-1] == "no_evidence"
        with pytest.raises(ReviewError, match="contract_run_conflict"):
            await service.run(actor, repo.record.id, progress.append)
        assert calls == ["vision", "brief", "selection"]
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_no_native_glyphs_still_finishes_with_unlocated_visual_advice():
    actor = _actor()
    service, repo, calls = _service(actor, draft=True, payload_override=_pdf(""))
    try:
        result = await service.run(actor, repo.record.id, lambda _: None)
        assert result.status == "draft"
        assert result.issues[0].anchor_id is None
        assert result.blocks[0].quality == "needs_review"
        assert result.blocks[0].anchors == ()
        assert result.verification_report.pages[0].reasons == ("missing_native_coordinates",)
        assert calls == ["vision", "brief", "selection", "react", "judge"]
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await service.get(_actor(), repo.record.id)
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_real_runtime_draft_runs_pdf_mcp_research_react_gate_and_persist() -> None:
    actor = _actor()
    service, repo, calls = _service(actor, draft=True)
    progress: list[str] = []
    try:
        result = await service.run(actor, repo.record.id, progress.append)
        assert result.status == "draft"
        assert [issue.quote for issue in result.issues] == ["Alpha beta"]
        assert repo.finish_calls == 1
        assert repo.record.evidence_manifest is not None
        assert repo.record.evidence_manifest["documents"][0]["document_id"] == "d" * 64
        assert len(repo.record.evidence_manifest["model_calls"]) == 5
        assert calls == ["vision", "brief", "selection", "react", "judge"]
        assert progress[-1] == "saved"
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_create_defaults_instruction_and_wrong_scope_cannot_run() -> None:
    actor = _actor()
    service, repo, calls = _service(actor, draft=False)
    try:
        view = await service.create(
            actor, CreateReviewInput(file_name="contract.pdf"), "idempotency-test"
        )
        assert repo.record.instruction == "请中立审阅这份合同有哪些问题。"
        assert view.status == "awaiting_upload"
        with pytest.raises(ReviewError, match="resource_unavailable"):
            await service.run(
                _actor(tenant=actor.context.tenant_id), repo.record.id, lambda _: None
            )
        assert not calls
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_failed_run_uses_failure_lease_and_persists_bounded_usage() -> None:
    actor = _actor()
    service, repo, calls = _service(actor, draft=True, fail_react=True)
    try:
        with pytest.raises(ReviewError, match="model_output_invalid"):
            await service.run(actor, repo.record.id, lambda _: None)
        assert repo.failures == [("model_output_invalid", False)]
        assert repo.record.status == "failed"
        assert repo.failure_manifest is not None
        assert len(repo.failure_manifest["model_calls"]) == 5
        assert all("error_code" not in item and "vector_count" not in item
                   for item in repo.failure_manifest["model_calls"])
        assert calls == ["vision", "brief", "selection", "react", "react"]
    finally:
        await service.aclose()


@pytest.mark.asyncio
async def test_cancelled_run_uses_failure_lease_and_retains_usage_record() -> None:
    actor = _actor()
    release = asyncio.Event()
    service, repo, calls = _service(actor, draft=True, react_wait=release)
    task = asyncio.create_task(service.run(actor, repo.record.id, lambda _: None))
    try:
        for _ in range(100):
            if calls and calls[-1] == "react":
                break
            await asyncio.sleep(0.01)
        assert calls[-1] == "react"
        await service.cancel(actor, repo.record.id)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert repo.failures == [("contract_review_failed", True)]
        assert repo.record.status == "cancelled"
        assert repo.failure_manifest is not None
        assert len(repo.failure_manifest["model_calls"]) >= 2
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await service.aclose()


@pytest.mark.asyncio
async def test_two_call_model_sends_all_images_each_time_and_forbids_third_call():
    from types import SimpleNamespace

    from lawyer_agent.application.contract_review.contracts import ReviewError, RunLimits
    from lawyer_agent.domain.model_gateway import ChatImage, ChatMessage
    from lawyer_agent.infrastructure.contract_review.runtime import _BoundedModel, _Usage
    from tests.unit.test_contract_review_pdf import _scope

    received = []

    class Gateway:
        model_ref = "synthetic"

        async def chat(self, messages):
            received.append(messages)
            return "{}"

    async def require(*args):
        pass

    bounded = _BoundedModel(Gateway(), SimpleNamespace(scope=_scope(), require=require),
                            RunLimits(), _Usage(), max_calls=2)
    image = ChatImage(data=b"\xff\xd8\xffsynthetic", media_type="image/jpeg")
    bounded.images = (image,) * 23
    await bounded.chat([ChatMessage(role="user", content="第一次总结")])
    await bounded.chat([ChatMessage(role="user", content="第二次审阅")])
    assert [len(messages[-1].images) for messages in received] == [23, 23]
    with pytest.raises(ReviewError, match="step_limit_exceeded"):
        await bounded.chat([ChatMessage(role="user", content="不允许第三次")])
