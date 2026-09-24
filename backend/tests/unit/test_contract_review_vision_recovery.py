from __future__ import annotations

import asyncio
import io
import json

import pytest
from PIL import Image

from lawyer_agent.application.contract_review.contracts import ReviewError, VersionInput
from lawyer_agent.domain.model_gateway import ChatImage
from lawyer_agent.infrastructure.contract_review.pdf import _ParsedPdf
from lawyer_agent.infrastructure.contract_review.vision import VisionPdfDocumentService
from tests.unit.test_contract_review_pdf import RecordingAuthorizer, _one_page_pdf, _scope


def parsed(texts):
    output = io.BytesIO()
    with Image.new("RGB", (80, 100), "white") as image:
        image.save(output, format="JPEG")
    return _ParsedPdf(
        (),
        ((80.0, 100.0),) * len(texts),
        bool(all(texts)),
        (ChatImage(data=output.getvalue(), media_type="image/jpeg"),) * len(texts),
        tuple(texts),
    )


def reply(messages, text=""):
    pages = json.loads(messages[-1].content)["pages"]
    return json.dumps(
        {
            "pages": [
                {
                    "page": p["page"],
                    "readable": True,
                    "native_text_complete": bool(p["native_text"]),
                    "text": text,
                    "observations": "表格按行对应",
                    "discrepancies": [],
                }
                for p in pages
            ]
        }
    )


async def run(monkeypatch, model, texts, fallback=None):
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model, nonstream_chat=fallback
    )
    monkeypatch.setattr(service, "_parse_sync", lambda: parsed(texts))
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    return service


@pytest.mark.asyncio
async def test_native_mode_returns_only_supplement_and_keeps_original(monkeypatch):
    class Model:
        async def chat(self, messages):
            request = json.loads(messages[-1].content)
            assert request["pages"][0]["mode"] == "verify_native"
            assert "不要重复抄写" in messages[0].content
            return reply(messages)

    service = await run(monkeypatch, Model(), ["付款总额\n付款总额"])
    assert service.verification_report.pages == ()
    notes = json.loads(service.visual_notes)["visual_page_readings"]
    assert notes[0]["text"] == ""


@pytest.mark.asyncio
async def test_batch_failure_splits_and_only_json_error_retries_nonstream(monkeypatch):
    calls = []

    class Model:
        async def chat(self, messages):
            numbers = [p["page"] for p in json.loads(messages[-1].content)["pages"]]
            calls.append(("stream", numbers))
            if len(numbers) > 1 or numbers == [2]:
                raise ReviewError("model_json_generation_failed")
            return reply(messages)

    async def fallback(messages):
        calls.append(("nonstream", [p["page"] for p in json.loads(messages[-1].content)["pages"]]))
        return reply(messages)

    service = await run(monkeypatch, Model(), ["a", "b", "c"], fallback)
    assert calls == [
        ("stream", [1, 2, 3]),
        ("stream", [1]),
        ("stream", [2]),
        ("nonstream", [2]),
        ("stream", [3]),
    ]
    assert service.verification_report.pages == ()


@pytest.mark.asyncio
async def test_exhausted_page_warns_and_later_page_is_read(monkeypatch):
    class Model:
        async def chat(self, messages):
            numbers = [p["page"] for p in json.loads(messages[-1].content)["pages"]]
            if len(numbers) > 1 or numbers == [1]:
                raise ReviewError("model_json_generation_failed")
            return reply(messages)

    fallback_calls = []

    async def fallback(messages):
        fallback_calls.append(messages)
        raise ReviewError("model_json_generation_failed")

    service = await run(monkeypatch, Model(), ["original", "later"], fallback)
    assert len(fallback_calls) == 1
    assert [(p.page, p.reasons) for p in service.verification_report.pages] == [
        (1, ("visual_read_failed",))
    ]
    assert not service._result.recognition_complete
    assert json.loads(service.visual_notes)["visual_page_readings"][1]["page"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["tool_not_allowed", "scope_mismatch", "step_limit_exceeded", "model_provider_unavailable"],
)
async def test_security_budget_and_configuration_failures_are_not_swallowed(monkeypatch, code):
    class Model:
        async def chat(self, messages):
            raise ReviewError(code)

    with pytest.raises(ReviewError, match=code):
        await run(monkeypatch, Model(), ["native"])


@pytest.mark.asyncio
async def test_cancellation_never_becomes_page_warning(monkeypatch):
    class Model:
        async def chat(self, messages):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run(monkeypatch, Model(), ["native"])


@pytest.mark.asyncio
async def test_scan_failure_uses_two_regions_once_and_marks_boundary_uncertain(monkeypatch):
    calls = []

    class Model:
        async def chat(self, messages):
            request = json.loads(messages[-1].content)
            calls.append(request)
            assert len(request["pages"]) == 1
            if "region" not in request:
                raise ReviewError("model_provider_invalid_response")
            return reply(messages, text="upper" if request["region"] == "top" else "lower")

    service = await run(monkeypatch, Model(), [""])
    assert len(calls) == 3
    assert [r.get("region") for r in calls] == [None, "top", "bottom"]
    assert "upper\nlower" in service.visual_notes.replace("\\n", "\n")
    assert "unconfirmed_coverage" in service.verification_report.pages[0].reasons
    assert all(not block.anchors for block in service._result.blocks)


@pytest.mark.asyncio
async def test_failed_scan_is_not_reported_as_blank_or_complete(monkeypatch):
    class Model:
        async def chat(self, messages):
            raise ReviewError("model_provider_invalid_response")

    service = await run(monkeypatch, Model(), [""])
    assert "visual_read_failed" in service.verification_report.pages[0].reasons
    assert "未完成" in service._result.blocks[0].text
    assert not service._result.recognition_complete


@pytest.mark.asyncio
async def test_scan_regions_share_one_nonstream_attempt_per_page(monkeypatch):
    class Model:
        async def chat(self, messages):
            raise ReviewError("model_json_generation_failed")

    calls = []

    async def fallback(messages):
        calls.append(messages)
        raise ReviewError("model_json_generation_failed")

    service = await run(monkeypatch, Model(), [""], fallback)
    assert len(calls) == 1
    assert "visual_read_failed" in service.verification_report.pages[0].reasons


@pytest.mark.asyncio
async def test_recheck_failure_preserves_prior_supplement(monkeypatch):
    calls = 0

    class Model:
        async def chat(self, messages):
            nonlocal calls
            calls += 1
            if calls > 1:
                raise ReviewError("model_provider_invalid_response")
            result = json.loads(reply(messages, text="新增可见条款"))
            result["pages"][0].update(
                native_text_complete=False,
                discrepancies=[{"kind": "missing_text", "quote": "新增____条款"}],
            )
            return json.dumps(result)

    service = await run(monkeypatch, Model(), ["native"])
    assert calls == 2
    assert service._result.blocks[0].text == "新增可见条款"
    assert "visual_read_failed" in service.verification_report.pages[0].reasons


@pytest.mark.asyncio
async def test_scan_merge_preserves_region_relationships_and_discrepancies(monkeypatch):
    class Model:
        async def chat(self, messages):
            request = json.loads(messages[-1].content)
            if "region" not in request:
                raise ReviewError("model_provider_invalid_response")
            result = json.loads(reply(messages, text=request["region"]))
            result["pages"][0].update(
                observations=request["region"] + "表格对应关系",
                discrepancies=[{"kind": "missing_text", "quote": request["region"] + "具体缺漏"}],
            )
            return json.dumps(result)

    service = await run(monkeypatch, Model(), [""])
    for expected in ("top表格对应关系", "bottom表格对应关系", "top具体缺漏", "bottom具体缺漏"):
        assert expected in service.visual_notes


@pytest.mark.asyncio
async def test_scan_region_text_limit_preserves_both_regions_and_separator(monkeypatch):
    class Model:
        async def chat(self, messages):
            request = json.loads(messages[-1].content)
            if "region" not in request:
                raise ReviewError("model_provider_invalid_response")
            return reply(messages, text=("a" if request["region"] == "top" else "b") * 32000)
    service = await run(monkeypatch, Model(), [""])
    assert "".join(b.text for b in service._result.blocks) == "a" * 32000 + "\n" + "b" * 32000
    assert all(len(b.text) <= 32000 and not b.anchors for b in service._result.blocks)


@pytest.mark.asyncio
async def test_native_supplement_is_retained_even_if_model_claims_complete(monkeypatch):
    class Model:
        async def chat(self, messages):
            return reply(messages, text="额外图片条款")
    service = await run(monkeypatch, Model(), ["原生正文"])
    assert service._result.blocks[0].text == "额外图片条款"
    assert "unconfirmed_coverage" in service.verification_report.pages[0].reasons


@pytest.mark.asyncio
async def test_empty_supplement_cannot_be_compared_as_full_transcription(monkeypatch):
    class Model:
        async def chat(self, messages):
            result = json.loads(reply(messages))
            result["pages"][0].update(native_text_complete=False,
                discrepancies=[{"kind":"formatting", "quote":"原生正文"}])
            return json.dumps(result)
    service = await run(monkeypatch, Model(), ["原生正文"])
    assert service.verification_report.pages[0].reasons == ("unconfirmed_coverage",)
