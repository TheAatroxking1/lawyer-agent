from __future__ import annotations

import io
import json
from collections.abc import Sequence

import pytest
from PIL import Image

from lawyer_agent.application.contract_review.contracts import ReviewError, VersionInput
from lawyer_agent.domain.model_gateway import ChatMessage
from lawyer_agent.infrastructure.contract_review.vision import VisionPdfDocumentService
from tests.unit.test_contract_review_pdf import RecordingAuthorizer, _one_page_pdf, _scope


class VisionModel:
    def __init__(self, pages: list[dict[str, object]] | None = None) -> None:
        self.messages: list[ChatMessage] = []
        self.pages = pages

    async def chat(self, messages: Sequence[ChatMessage]) -> str:
        self.messages = list(messages)
        return json.dumps(
            {
                "pages": self.pages
                if self.pages is not None
                else [
                    {
                        "page": 1,
                        "readable": True,
                        "native_text_complete": True,
                        "text": "Alpha beta",
                        "observations": "A single text line.",
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_prepare_only_renders_images_without_separate_model_call():
    model = VisionModel()
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model, prepare_only=True,
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert not model.messages
    assert len(service.page_images) == 1
    assert service._result.blocks[0].text == "Alpha beta"
    await service.register_visual_quote(_scope(), 1, "仅图片可见文字")
    block = service._result.blocks[-1]
    assert block.quality == "needs_review" and not block.anchors
    assert block.text == "仅图片可见文字"


@pytest.mark.asyncio
async def test_vision_receives_page_image_without_table_detection_or_ocr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("legacy document analysis must not run")

    monkeypatch.setattr("pdfplumber.page.Page.find_tables", forbidden)
    monkeypatch.setattr(
        "lawyer_agent.infrastructure.contract_review.pdf._load_rapid_ocr", forbidden
    )
    model = VisionModel()
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert model.messages[-1].images[0].media_type == "image/jpeg"
    assert "Alpha beta" in model.messages[-1].content
    assert service.page_dimensions == ((200.0, 200.0),)
    assert "A single text line." in service.visual_notes
    assert service._result is not None
    assert service._result.recognition_complete
    assert service._result.blocks[0].text == "Alpha beta"
    assert service._result.blocks[0].quality == "verified"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pages",
    [
        [],
        [
            {
                "page": 2,
                "readable": True,
                "native_text_complete": True,
                "text": "x",
                "observations": "",
            },
        ],
    ],
    ids=["missing-page", "wrong-page"],
)
async def test_vision_rejects_missing_or_invented_pages(pages: list[dict[str, object]]) -> None:
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=VisionModel(pages)
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert service.verification_report.pages[0].page == 1
    assert "visual_read_failed" in service.verification_report.pages[0].reasons
    assert not service._result.recognition_complete
    assert all(p["page"] == 1 for p in json.loads(service.visual_notes)["visual_page_readings"])


@pytest.mark.asyncio
async def test_vision_unmatched_text_is_not_published_as_verified_native_text() -> None:
    model = VisionModel(
        [
            {
                "page": 1,
                "readable": True,
                "native_text_complete": False,
                "text": "Additional image text",
                "observations": "image-only clause",
            }
        ]
    )
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert service._result is not None
    assert not service._result.recognition_complete
    assert all(b.text != "Additional image text" for b in service._result.blocks
               if b.quality == "verified")
    supplemental = [b for b in service._result.blocks if b.quality == "needs_review"]
    assert supplemental and supplemental[0].text == "Additional image text"
    assert not supplemental[0].anchors


@pytest.mark.asyncio
async def test_scan_cannot_gain_verified_coordinates_from_model_claim() -> None:
    output = io.BytesIO()
    with Image.new("RGB", (200, 200), "white") as image:
        image.save(output, format="PDF")
    model = VisionModel()  # Even an incorrectly optimistic model cannot supply glyphs.
    service = VisionPdfDocumentService(
        _scope(), output.getvalue(), RecordingAuthorizer(), model=model
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert service._result is not None
    assert not service._result.recognition_complete
    assert all(block.quality == "needs_review" for block in service._result.blocks)
    assert all(not block.anchors for block in service._result.blocks)
    assert service._result.blocks[0].text == "Alpha beta"


@pytest.mark.asyncio
async def test_vision_rejects_cross_tenant_before_model_call() -> None:
    from uuid import UUID

    model = VisionModel()
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model
    )
    other_scope = _scope().model_copy(update={"tenant_id": UUID(int=999)})
    with pytest.raises(ReviewError, match="scope_mismatch"):
        await service.parse(other_scope, VersionInput(document_version_id="version-1"))
    assert model.messages == []


@pytest.mark.asyncio
async def test_vision_batches_pages_and_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    from lawyer_agent.domain.model_gateway import ChatImage
    from lawyer_agent.infrastructure.contract_review.pdf import _ParsedPdf

    batches: list[list[int]] = []

    class BatchModel:
        async def chat(self, messages: Sequence[ChatMessage]) -> str:
            numbers = [item["page"] for item in json.loads(messages[-1].content)["pages"]]
            assert len(messages[-1].images) == len(numbers)
            batches.append(numbers)
            return json.dumps({"pages": [
                {"page": n, "readable": True, "native_text_complete": True,
                 "text": "synthetic", "observations": ""} for n in numbers
            ]})

    parsed = _ParsedPdf(
        (), ((200.0, 200.0),) * 5, True,
        (ChatImage(data=b"\xff\xd8\xffsynthetic", media_type="image/jpeg"),) * 5,
        ("synthetic",) * 5,
    )
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=BatchModel()
    )
    monkeypatch.setattr(service, "_parse_sync", lambda: parsed)
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert batches == [[1, 2, 3, 4], [5]]
    assert service._result is not None and service._result.page_images == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Alpha  beta", "Alpha\nbeta"])
async def test_whitespace_only_coverage_disagreement_is_checked_against_native(text: str) -> None:
    model = VisionModel([{
        "page": 1, "readable": True, "native_text_complete": False,
        "text": text, "observations": "PRIVATE_MODEL_OBSERVATION",
        "discrepancies": [{"kind": "formatting", "quote": "Alpha beta"}],
    }])
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model,
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert service._result is not None and service._result.recognition_complete
    assert service.verification_report.pages == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,readable,complete,discrepancies,code",
    [
        ("Alpha beta EXTRA", True, False,
         [{"kind": "formatting", "quote": "EXTRA"}], "text_mismatch"),
        ("Alpha beta", False, True, [], "unreadable"),
        ("Alpha beta", True, False, [], "unconfirmed_coverage"),
        ("Alpha beta", True, True,
         [{"kind": "missing_text", "quote": "PRIVATE_MISSING_TEXT"}], "missing_text"),
        ("Alpha beta", True, False,
         [{"kind": "garbled_text", "quote": "PRIVATE_GARBLED_TEXT"}], "garbled_text"),
    ],
)
async def test_coverage_failures_keep_only_page_and_safe_reason(
    text, readable, complete, discrepancies, code,
) -> None:
    model = VisionModel([{
        "page": 1, "readable": readable, "native_text_complete": complete,
        "text": text, "observations": "PRIVATE_MODEL_OBSERVATION",
        "discrepancies": discrepancies,
    }])
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model,
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert service._result is not None and not service._result.recognition_complete
    report = service.verification_report.model_dump(mode="json")
    assert report == {"pages": [{"page": 1, "reasons": [code]}]}
    assert "PRIVATE" not in json.dumps(report)


@pytest.mark.parametrize(
    "native,visual,expected",
    [
        ("租金1000元\n不得转租", "不得转租\n租金1000元", True),
        ("租金1000元\n不得转租", "得转租\n租金1000元", False),
        ("租金1000元", "租金100元", False),
        ("租金1000元", "租金1000万元", False),
        ("甲方付100元\n乙方付200元", "甲方付200元\n乙方付100元", False),
        ("支付100元\n支付100元", "支付100元", False),
        ("甲、乙", "甲乙", False),
        ("", "", False),
    ],
)
def test_text_coverage_preserves_numbers_negations_punctuation_and_repeated_lines(
    native: str, visual: str, expected: bool,
) -> None:
    from lawyer_agent.infrastructure.contract_review.vision import _same_text_coverage

    assert _same_text_coverage(native, visual) is expected


def test_native_coverage_text_interleaves_font_runs_without_changing_anchor_blocks() -> None:
    import pdfplumber

    from lawyer_agent.infrastructure.contract_review.vision import _native_coverage_text

    # A filled value is drawn slightly above the printed separators, as in PDF forms.
    chars = [
        {"text": text, "x0": x, "x1": x + 6, "top": top, "bottom": top + 10,
         "doctop": top, "upright": True}
        for text, x, top in [("2", 10, 10), ("0", 16, 10), ("3", 22, 10), ("0", 28, 10),
                             ("/", 36, 14), ("1", 44, 12), ("/", 52, 14), ("2", 60, 10)]
    ]
    # pdfplumber's native glyph reader uses word geometry; no OCR or table inference.
    expected = pdfplumber.utils.extract_text(chars, x_tolerance=2, y_tolerance=3)
    assert "".join(expected.split()) == "2030/1/2"
    original = "203012\n//"
    assert _native_coverage_text(expected, original) == expected
    assert _native_coverage_text("2030/1", original) == original  # Lost glyphs cannot pass.
    assert _native_coverage_text("2030/1/2EXTRA", original) == original


@pytest.mark.asyncio
async def test_page_reader_supplies_geometry_order_but_preserves_canonical_quotes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Purely reordered native text is for coverage, never a replacement anchor source.
    monkeypatch.setattr("pdfplumber.page.Page.extract_text", lambda *a, **kw: "beta\nAlpha")
    model = VisionModel([{
        "page": 1, "readable": True, "native_text_complete": False,
        "text": "Alpha\nbeta", "observations": "",
        "discrepancies": [{"kind": "reading_order", "quote": "Alpha beta"}],
    }])
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model,
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert json.loads(model.messages[-1].content)["pages"][0]["native_text"] == "beta\nAlpha"
    assert service._result is not None and service._result.recognition_complete
    assert service._result.blocks[0].text == "Alpha beta"


@pytest.mark.asyncio
async def test_reported_missing_quote_already_present_once_is_not_a_missing_text_failure() -> None:
    model = VisionModel([{
        "page": 1, "readable": True, "native_text_complete": False,
        "text": "Alpha beta", "observations": "",
        "discrepancies": [{"kind": "missing_text", "quote": "Alpha beta"}],
    }])
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=model,
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert service._result is not None and service._result.recognition_complete
    assert service.verification_report.pages == ()


@pytest.mark.parametrize(
    "native,visual,quote",
    [("Alpha beta Alpha beta", "Alpha beta", "Alpha beta"),
     ("Alpha beta", "Alpha beta Alpha beta", "Alpha beta"),
     ("Alpha beta", "Something else", "Alpha beta"),
     ("Alpha beta", "Alpha beta", "a"),
     ("Alpha beta", "Alpha beta", "   "),
     ("租金1000元", "租金10000元", "租金10000元"),
     ("可转租", "不可转租", "不可转租")],
)
def test_missing_text_disagreement_cannot_clear_ambiguous_or_changed_quotes(native, visual, quote):
    from lawyer_agent.infrastructure.contract_review.vision import _PageReading, _verification_issue

    page = _PageReading(page=1, readable=True, native_text_complete=False, text=visual,
                        observations="", discrepancies=[{"kind": "missing_text", "quote": quote}])
    issue = _verification_issue(page, native)
    assert issue is not None and issue.reasons == ("missing_text",)


@pytest.mark.asyncio
@pytest.mark.parametrize("still_missing", [False, True])
async def test_placeholder_disagreement_gets_one_image_recheck_not_automatic_acceptance(
    still_missing: bool,
) -> None:
    requests: list[list[ChatMessage]] = []

    class RecheckModel:
        async def chat(self, messages: Sequence[ChatMessage]) -> str:
            requests.append(list(messages))
            if len(requests) == 1:
                return json.dumps({"pages": [{
                    "page": 1, "readable": True, "native_text_complete": False,
                    "text": "Alpha beta ____", "observations": "",
                    "discrepancies": [{"kind": "missing_text", "quote": "Alpha beta ____"}],
                }]})
            if still_missing:
                return json.dumps({"pages": [{
                    "page": 1, "readable": True, "native_text_complete": False,
                    "text": "Alpha beta 100", "observations": "A filled number is missing.",
                    "discrepancies": [{"kind": "missing_text", "quote": "beta 100"}],
                }]})
            return json.dumps({"pages": [{
                "page": 1, "readable": True, "native_text_complete": True,
                "text": "Alpha beta", "observations": "The extra line was not text.",
                "discrepancies": [],
            }]})

    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=RecheckModel(),
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert len(requests) == 2  # Still failing cannot cause a third model call.
    assert requests[1][-1].images == requests[0][-1].images
    assert "Alpha beta ____" in requests[1][-1].content
    assert service._result is not None
    assert service._result.recognition_complete is (not still_missing)
    assert service._result.blocks[0].text == "Alpha beta"
    if still_missing:
        assert service.verification_report.pages[0].reasons == ("missing_text",)
    else:
        assert "extra line was not text" in service.visual_notes
        assert "Alpha beta ____" not in service.visual_notes


@pytest.mark.asyncio
async def test_placeholder_recheck_is_limited_to_four_flagged_pages_in_one_extra_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lawyer_agent.domain.model_gateway import ChatImage
    from lawyer_agent.infrastructure.contract_review.pdf import _ParsedPdf

    batches = []

    class RecheckModel:
        async def chat(self, messages: Sequence[ChatMessage]) -> str:
            pages = [item["page"] for item in json.loads(messages[-1].content)["pages"]]
            batches.append(pages)
            repaired = len(batches) == 3
            return json.dumps({"pages": [{
                "page": n, "readable": True, "native_text_complete": repaired,
                "text": "Alpha beta", "observations": "",
                "discrepancies": [] if repaired else [
                    {"kind": "missing_text", "quote": "Alpha ____ beta"},
                ],
            } for n in pages]})

    parsed = _ParsedPdf(
        (), ((200.0, 200.0),) * 5, True,
        (ChatImage(data=b"\xff\xd8\xffsynthetic", media_type="image/jpeg"),) * 5,
        ("Alpha beta",) * 5,
    )
    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=RecheckModel(),
    )
    monkeypatch.setattr(service, "_parse_sync", lambda: parsed)
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert batches == [[1, 2, 3, 4], [5], [1, 2, 3, 4]]
    assert service._result is not None and not service._result.recognition_complete
    assert [p.page for p in service.verification_report.pages] == [5]


@pytest.mark.asyncio
async def test_placeholder_recheck_rejects_wrong_page_instead_of_releasing_results() -> None:
    calls = 0

    class WrongPageModel:
        async def chat(self, messages: Sequence[ChatMessage]) -> str:
            nonlocal calls
            calls += 1
            return json.dumps({"pages": [{
                "page": calls, "readable": True, "native_text_complete": calls > 1,
                "text": "Alpha beta", "observations": "",
                "discrepancies": [{"kind": "missing_text", "quote": "Alpha ____ beta"}],
            }]})

    service = VisionPdfDocumentService(
        _scope(), _one_page_pdf(), RecordingAuthorizer(), model=WrongPageModel(),
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    assert "visual_read_failed" in service.verification_report.pages[0].reasons
    assert not service._result.recognition_complete
    assert all(p["page"] == 1 for p in json.loads(service.visual_notes)["visual_page_readings"])
    assert calls == 2 and service._result is not None
    assert service._result.blocks[0].text == "Alpha beta"
    assert service._result.blocks[0].quality == "verified"
