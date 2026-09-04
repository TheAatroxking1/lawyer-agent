from __future__ import annotations

import pytest

from lawyer_agent.infrastructure.documents.docx_loader import ZipDocxLoader
from lawyer_agent.infrastructure.documents.docx_writer import (
    ReportSegment,
    ReportSegmentKind,
    ZipDocxReportWriter,
)


def _read_back(payload: bytes) -> list[str]:
    loader = ZipDocxLoader(source_ref="object://tenant/report.docx")
    document = loader.load("object://tenant/report.docx", payload)
    return [paragraph.text for paragraph in document.paragraphs]


def test_writer_round_trips_title_body_and_list() -> None:
    segments = (
        ReportSegment(kind=ReportSegmentKind.TITLE, text="合同风险检查报告"),
        ReportSegment(
            kind=ReportSegmentKind.PARAGRAPH,
            text="免责声明：规则候选提示，非法律意见。",
        ),
        ReportSegment(kind=ReportSegmentKind.LIST_ITEM, text="第一条：违约金比例较高。"),
        ReportSegment(kind=ReportSegmentKind.LIST_ITEM, text="第二条：逾期交付责任缺失。"),
        ReportSegment(kind=ReportSegmentKind.NOTE, text="高风险建议需人工核验。"),
    )
    payload = ZipDocxReportWriter().write(segments)
    assert payload[:2] == b"PK"
    assert _read_back(payload) == [
        "合同风险检查报告",
        "免责声明：规则候选提示，非法律意见。",
        "第一条：违约金比例较高。",
        "第二条：逾期交付责任缺失。",
        "高风险建议需人工核验。",
    ]


def test_writer_escapes_xml_special_characters() -> None:
    segments = (
        ReportSegment(
            kind=ReportSegmentKind.PARAGRAPH,
            text="比例 30% & <违约金> \"约定\" '文本' 未封闭",
        ),
    )
    payload = ZipDocxReportWriter().write(segments)
    # The produced payload must stay valid XML for the read-only loader.
    assert _read_back(payload) == ['比例 30% & <违约金> "约定" \'文本\' 未封闭']


def test_writer_rejects_control_characters() -> None:
    with pytest.raises(ValueError, match="control character"):
        ZipDocxReportWriter().write(
            (ReportSegment(kind=ReportSegmentKind.PARAGRAPH, text="含\u0000空字节"),)
        )


def test_writer_rejects_oversized_segment() -> None:
    with pytest.raises(ValueError, match="too long"):
        ZipDocxReportWriter().write(
            (ReportSegment(kind=ReportSegmentKind.PARAGRAPH, text="x" * 200_001),)
        )


def test_writer_empty_segments_produce_valid_empty_document() -> None:
    payload = ZipDocxReportWriter().write(())
    assert payload[:2] == b"PK"
    assert _read_back(payload) == []
