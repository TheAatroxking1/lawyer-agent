from __future__ import annotations

import asyncio
import io
import threading
from collections import Counter
from uuid import UUID

import pytest
from PIL import Image, ImageDraw, ImageFont

from lawyer_agent.application.contract_review.contracts import (
    Anchor,
    DocumentBlock,
    JobInput,
    ReadBlocksInput,
    RegionInput,
    ReviewError,
    ReviewScope,
    StructureInput,
    VersionInput,
)
from lawyer_agent.infrastructure.contract_review.pdf import (
    PdfDocumentService,
    _image_regions,
    _load_rapid_ocr,
    _merge_mixed_blocks,
    _normalise_ocr_rows,
    _validate_page_geometry,
)


def test_installed_rapidocr_loader_recognizes_synthetic_scan() -> None:
    glyphs = Image.new("RGB", (100, 25), "white")
    ImageDraw.Draw(glyphs).text(
        (4, 5), "CONTRACT 123", font=ImageFont.load_default(), fill="black"
    )
    image = glyphs.resize((800, 200))

    engine = _load_rapid_ocr()

    assert engine is not None
    rows = _normalise_ocr_rows(engine(image))
    assert rows
    assert "CONTRACT" in " ".join(row[1] for row in rows).upper()


@pytest.mark.asyncio
async def test_synthetic_scanned_pdf_uses_installed_ocr_adapter() -> None:
    glyphs = Image.new("RGB", (100, 25), "white")
    ImageDraw.Draw(glyphs).text(
        (4, 5), "CONTRACT 123", font=ImageFont.load_default(), fill="black"
    )
    image = glyphs.resize((800, 200))
    output = io.BytesIO()
    image.save(output, format="PDF", resolution=144)
    service = PdfDocumentService(_scope(), output.getvalue(), RecordingAuthorizer())
    try:
        job = await service.parse(
            _scope(), VersionInput(document_version_id="version-1")
        )
        structure = await service.structure(
            _scope(), StructureInput(document_version_id="version-1")
        )
        blocks = await service.read_blocks(
            _scope(),
            ReadBlocksInput(
                document_version_id="version-1", block_ids=structure.block_ids
            ),
        )
    finally:
        await service.aclose()

    assert job.status == "ready"
    assert structure.recognition_complete is True
    assert "CONTRACT" in " ".join(block.text for block in blocks.blocks).upper()
    assert all(block.kind == "image" for block in blocks.blocks)


def _scope(*, version: str = "version-1") -> ReviewScope:
    return ReviewScope(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        actor_id=UUID("00000000-0000-0000-0000-000000000002"),
        run_id=UUID("00000000-0000-0000-0000-000000000003"),
        document_version_id=version,
    )


class RecordingAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[ReviewScope, str, str]] = []

    async def require(self, scope: ReviewScope, action: str, resource_id: str) -> None:
        self.calls.append((scope, action, resource_id))


def _one_page_pdf(text: str = "Alpha beta") -> bytes:
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 20 160 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    result = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode())
        result.extend(body)
        result.extend(b"\nendobj\n")
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


@pytest.mark.asyncio
async def test_native_text_becomes_exact_line_block_with_top_left_anchor() -> None:
    authorizer = RecordingAuthorizer()
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=authorizer
    )

    job = await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    structure = await service.structure(
        _scope(), StructureInput(document_version_id="version-1")
    )
    blocks = await service.read_blocks(
        _scope(),
        ReadBlocksInput(
            document_version_id="version-1", block_ids=structure.block_ids
        ),
    )

    assert job.status == "ready"
    assert len(structure.block_ids) == 1
    assert structure.recognition_complete is True
    assert blocks.blocks[0].text == "Alpha beta"
    assert blocks.blocks[0].quality == "verified"
    anchor = blocks.blocks[0].anchors[0]
    assert blocks.blocks[0].text[anchor.start : anchor.end] == "Alpha beta"
    assert anchor.page == 1
    assert anchor.quad[1] < anchor.quad[5]
    assert service.page_dimensions == ((200.0, 200.0),)
    assert [call[1] for call in authorizer.calls] == [
        "document_parse",
        "document_get_structure",
        "document_read_blocks",
    ]


@pytest.mark.asyncio
async def test_parse_is_idempotent_and_job_is_bound_to_run_and_version() -> None:
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    request = VersionInput(document_version_id="version-1")
    first = await service.parse(_scope(), request)
    second = await service.parse(_scope(), request)
    assert first == second
    assert await service.job_status(_scope(), JobInput(job_id=first.job_id)) == first

    with pytest.raises(ReviewError, match="scope_mismatch"):
        await service.parse(_scope(version="other"), request)
    with pytest.raises(ReviewError, match="job_not_found"):
        await service.job_status(_scope(), JobInput(job_id="other-job"))


@pytest.mark.asyncio
async def test_read_rejects_unknown_or_cross_version_block_ids() -> None:
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    with pytest.raises(ReviewError, match="block_not_found"):
        await service.read_blocks(
            _scope(),
            ReadBlocksInput(document_version_id="version-1", block_ids=("block-unknown",)),
        )


@pytest.mark.asyncio
async def test_inspect_region_is_explicitly_unsupported() -> None:
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    with pytest.raises(ReviewError, match="region_inspection_unsupported"):
        await service.inspect_region(
            _scope(), RegionInput(document_version_id="version-1", region_id="region-1")
        )


def test_constructor_rejects_oversized_or_non_pdf_input() -> None:
    with pytest.raises(ReviewError, match="document_too_large"):
        PdfDocumentService(
            trusted_scope=_scope(),
            pdf_bytes=b"%PDF-" + b"x" * (10 * 1024 * 1024),
            authorizer=RecordingAuthorizer(),
        )


@pytest.mark.parametrize(
    ("width", "height", "box"),
    [
        (0, 200, (0, 0, 0, 200)),
        (float("nan"), 200, (0, 0, float("nan"), 200)),
        (4001, 100, (0, 0, 4001, 100)),
        (3000, 2000, (0, 0, 3000, 2000)),
        (200, 200, (10, 10, 210, 210)),
    ],
)
def test_page_geometry_rejects_invalid_dimensions_pixel_budget_and_nonzero_origin(
    width: float, height: float, box: tuple[float, float, float, float]
) -> None:
    page = type(
        "Page",
        (),
        {
            "rotation": 0,
            "cropbox": box,
            "mediabox": box,
            "width": width,
            "height": height,
        },
    )()
    with pytest.raises(ReviewError, match="page_geometry_unsupported"):
        _validate_page_geometry(page)


def test_overlapping_image_layers_are_merged_and_ambiguous_ocr_needs_review() -> None:
    assert _image_regions(
        [
            {"x0": 10, "top": 10, "x1": 80, "bottom": 80},
            {"x0": 40, "top": 40, "x1": 100, "bottom": 100},
        ],
        200,
        200,
    ) == [(10.0, 10.0, 100.0, 100.0)]
    native = DocumentBlock(
        block_id="native",
        kind="text",
        text="甲",
        anchors=(Anchor(anchor_id="n", page=1, start=0, end=1,
                        quad=(20, 20, 40, 20, 40, 40, 20, 40)),),
        quality="verified",
    )
    ocr = DocumentBlock(
        block_id="ocr",
        kind="image",
        text="乙",
        anchors=(Anchor(anchor_id="o", page=1, start=0, end=1,
                        quad=(25, 25, 45, 25, 45, 45, 25, 45)),),
        quality="verified",
    )
    merged, unambiguous = _merge_mixed_blocks(
        [native], [ocr], (10.0, 10.0, 100.0, 100.0)
    )
    assert merged[0].quality == "needs_review"
    assert unambiguous is False


@pytest.mark.asyncio
async def test_mixed_page_ocr_is_cropped_offset_and_deduplicates_native_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page:
        rotation = 0
        cropbox = (0, 0, 200, 200)
        mediabox = (0, 0, 200, 200)
        width = 200
        height = 200
        images = [{"x0": 10, "x1": 100, "top": 10, "bottom": 80}]
        chars = [
            {"text": value, "x0": 20 + index * 6, "x1": 26 + index * 6,
             "top": 20, "bottom": 30}
            for index, value in enumerate("Same")
        ]

        def find_tables(self) -> list[object]:
            return []

    class Pdf:
        pages = [Page()]

        def __enter__(self) -> Pdf:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    class Image:
        def __init__(self) -> None:
            self.crops: list[tuple[int, int, int, int]] = []

        def crop(self, box: tuple[int, int, int, int]) -> object:
            self.crops.append(box)
            return object()

    image = Image()
    monkeypatch.setattr("pdfplumber.open", lambda source: Pdf())
    monkeypatch.setattr(
        "lawyer_agent.infrastructure.contract_review.pdf._render_page",
        lambda pdf_bytes, page_index: image,
    )

    def engine(crop: object) -> list[tuple[object, str, float]]:
        del crop
        return [([[20, 20], [68, 20], [68, 40], [20, 40]], "Same", 0.99)]

    service = PdfDocumentService(
        trusted_scope=_scope(),
        pdf_bytes=_one_page_pdf(),
        authorizer=RecordingAuthorizer(),
        ocr_engine=engine,
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    structure = await service.structure(
        _scope(), StructureInput(document_version_id="version-1")
    )
    result = await service.read_blocks(
        _scope(), ReadBlocksInput(document_version_id="version-1", block_ids=structure.block_ids)
    )

    assert image.crops == [(20, 20, 200, 160)]
    assert [block.text for block in result.blocks] == ["Same"]
    assert result.blocks[0].anchors[0].quad == (20.0, 20.0, 44.0, 20.0, 44.0, 30.0, 20.0, 30.0)


@pytest.mark.asyncio
async def test_mixed_image_without_ocr_is_region_scoped_and_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page:
        rotation = 0
        cropbox = (0, 0, 200, 200)
        mediabox = (0, 0, 200, 200)
        width = 200
        height = 200
        images = [{"x0": 100, "x1": 150, "top": 100, "bottom": 140}]
        chars = [{"text": "A", "x0": 20, "x1": 28, "top": 20, "bottom": 30}]

        def find_tables(self) -> list[object]:
            return []

    class Pdf:
        pages = [Page()]

        def __enter__(self) -> Pdf:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    class Image:
        def crop(self, box: tuple[int, int, int, int]) -> object:
            return box

    monkeypatch.setattr("pdfplumber.open", lambda source: Pdf())
    monkeypatch.setattr(
        "lawyer_agent.infrastructure.contract_review.pdf._render_page",
        lambda pdf_bytes, page_index: Image(),
    )
    service = PdfDocumentService(
        trusted_scope=_scope(),
        pdf_bytes=_one_page_pdf(),
        authorizer=RecordingAuthorizer(),
        ocr_engine=lambda image: [],
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    structure = await service.structure(
        _scope(), StructureInput(document_version_id="version-1")
    )
    result = await service.read_blocks(
        _scope(), ReadBlocksInput(document_version_id="version-1", block_ids=structure.block_ids)
    )
    image_block = next(block for block in result.blocks if block.kind == "image")
    assert image_block.quality == "needs_review"
    assert image_block.anchors[0].quad == (
        100.0, 100.0, 150.0, 100.0, 150.0, 140.0, 100.0, 140.0
    )
    assert structure.recognition_complete is False
    with pytest.raises(ReviewError, match="document_invalid"):
        PdfDocumentService(
            trusted_scope=_scope(), pdf_bytes=b"not-pdf", authorizer=RecordingAuthorizer()
        )


@pytest.mark.asyncio
async def test_rotated_page_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    class Page:
        rotation = 90
        cropbox = (0, 0, 200, 200)
        mediabox = (0, 0, 200, 200)
        width = 200
        height = 200
        chars: list[object] = []
        images: list[object] = []

    class Pdf:
        pages = [Page()]

        def __enter__(self) -> Pdf:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("pdfplumber.open", lambda source: Pdf())
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    with pytest.raises(ReviewError, match="page_geometry_unsupported"):
        await service.parse(_scope(), VersionInput(document_version_id="version-1"))


@pytest.mark.asyncio
async def test_ocr_fallback_converts_render_pixels_to_page_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page:
        rotation = 0
        cropbox = (0, 0, 200, 200)
        mediabox = (0, 0, 200, 200)
        width = 200
        height = 200
        chars: list[object] = []
        images = [{"x0": 0}]

        def find_tables(self) -> list[object]:
            return []

    class Pdf:
        pages = [Page()]

        def __enter__(self) -> Pdf:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("pdfplumber.open", lambda source: Pdf())
    monkeypatch.setattr(
        "lawyer_agent.infrastructure.contract_review.pdf._render_page",
        lambda pdf_bytes, page_index: object(),
    )
    def engine(image: object) -> list[tuple[object, str, float]]:
        del image
        return [
            ([[40, 80], [120, 80], [120, 100], [40, 100]], "OCR text", 0.8)
        ]
    service = PdfDocumentService(
        trusted_scope=_scope(),
        pdf_bytes=_one_page_pdf(),
        authorizer=RecordingAuthorizer(),
        ocr_engine=engine,
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    structure = await service.structure(
        _scope(), StructureInput(document_version_id="version-1")
    )
    result = await service.read_blocks(
        _scope(),
        ReadBlocksInput(
            document_version_id="version-1", block_ids=structure.block_ids
        ),
    )
    assert result.blocks[0].text == "OCR text"
    assert result.blocks[0].quality == "needs_review"
    assert result.blocks[0].anchors[0].quad == (
        20.0,
        40.0,
        60.0,
        40.0,
        60.0,
        50.0,
        20.0,
        50.0,
    )
    assert structure.recognition_complete is False


@pytest.mark.asyncio
async def test_clear_two_by_two_table_assigns_each_native_char_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Row:
        def __init__(self, cells: list[tuple[int, int, int, int] | None]) -> None:
            self.cells = cells

    class Table:
        bbox = (10, 10, 190, 110)
        rows = [
            Row([(10, 10, 90, 60), (110, 10, 190, 60)]),
            Row([(10, 60, 100, 110), (100, 60, 190, 110)]),
        ]

    def chars(text: str, x: float, top: float) -> list[dict[str, object]]:
        return [
            {"text": value, "x0": x + index * 8, "x1": x + index * 8 + 7,
             "top": top, "bottom": top + 12}
            for index, value in enumerate(text)
        ]

    class Page:
        rotation = 0
        cropbox = (0, 0, 200, 200)
        mediabox = (0, 0, 200, 200)
        width = 200
        height = 200
        images: list[object] = []
        chars: list[dict[str, object]] = []

        def find_tables(self) -> list[Table]:
            return [Table()]

    Page.chars = (
        chars("名称", 20, 20)
        + chars("Name", 120, 20)
        + chars("中", 98, 40)
        + chars("金额", 20, 70)
        + chars("正文", 20, 140)
    )

    class Pdf:
        pages = [Page()]

        def __enter__(self) -> Pdf:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("pdfplumber.open", lambda source: Pdf())
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    await service.parse(_scope(), VersionInput(document_version_id="version-1"))
    structure = await service.structure(
        _scope(), StructureInput(document_version_id="version-1")
    )
    result = await service.read_blocks(
        _scope(),
        ReadBlocksInput(document_version_id="version-1", block_ids=structure.block_ids),
    )

    tables = [block for block in result.blocks if block.kind == "table"]
    body = [block for block in result.blocks if block.kind == "text"]
    assert len(tables) == 4
    assert [block.text for block in tables] == ["名称", "Name", "金额", ""]
    assert [(block.table_position.row, block.table_position.column) for block in tables] == [
        (0, 0), (0, 1), (1, 0), (1, 1)
    ]
    assert all(block.quality == "verified" for block in tables)
    assert [block.text for block in body] == ["中", "正文"]
    assert Counter("".join(block.text.replace("\n", "") for block in result.blocks)) == Counter(
        "名称Name中金额正文"
    )


@pytest.mark.asyncio
async def test_overlapping_table_cells_fail_closed_without_duplicate_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Row:
        cells = [(10, 10, 120, 60), (80, 10, 190, 60)]

    class Table:
        bbox = (10, 10, 190, 60)
        rows = [Row()]

    class Page:
        rotation = 0
        cropbox = (0, 0, 200, 200)
        mediabox = (0, 0, 200, 200)
        width = 200
        height = 200
        images: list[object] = []
        chars = [{"text": "X", "x0": 90, "x1": 98, "top": 20, "bottom": 32}]

        def find_tables(self) -> list[Table]:
            return [Table()]

    class Pdf:
        pages = [Page()]

        def __enter__(self) -> Pdf:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("pdfplumber.open", lambda source: Pdf())
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    with pytest.raises(ReviewError, match="table_geometry_ambiguous"):
        await service.parse(_scope(), VersionInput(document_version_id="version-1"))


@pytest.mark.asyncio
async def test_table_detection_failure_cannot_be_reported_as_verified_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Page:
        rotation = 0
        cropbox = (0, 0, 200, 200)
        mediabox = (0, 0, 200, 200)
        width = 200
        height = 200
        images: list[object] = []
        chars = [{"text": "X", "x0": 20, "x1": 28, "top": 20, "bottom": 32}]

        def find_tables(self) -> list[object]:
            raise RuntimeError("synthetic detector failure")

    class Pdf:
        pages = [Page()]

        def __enter__(self) -> Pdf:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr("pdfplumber.open", lambda source: Pdf())
    service = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    with pytest.raises(ReviewError, match="table_detection_failed"):
        await service.parse(_scope(), VersionInput(document_version_id="version-1"))


@pytest.mark.asyncio
async def test_cancelled_waiter_returns_but_capacity_remains_held_until_thread_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    call_count = 0
    real_parser = __import__(
        "lawyer_agent.infrastructure.contract_review.pdf", fromlist=["parse_pdf_bytes"]
    ).parse_pdf_bytes

    def slow_parser(pdf_bytes: bytes, ocr_engine: object) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            first_started.set()
            release_first.wait(2)
        else:
            second_started.set()
            release_second.wait(2)
        return real_parser(pdf_bytes, ocr_engine)

    monkeypatch.setattr(
        "lawyer_agent.infrastructure.contract_review.pdf.parse_pdf_bytes", slow_parser
    )
    first = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf(), authorizer=RecordingAuthorizer()
    )
    second = PdfDocumentService(
        trusted_scope=_scope(), pdf_bytes=_one_page_pdf("Second"), authorizer=RecordingAuthorizer()
    )
    request = VersionInput(document_version_id="version-1")
    first_waiter = asyncio.create_task(first.parse(_scope(), request))
    assert await asyncio.to_thread(first_started.wait, 1)
    first_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(first_waiter, 0.2)

    second_waiter = asyncio.create_task(second.parse(_scope(), request))
    await asyncio.sleep(0.05)
    assert not second_started.is_set()
    with pytest.raises(ReviewError, match="document_close_timeout"):
        await first.aclose(timeout_seconds=0.01)

    release_first.set()
    assert await asyncio.to_thread(second_started.wait, 1)
    release_second.set()
    assert (await second_waiter).status == "ready"
    await first.aclose()
    await second.aclose()
