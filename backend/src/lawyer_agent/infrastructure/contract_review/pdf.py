"""Bounded, authorization-aware PDF document service for contract review."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

import pdfplumber

from lawyer_agent.application.contract_review.contracts import (
    Anchor,
    BlocksResult,
    DocumentBlock,
    JobInput,
    JobResult,
    ReadBlocksInput,
    RegionInput,
    ReviewError,
    ReviewScope,
    StructureInput,
    StructureResult,
    TablePosition,
    VersionInput,
)
from lawyer_agent.application.contract_review.ports import Authorizer
from lawyer_agent.domain.model_gateway import ChatImage

_MAX_PDF_BYTES = 10 * 1024 * 1024
_MAX_PAGES = 30
_MAX_BLOCKS = 2000
_MAX_SERIALIZED_BYTES = 2 * 1024 * 1024
_OCR_RENDER_SCALE = 2.0
_MAX_PAGE_SIDE_POINTS = 4000.0
_MAX_OCR_PIXELS = 20_000_000
_PARSE_CAPACITY = asyncio.Semaphore(1)
_ACTIVE_PARSE_TASKS: set[asyncio.Task[_ParsedPdf]] = set()
_Box = tuple[float, float, float, float]
_TableRows = tuple[tuple[_Box, ...], ...]


class OcrEngine(Protocol):
    def __call__(self, image: object) -> object: ...


@dataclass(frozen=True, slots=True)
class _ParsedPdf:
    blocks: tuple[DocumentBlock, ...]
    page_dimensions: tuple[tuple[float, float], ...]
    recognition_complete: bool
    page_images: tuple[ChatImage, ...] = field(default=(), repr=False)
    native_page_texts: tuple[str, ...] = field(default=(), repr=False)


class PdfDocumentService:
    """In-memory adapter for one trusted review run and immutable PDF version."""

    def __init__(
        self,
        trusted_scope: ReviewScope,
        pdf_bytes: bytes,
        authorizer: Authorizer,
        *,
        ocr_engine: OcrEngine | None = None,
    ) -> None:
        if not isinstance(pdf_bytes, bytes) or not pdf_bytes.startswith(b"%PDF-"):
            raise ReviewError("document_invalid")
        if len(pdf_bytes) > _MAX_PDF_BYTES:
            raise ReviewError("document_too_large")
        self._scope = trusted_scope
        self._pdf_bytes = pdf_bytes
        self._authorizer = authorizer
        self._ocr_engine = ocr_engine
        digest = hashlib.sha256(pdf_bytes).hexdigest()[:24]
        run_part = trusted_scope.run_id.hex[:16]
        self._job_id = f"pdf-{run_part}-{digest}"
        self._result: _ParsedPdf | None = None
        self._parse_lock = asyncio.Lock()
        self._owned_tasks: set[asyncio.Task[_ParsedPdf]] = set()

    @property
    def page_dimensions(self) -> tuple[tuple[float, float], ...]:
        return () if self._result is None else self._result.page_dimensions

    async def parse(self, scope: ReviewScope, request: VersionInput) -> JobResult:
        self._require_scope_and_version(scope, request.document_version_id)
        await self._authorizer.require(scope, "document_parse", request.document_version_id)
        async with self._parse_lock:
            if self._result is None:
                self._result = await self._run_parse()
        return self._job_result("ready")

    async def job_status(self, scope: ReviewScope, request: JobInput) -> JobResult:
        self._require_scope(scope)
        await self._authorizer.require(scope, "document_job_status", request.job_id)
        if request.job_id != self._job_id:
            raise ReviewError("job_not_found")
        return self._job_result("ready" if self._result is not None else "queued")

    async def structure(
        self, scope: ReviewScope, request: StructureInput
    ) -> StructureResult:
        self._require_scope_and_version(scope, request.document_version_id)
        await self._authorizer.require(
            scope, "document_get_structure", request.document_version_id
        )
        result = self._require_parsed()
        offset = _decode_cursor(request.cursor)
        block_ids = tuple(block.block_id for block in result.blocks[offset : offset + 200])
        next_offset = offset + len(block_ids)
        return StructureResult(
            document_version_id=request.document_version_id,
            block_ids=block_ids,
            next_cursor=(
                f"cursor-{next_offset}" if next_offset < len(result.blocks) else None
            ),
            recognition_complete=result.recognition_complete,
        )

    async def read_blocks(
        self, scope: ReviewScope, request: ReadBlocksInput
    ) -> BlocksResult:
        self._require_scope_and_version(scope, request.document_version_id)
        await self._authorizer.require(
            scope, "document_read_blocks", request.document_version_id
        )
        result = self._require_parsed()
        by_id = {block.block_id: block for block in result.blocks}
        if any(block_id not in by_id for block_id in request.block_ids):
            raise ReviewError("block_not_found")
        return BlocksResult(
            document_version_id=request.document_version_id,
            blocks=tuple(by_id[block_id] for block_id in request.block_ids),
        )

    async def inspect_region(
        self, scope: ReviewScope, request: RegionInput
    ) -> JobResult:
        self._require_scope_and_version(scope, request.document_version_id)
        await self._authorizer.require(
            scope, "document_inspect_region", request.region_id
        )
        raise ReviewError("region_inspection_unsupported")

    async def aclose(self, timeout_seconds: float = 5.0) -> None:
        """Wait for owned parser work; a timeout never pretends the thread stopped."""
        if not self._owned_tasks:
            return
        done, pending = await asyncio.wait(
            tuple(self._owned_tasks), timeout=timeout_seconds
        )
        del done
        if pending:
            raise ReviewError("document_close_timeout")

    async def _run_parse(self) -> _ParsedPdf:
        await _PARSE_CAPACITY.acquire()
        try:
            task = asyncio.create_task(
                asyncio.to_thread(self._parse_sync)
            )
        except BaseException:
            _PARSE_CAPACITY.release()
            raise
        self._owned_tasks.add(task)
        _ACTIVE_PARSE_TASKS.add(task)

        def release_when_worker_really_finishes(done: asyncio.Task[_ParsedPdf]) -> None:
            self._owned_tasks.discard(done)
            _ACTIVE_PARSE_TASKS.discard(done)
            _PARSE_CAPACITY.release()
            if done.cancelled():
                return
            done.exception()

        task.add_done_callback(release_when_worker_really_finishes)
        # Cancelling this waiter leaves the real worker and its capacity slot intact.
        return await asyncio.shield(task)

    def _parse_sync(self) -> _ParsedPdf:
        return parse_pdf_bytes(self._pdf_bytes, self._ocr_engine)

    def _require_scope(self, scope: ReviewScope) -> None:
        if scope != self._scope:
            raise ReviewError("scope_mismatch")

    def _require_scope_and_version(self, scope: ReviewScope, version_id: str) -> None:
        self._require_scope(scope)
        if version_id != self._scope.document_version_id:
            raise ReviewError("document_version_mismatch")

    def _require_parsed(self) -> _ParsedPdf:
        if self._result is None:
            raise ReviewError("document_not_parsed")
        return self._result

    def _job_result(self, status: str) -> JobResult:
        block_ids: tuple[str, ...] = ()
        if self._result is not None:
            block_ids = tuple(block.block_id for block in self._result.blocks[:20])
        return JobResult(
            document_version_id=self._scope.document_version_id,
            job_id=self._job_id,
            status=status,  # type: ignore[arg-type]
            result_block_ids=block_ids,
        )


def parse_pdf_bytes(
    pdf_bytes: bytes, ocr_engine: OcrEngine | None = None
) -> _ParsedPdf:
    """Pure synchronous parser entry suitable for a separately owned worker."""
    blocks: list[DocumentBlock] = []
    dimensions: list[tuple[float, float]] = []
    recognition_complete = True
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as document:
            if not document.pages or len(document.pages) > _MAX_PAGES:
                raise ReviewError("document_page_limit")
            for page_index, page in enumerate(document.pages, 1):
                _validate_page_geometry(page)
                dimensions.append((float(page.width), float(page.height)))
                native = _native_line_blocks(page, page_index)
                blocks.extend(native)
                if not native:
                    ocr_blocks, complete = _ocr_blocks(
                        pdf_bytes, page_index, page.width, page.height, ocr_engine
                    )
                    blocks.extend(ocr_blocks)
                    recognition_complete = recognition_complete and complete
                elif page.images:
                    for region_index, region in enumerate(
                        _image_regions(page.images, page.width, page.height), 1
                    ):
                        ocr_blocks, complete = _ocr_blocks(
                            pdf_bytes,
                            page_index,
                            page.width,
                            page.height,
                            ocr_engine,
                            region=region,
                            region_index=region_index,
                        )
                        merged, unambiguous = _merge_mixed_blocks(
                            native, ocr_blocks, region
                        )
                        blocks.extend(merged)
                        recognition_complete = (
                            recognition_complete and complete and unambiguous
                        )
                if len(blocks) > _MAX_BLOCKS:
                    raise ReviewError("document_block_limit")
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("document_parse_failed") from exc
    serialized_size = sum(
        len(json.dumps(block.model_dump(mode="json"), ensure_ascii=False).encode("utf-8"))
        for block in blocks
    )
    if serialized_size > _MAX_SERIALIZED_BYTES:
        raise ReviewError("document_serialized_limit")
    return _ParsedPdf(tuple(blocks), tuple(dimensions), recognition_complete)


def _validate_page_geometry(page: Any) -> None:
    rotation = int(page.rotation or 0) % 360
    cropbox = tuple(float(value) for value in page.cropbox)
    mediabox = tuple(float(value) for value in page.mediabox)
    if rotation != 0 or len(cropbox) != 4 or len(mediabox) != 4:
        raise ReviewError("page_geometry_unsupported")
    if any(abs(left - right) > 0.01 for left, right in zip(cropbox, mediabox, strict=True)):
        raise ReviewError("page_geometry_unsupported")
    width = float(page.width)
    height = float(page.height)
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
        or width > _MAX_PAGE_SIDE_POINTS
        or height > _MAX_PAGE_SIDE_POINTS
        or width * _OCR_RENDER_SCALE * height * _OCR_RENDER_SCALE > _MAX_OCR_PIXELS
        or any(not math.isfinite(value) for value in (*cropbox, *mediabox))
        or abs(mediabox[0]) > 0.01
        or abs(mediabox[1]) > 0.01
        or abs(cropbox[0]) > 0.01
        or abs(cropbox[1]) > 0.01
        or abs((mediabox[2] - mediabox[0]) - width) > 0.01
        or abs((mediabox[3] - mediabox[1]) - height) > 0.01
    ):
        raise ReviewError("page_geometry_unsupported")


def _native_line_blocks(page: Any, page_number: int) -> list[DocumentBlock]:
    chars = [char for char in page.chars if isinstance(char.get("text"), str)]
    tables = _tables(page)
    assignments: dict[tuple[int, int, int], list[dict[str, Any]]] = {
        (table_index, row_index, column_index): []
        for table_index, rows in enumerate(tables)
        for row_index, row in enumerate(rows)
        for column_index, _box in enumerate(row)
    }
    body_chars: list[dict[str, Any]] = []
    for char in chars:
        x = (float(char["x0"]) + float(char["x1"])) / 2
        y = (float(char["top"]) + float(char["bottom"])) / 2
        candidates = [
            (table_index, row_index, column_index)
            for table_index, rows in enumerate(tables)
            for row_index, row in enumerate(rows)
            for column_index, box in enumerate(row)
            if _inside_half_open(x, y, box)
        ]
        if len(candidates) > 1:
            raise ReviewError("table_geometry_ambiguous")
        if candidates:
            assignments[candidates[0]].append(char)
        else:
            body_chars.append(char)

    result: list[DocumentBlock] = []
    for table_index, rows in enumerate(tables, 1):
        table_id = f"p{page_number:03d}-table{table_index:03d}"
        for row_index, row in enumerate(rows):
            for column_index, _box in enumerate(row):
                cell_chars = assignments[(table_index - 1, row_index, column_index)]
                block_id = f"{table_id}-r{row_index:03d}-c{column_index:03d}"
                text, anchors = _text_and_anchors(cell_chars, page_number, block_id)
                result.append(
                    DocumentBlock(
                        block_id=block_id,
                        kind="table",
                        text=text,
                        anchors=anchors,
                        quality="verified",
                        table_position=TablePosition(
                            table_id=table_id, row=row_index, column=column_index
                        ),
                    )
                )

    result.extend(_body_line_blocks(body_chars, page_number))
    return result


def _body_line_blocks(
    chars: list[dict[str, Any]], page_number: int
) -> list[DocumentBlock]:
    chars.sort(key=lambda char: (round(float(char["top"]), 1), float(char["x0"])))
    lines: list[list[dict[str, Any]]] = []
    for char in chars:
        if not lines or abs(float(char["top"]) - float(lines[-1][0]["top"])) > 2.0:
            lines.append([char])
        else:
            lines[-1].append(char)
    result: list[DocumentBlock] = []
    for line_number, line in enumerate(lines, 1):
        block_id = f"p{page_number:03d}-line{line_number:04d}"
        text, anchors = _text_and_anchors(line, page_number, block_id)
        if not text:
            continue
        result.append(
            DocumentBlock(
                block_id=block_id,
                kind="text",
                text=text,
                anchors=anchors,
                quality="verified",
            )
        )
    return result


def _text_and_anchors(
    chars: list[dict[str, Any]], page_number: int, block_id: str
) -> tuple[str, tuple[Anchor, ...]]:
    chars.sort(key=lambda char: (round(float(char["top"]), 1), float(char["x0"])))
    lines: list[list[dict[str, Any]]] = []
    for char in chars:
        if not lines or abs(float(char["top"]) - float(lines[-1][0]["top"])) > 2.0:
            lines.append([char])
        else:
            lines[-1].append(char)
    texts: list[str] = []
    anchors: list[Anchor] = []
    offset = 0
    for line_index, line in enumerate(lines, 1):
        line.sort(key=lambda char: float(char["x0"]))
        text = "".join(str(char["text"]) for char in line)
        if not text:
            continue
        if texts:
            offset += 1
        start = offset
        offset += len(text)
        texts.append(text)
        x0 = min(float(char["x0"]) for char in line)
        x1 = max(float(char["x1"]) for char in line)
        top = min(float(char["top"]) for char in line)
        bottom = max(float(char["bottom"]) for char in line)
        anchors.append(
            Anchor(
                anchor_id=f"{block_id}-anchor{line_index:03d}",
                page=page_number,
                start=start,
                end=offset,
                quad=(x0, top, x1, top, x1, bottom, x0, bottom),
            )
        )
    return "\n".join(texts), tuple(anchors)


def _tables(page: Any) -> tuple[_TableRows, ...]:
    try:
        tables = []
        for table in page.find_tables():
            rows = []
            for row in table.rows:
                if any(cell is None for cell in row.cells):
                    raise ReviewError("table_geometry_ambiguous")
                rows.append(
                    tuple(
                        (float(cell[0]), float(cell[1]), float(cell[2]), float(cell[3]))
                        for cell in row.cells
                    )
                )
            flat = [box for row in rows for box in row]
            overlaps = any(
                _overlap(left, right)
                for index, left in enumerate(flat)
                for right in flat[index + 1 :]
            )
            if not rows or overlaps:
                raise ReviewError("table_geometry_ambiguous")
            tables.append(tuple(rows))
        return tuple(tables)
    except ReviewError:
        raise
    except Exception as exc:
        raise ReviewError("table_detection_failed") from exc


def _overlap(left: Sequence[float], right: Sequence[float]) -> bool:
    return (
        min(left[2], right[2]) - max(left[0], right[0]) > 0.01
        and min(left[3], right[3]) - max(left[1], right[1]) > 0.01
    )


def _inside_half_open(x: float, y: float, box: Sequence[float]) -> bool:
    return bool(
        len(box) == 4
        and box[0] <= x < box[2]
        and box[1] <= y < box[3]
    )


def _inside(x: float, y: float, box: Sequence[float]) -> bool:
    return len(box) == 4 and box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _ocr_blocks(
    pdf_bytes: bytes,
    page_number: int,
    width: float,
    height: float,
    engine: OcrEngine | None,
    *,
    region: _Box | None = None,
    region_index: int = 0,
) -> tuple[list[DocumentBlock], bool]:
    if engine is None:
        engine = _load_rapid_ocr()
    if engine is None:
        return [_unrecognized_image_block(page_number, width, height, region)], False
    try:
        image = _render_page(pdf_bytes, page_number - 1)
        if region is not None:
            image = cast(Any, image).crop(
                tuple(round(value * _OCR_RENDER_SCALE) for value in region)
            )
        raw = engine(image)
        rows = _normalise_ocr_rows(raw)
    except Exception:
        return [_unrecognized_image_block(page_number, width, height, region)], False
    if not rows:
        return [_unrecognized_image_block(page_number, width, height, region)], False
    blocks: list[DocumentBlock] = []
    complete = True
    for index, (quad, text, confidence) in enumerate(rows, 1):
        if not text:
            complete = False
            continue
        offset_x, offset_y = (0.0, 0.0) if region is None else (region[0], region[1])
        page_quad = tuple(
            value / _OCR_RENDER_SCALE + (offset_x if index % 2 == 0 else offset_y)
            for index, value in enumerate(quad)
        )
        if not _valid_page_quad(page_quad, width, height):
            complete = False
            continue
        quality: Literal["verified", "needs_review"] = (
            "verified" if confidence >= 0.95 else "needs_review"
        )
        complete = complete and quality == "verified"
        region_part = "" if region is None else f"-region{region_index:03d}"
        block_id = f"p{page_number:03d}{region_part}-ocr{index:04d}"
        blocks.append(
            DocumentBlock(
                block_id=block_id,
                kind="image",
                text=text,
                anchors=(
                    Anchor(
                        anchor_id=f"{block_id}-anchor",
                        page=page_number,
                        start=0,
                        end=len(text),
                        quad=page_quad,  # type: ignore[arg-type]
                    ),
                ),
                quality=quality,
            )
        )
    return blocks or [_unrecognized_image_block(page_number, width, height, region)], complete


def _load_rapid_ocr() -> OcrEngine | None:
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]

        return RapidOCR()  # type: ignore[no-any-return]
    except Exception:
        return None


def _render_page(pdf_bytes: bytes, page_index: int) -> object:
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    document = pdfium.PdfDocument(pdf_bytes)
    try:
        page = document[page_index]
        try:
            return page.render(scale=_OCR_RENDER_SCALE).to_pil()
        finally:
            page.close()
    finally:
        document.close()


def _normalise_ocr_rows(
    raw: object,
) -> list[tuple[tuple[float, float, float, float, float, float, float, float], str, float]]:
    source = getattr(raw, "result", raw)
    if isinstance(source, tuple):
        source = source[0]
    if not isinstance(source, Sequence) or isinstance(source, (str, bytes)):
        return []
    rows = []
    for item in source:
        if not isinstance(item, Sequence) or len(item) < 3:
            continue
        box, text, confidence = item[0], item[1], item[2]
        if not isinstance(box, Sequence) or len(box) != 4 or not isinstance(text, str):
            continue
        try:
            points = [float(coordinate) for point in box for coordinate in point]
            score = float(confidence)
        except (TypeError, ValueError):
            continue
        if len(points) != 8:
            continue
        quad = (
            points[0],
            points[1],
            points[2],
            points[3],
            points[4],
            points[5],
            points[6],
            points[7],
        )
        rows.append((quad, text, score))
    return rows


def _unrecognized_image_block(
    page_number: int, width: float, height: float, region: _Box | None = None
) -> DocumentBlock:
    box = (0.0, 0.0, width, height) if region is None else region
    suffix = "" if region is None else f"-{round(box[0])}-{round(box[1])}"
    block_id = f"p{page_number:03d}-image-unrecognized{suffix}"
    text = "[image content requires review]"
    return DocumentBlock(
        block_id=block_id,
        kind="image",
        text=text,
        anchors=(
            Anchor(
                anchor_id=f"{block_id}-anchor",
                page=page_number,
                start=0,
                end=len(text),
                quad=(box[0], box[1], box[2], box[1], box[2], box[3], box[0], box[3]),
            ),
        ),
        quality="needs_review",
    )


def _image_regions(images: Sequence[object], width: float, height: float) -> list[_Box]:
    regions: list[_Box] = []
    for image in images:
        if not isinstance(image, dict):
            raise ReviewError("page_geometry_unsupported")
        try:
            box = tuple(float(image[key]) for key in ("x0", "top", "x1", "bottom"))
        except (KeyError, TypeError, ValueError) as exc:
            raise ReviewError("page_geometry_unsupported") from exc
        if (
            len(box) != 4
            or any(not math.isfinite(value) for value in box)
            or not (0 <= box[0] < box[2] <= width)
            or not (0 <= box[1] < box[3] <= height)
        ):
            raise ReviewError("page_geometry_unsupported")
        current = box
        remaining: list[_Box] = []
        for existing in regions:
            if _overlap(existing, current) or existing == current:
                current = (
                    min(existing[0], current[0]),
                    min(existing[1], current[1]),
                    max(existing[2], current[2]),
                    max(existing[3], current[3]),
                )
            else:
                remaining.append(existing)
        remaining.append(current)
        regions = remaining
    return sorted(regions, key=lambda box: (box[1], box[0], box[3], box[2]))


def _merge_mixed_blocks(
    native: Sequence[DocumentBlock], ocr: Sequence[DocumentBlock], region: _Box
) -> tuple[list[DocumentBlock], bool]:
    native_in_region = [
        block
        for block in native
        if any(_overlap(_anchor_box(anchor), region) for anchor in block.anchors)
    ]
    merged: list[DocumentBlock] = []
    unambiguous = True
    for block in ocr:
        exact_duplicate = any(
            block.text.strip() == native_block.text.strip()
            and _boxes_close(_block_box(block), _block_box(native_block))
            for native_block in native_in_region
        )
        if exact_duplicate:
            continue
        overlaps_native = any(
            _overlap(_block_box(block), _block_box(native_block))
            for native_block in native_in_region
        )
        if overlaps_native:
            block = block.model_copy(update={"quality": "needs_review"})
            unambiguous = False
        merged.append(block)
    return merged, unambiguous


def _anchor_box(anchor: Anchor) -> _Box:
    return (
        min(anchor.quad[0::2]),
        min(anchor.quad[1::2]),
        max(anchor.quad[0::2]),
        max(anchor.quad[1::2]),
    )


def _block_box(block: DocumentBlock) -> _Box:
    boxes = [_anchor_box(anchor) for anchor in block.anchors]
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _boxes_close(left: _Box, right: _Box, tolerance: float = 1.0) -> bool:
    return all(abs(a - b) <= tolerance for a, b in zip(left, right, strict=True))


def _valid_page_quad(quad: Sequence[float], width: float, height: float) -> bool:
    return len(quad) == 8 and all(
        math.isfinite(value)
        and 0 <= value <= (width if index % 2 == 0 else height)
        for index, value in enumerate(quad)
    )


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not cursor.startswith("cursor-"):
        raise ReviewError("cursor_invalid")
    try:
        value = int(cursor.removeprefix("cursor-"))
    except ValueError as exc:
        raise ReviewError("cursor_invalid") from exc
    if value < 0:
        raise ReviewError("cursor_invalid")
    return value
