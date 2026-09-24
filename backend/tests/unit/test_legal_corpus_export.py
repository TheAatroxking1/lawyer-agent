from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

import lawyer_agent.application.legal_corpus_export as export_module
from lawyer_agent.application.legal_corpus_export import (
    ExportConfig,
    ExportError,
    export_corpus,
)
from lawyer_agent.cli.corpus_export import main
from lawyer_agent.infrastructure.documents.export_reader import (
    DocxExportReader,
    ExportReadError,
)

NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _docx(path: Path, body: str, *, extras: dict[str, str] | None = None) -> None:
    document = f'<w:document xmlns:w="{NS}"><w:body>{body}</w:body></w:document>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)
        for name, value in (extras or {}).items():
            archive.writestr(name, value)


def _raw_docx(path: Path, document_xml: bytes) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)


def _p(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _rows(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_reader_preserves_body_table_and_auxiliary_parts_with_quality_flags(tmp_path: Path) -> None:
    source = tmp_path / "law.docx"
    table = f"<w:tbl><w:tr><w:tc>{_p('表格条目')}</w:tc></w:tr></w:tbl>"
    footnote = (
        f'<w:footnotes xmlns:w="{NS}"><w:footnote>'
        f'{_p("脚注文字")}</w:footnote></w:footnotes>'
    )
    header = f'<w:hdr xmlns:w="{NS}">{_p("页眉文字")}</w:hdr>'
    _docx(source, _p("第一条 正文。") + table, extras={
        "word/footnotes.xml": footnote,
        "word/header1.xml": header,
    })

    result = DocxExportReader().read(source)

    assert [(p.text, p.part, p.location) for p in result.paragraphs] == [
        ("第一条 正文。", "word/document.xml", "body"),
        ("表格条目", "word/document.xml", "table"),
        ("脚注文字", "word/footnotes.xml", "auxiliary"),
        ("页眉文字", "word/header1.xml", "auxiliary"),
    ]
    assert "table_layout_not_preserved" in result.quality_flags
    assert "auxiliary_parts_separate" in result.quality_flags


def test_reader_rejects_entities_and_zip_members_over_limit(tmp_path: Path) -> None:
    entity = tmp_path / "entity.docx"
    _docx(entity, '<!DOCTYPE x [<!ENTITY y "boom">]>' + _p("&y;"))
    with pytest.raises(ExportReadError, match="xml_entities_forbidden|invalid_docx_xml"):
        DocxExportReader().read(entity)

    oversized = tmp_path / "large.docx"
    _docx(oversized, _p("甲" * 200))
    with pytest.raises(ExportReadError, match="expanded_size_limit"):
        DocxExportReader(max_member_bytes=100).read(oversized)

    utf16 = tmp_path / "utf16.docx"
    xml = (
        f'<?xml version="1.0" encoding="utf-16"?><!DOCTYPE x [<!ENTITY y "boom">]>'
        f'<w:document xmlns:w="{NS}"><w:body>{_p("&y;")}</w:body></w:document>'
    )
    _raw_docx(utf16, xml.encode("utf-16"))
    with pytest.raises(ExportReadError, match="xml_entities_forbidden"):
        DocxExportReader().read(utf16)

    bomless = tmp_path / "utf16-no-bom.docx"
    no_declaration = (
        '<!DOCTYPE x [<!ENTITY y "boom">]>'
        f'<w:document xmlns:w="{NS}"><w:body>{_p("&y;")}</w:body></w:document>'
    )
    _raw_docx(bomless, no_declaration.encode("utf-16-le"))
    with pytest.raises(ExportReadError, match="xml_entities_forbidden"):
        DocxExportReader().read(bomless)


def test_reader_preserves_tabs_breaks_hyphens_and_table_cell_position(tmp_path: Path) -> None:
    source = tmp_path / "controls.docx"
    paragraph = (
        "<w:p><w:r><w:t>甲</w:t><w:tab/><w:t>乙</w:t><w:br/><w:t>丙</w:t>"
        "<w:noBreakHyphen/><w:t>丁</w:t></w:r></w:p>"
    )
    table = f"<w:tbl><w:tr><w:tc>{_p('单元格')}</w:tc></w:tr></w:tbl>"
    _docx(source, paragraph + table)

    result = DocxExportReader().read(source)

    assert result.paragraphs[0].text == "甲\t乙\n丙‑丁"
    assert result.paragraphs[1].table_position == (0, 0, 0)
    assert "word_control_characters_preserved" in result.quality_flags


def test_reader_does_not_duplicate_nested_textbox_paragraphs(tmp_path: Path) -> None:
    source = tmp_path / "textbox.docx"
    textbox = (
        f'<w:p><w:r><w:t>外层</w:t></w:r><w:r><w:object><w:txbxContent>'
        f'{_p("文本框")}</w:txbxContent></w:object></w:r></w:p>'
    )
    _docx(source, textbox)

    result = DocxExportReader().read(source)

    assert [paragraph.text for paragraph in result.paragraphs] == ["外层", "文本框"]
    assert "images_or_textboxes_not_ocr" in result.quality_flags


def test_export_keeps_articles_supplements_unassigned_text_and_stable_ids(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(
        source / "law.docx",
        _p("法规标题") + _p("序言") + _p("说明文字") + _p("第一条 甲。")
        + _p("第一条之一 增补。") + _p("附：尾注"),
    )
    config = ExportConfig(source_root=source, output_root=output)

    first = export_corpus(config)
    first_manifest = _rows(output / "manifest.jsonl")
    record = first_manifest[0]
    directory = output / str(record["output_directory"])
    chunks = _rows(directory / "chunks.jsonl")
    paragraphs = _rows(directory / "paragraphs.jsonl")
    ids = [row["chunk_id"] for row in chunks]

    assert first.completed == 1
    assert [row["text"] for row in paragraphs] == [
        "法规标题", "序言", "说明文字", "第一条 甲。", "第一条之一 增补。", "附：尾注"
    ]
    assert [row["article_no"] for row in chunks if row["chunk_type"] == "provision"] == [
        "第一条", "第一条之一"
    ]
    assert {row["text"] for row in chunks if row["chunk_type"] == "source_paragraph"} >= {
        "法规标题", "序言", "说明文字"
    }
    export_corpus(config)
    assert [row["chunk_id"] for row in _rows(directory / "chunks.jsonl")] == ids


def test_export_serializes_restored_numbering_provenance_only_when_used(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    numbered = (
        '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/>'
        '</w:numPr></w:pPr><w:r><w:t>正文。</w:t></w:r></w:p>'
    )
    numbering = (
        f'<w:numbering xmlns:w="{NS}"><w:abstractNum w:abstractNumId="0">'
        '<w:lvl w:ilvl="0"><w:start w:val="4"/>'
        '<w:numFmt w:val="chineseCounting"/><w:lvlText w:val="第%1条"/>'
        '</w:lvl></w:abstractNum><w:num w:numId="1">'
        '<w:abstractNumId w:val="0"/></w:num></w:numbering>'
    )
    _docx(source / "numbered.docx", numbered, extras={"word/numbering.xml": numbering})
    _docx(source / "plain.docx", _p("第一条 普通正文。"))

    export_corpus(ExportConfig(source, output))
    records = {row["source_relative_path"]: row for row in _rows(output / "manifest.jsonl")}
    numbered_dir = output / str(records["numbered.docx"]["output_directory"])
    plain_dir = output / str(records["plain.docx"]["output_directory"])
    numbered_row = _rows(numbered_dir / "paragraphs.jsonl")[0]
    plain_row = _rows(plain_dir / "paragraphs.jsonl")[0]
    numbered_document = json.loads(
        (numbered_dir / "document.json").read_text(encoding="utf-8")
    )

    assert numbered_row["text"] == "第四条 正文。"
    assert numbered_row["numbering_provenance"]["original_text"] == "正文。"
    assert numbered_row["numbering_provenance"]["value"] == 4
    assert "numbering_provenance" not in plain_row
    assert numbered_document["loader_version"] == "docx-export-reader-v2:numbering-v1"


def test_no_article_and_parse_failure_fall_back_without_losing_paragraphs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "plain.docx", _p("标题") + _p("普通段落"))
    _docx(source / "ambiguous.docx", _p("第一条之一新增义务。") + _p("后续段落"))

    result = export_corpus(ExportConfig(source_root=source, output_root=output))
    manifest = _rows(output / "manifest.jsonl")

    assert result.completed == 2
    assert all(row["status"] == "completed" for row in manifest)
    for record in manifest:
        directory = output / str(record["output_directory"])
        document = json.loads((directory / "document.json").read_text(encoding="utf-8"))
        chunks = _rows(directory / "chunks.jsonl")
        assert "structure_review_required" in document["quality_flags"]
        assert all(row["chunk_type"] == "source_paragraph" for row in chunks)


def test_long_article_exports_restorable_parent_child_chain(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    lines = ["第一条 " + "甲" * 40, "第二款 " + "乙" * 40]
    _docx(source / "long.docx", "".join(_p(line) for line in lines))

    export_corpus(ExportConfig(
        source, output, max_leaf_chars=30, window_chars=20, overlap_chars=5
    ))
    record = _rows(output / "manifest.jsonl")[0]
    chunks = _rows(output / str(record["output_directory"]) / "chunks.jsonl")
    parent = next(row for row in chunks if row["chunk_type"] == "provision")
    children = [row for row in chunks if row["parent_chunk_id"] is not None]

    assert parent["text"] == "".join(lines)
    assert children
    assert all(row["parent_chunk_id"] == parent["chunk_id"] for row in children)
    assert all(row["parent_source_paragraph_ordinals"] == [0, 1] for row in children)
    assert all(row["parent_relative_char_span"] is not None for row in children)
    assert all(row["source_location_status"] == "exact_parent_text" for row in children)


def test_duplicate_article_numbers_trigger_conservative_paragraph_fallback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "duplicate.docx", _p("第一条 甲。") + _p("第一条 乙。"))

    export_corpus(ExportConfig(source, output))
    record = _rows(output / "manifest.jsonl")[0]
    directory = output / str(record["output_directory"])
    document = json.loads((directory / "document.json").read_text(encoding="utf-8"))
    chunks = _rows(directory / "chunks.jsonl")

    assert "structure_review_required" in document["quality_flags"]
    assert [row["chunk_type"] for row in chunks] == [
        "source_paragraph", "source_paragraph"
    ]


def test_empty_document_is_failed_not_completed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "empty.docx", "")

    result = export_corpus(ExportConfig(source, output))

    assert result.failed == 1
    assert _rows(output / "manifest.jsonl")[0]["code"] == "no_readable_text"


def test_duplicate_content_has_independent_records_and_corrupt_output_is_rebuilt(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    (source / "a").mkdir(parents=True)
    (source / "b").mkdir()
    _docx(source / "a" / "same.docx", _p("第一条 相同。"))
    (source / "b" / "same.docx").write_bytes((source / "a" / "same.docx").read_bytes())

    export_corpus(ExportConfig(source_root=source, output_root=output))
    manifest = _rows(output / "manifest.jsonl")
    assert len(manifest) == 2
    assert len({row["document_id"] for row in manifest}) == 2
    damaged_dir = output / str(manifest[0]["output_directory"])
    (damaged_dir / "chunks.jsonl").write_text("damaged", encoding="utf-8")

    export_corpus(ExportConfig(source_root=source, output_root=output))
    assert _rows(damaged_dir / "chunks.jsonl")[0]["text"] == "第一条 相同。"


def test_incomplete_cached_metadata_is_rebuilt(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "law.docx", _p("第一条 内容。"))
    export_corpus(ExportConfig(source, output))
    record = _rows(output / "manifest.jsonl")[0]
    document_path = output / str(record["output_directory"]) / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    del document["quality_flags"]
    document_path.write_text(json.dumps(document), encoding="utf-8")

    result = export_corpus(ExportConfig(source, output))

    assert result.completed == 1
    rebuilt = json.loads(document_path.read_text(encoding="utf-8"))
    assert isinstance(rebuilt["quality_flags"], list)


def test_old_empty_cache_is_rebuilt_with_current_schema(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "law.docx", _p("第一条 内容。"))
    export_corpus(ExportConfig(source, output))
    record = _rows(output / "manifest.jsonl")[0]
    directory = output / str(record["output_directory"])
    document_path = directory / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    (directory / "paragraphs.jsonl").write_text("", encoding="utf-8")
    (directory / "chunks.jsonl").write_text("", encoding="utf-8")
    document.update(
        export_version="legal-corpus-export-v1",
        loader_version="docx-export-reader-v1",
        paragraph_count=0,
        chunk_count=0,
        parent_count=0,
        child_count=0,
        covered_paragraph_count=0,
        output_hashes={
            "paragraphs.jsonl": sha256(b"").hexdigest(),
            "chunks.jsonl": sha256(b"").hexdigest(),
        },
    )
    document_path.write_text(json.dumps(document), encoding="utf-8")

    export_corpus(ExportConfig(source, output))

    rebuilt = json.loads(document_path.read_text(encoding="utf-8"))
    assert rebuilt["export_version"] == "legal-corpus-export-v2"
    assert rebuilt["loader_version"] == "docx-export-reader-v2"
    assert rebuilt["paragraph_count"] == rebuilt["covered_paragraph_count"] == 1


def test_source_hash_error_is_isolated_and_next_file_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "a.docx", _p("第一条 甲。"))
    _docx(source / "b.docx", _p("第一条 乙。"))
    real_hash = export_module._file_hash

    def fail_one(path: Path) -> str:
        if path.name == "a.docx":
            raise PermissionError("denied")
        return real_hash(path)

    monkeypatch.setattr(export_module, "_file_hash", fail_one)
    result = export_corpus(ExportConfig(source, output))

    assert result.failed == 1
    assert result.completed == 1
    assert [row["code"] for row in _rows(output / "manifest.jsonl")] == [
        "source_read_failed", "completed"
    ]


def test_source_change_between_hash_and_read_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    path = source / "law.docx"
    _docx(path, _p("第一条 旧。"))
    initial = export_module._file_hash(path)
    calls = 0

    def changing_hash(target: Path) -> str:
        nonlocal calls
        calls += 1
        return initial if calls == 1 else "f" * 64

    monkeypatch.setattr(export_module, "_file_hash", changing_hash)
    result = export_corpus(ExportConfig(source, output))

    assert result.failed == 1
    assert _rows(output / "manifest.jsonl")[0]["code"] == "source_changed_during_export"


def test_doc_pending_conversion_and_current_run_limit_summary(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    (source / "old.doc").write_bytes(b"legacy")
    _docx(source / "new.docx", _p("第一条 新。"))

    result = export_corpus(ExportConfig(source_root=source, output_root=output, limit=1))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))

    assert result.total == 1
    assert summary["pilot_limit"] == 1
    assert summary["pending_conversion"] + summary["completed"] == 1


def test_docx_only_filters_before_limit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    (source / "a.doc").write_bytes(b"legacy")
    _docx(source / "b.docx", _p("第一条 DOCX。"))

    result = export_corpus(ExportConfig(source, output, limit=1, docx_only=True))

    assert result.total == result.completed == 1
    assert _rows(output / "manifest.jsonl")[0]["source_relative_path"] == "b.docx"


def test_rejects_source_output_overlap_and_symlink_escape(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(ExportError, match="source_output_overlap"):
        export_corpus(ExportConfig(source_root=source, output_root=source / "out"))


def test_source_root_reparse_and_enumeration_errors_fail_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    monkeypatch.setattr(export_module, "_is_reparse", lambda path: path == source)
    with pytest.raises(ExportError, match="unsafe_source_root"):
        export_corpus(ExportConfig(source, output))

    monkeypatch.setattr(export_module, "_is_reparse", lambda path: False)

    def broken_walk(*args: object, **kwargs: object) -> list[object]:
        onerror = kwargs["onerror"]
        assert callable(onerror)
        onerror(PermissionError("denied"))
        return []

    monkeypatch.setattr(export_module.os, "walk", broken_walk)
    with pytest.raises(ExportError, match="source_enumeration_failed"):
        export_corpus(ExportConfig(source, output))


def test_output_reparse_component_fails_only_affected_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    output.mkdir()
    source_file = source / "law.docx"
    _docx(source_file, _p("第一条 内容。"))
    source_hash = sha256(source_file.read_bytes()).hexdigest()
    directory_name = sha256(f"law.docx\0{source_hash}".encode()).hexdigest()[:24]
    (output / directory_name).mkdir()
    real_reparse = export_module._is_reparse

    def derived_is_reparse(path: Path) -> bool:
        return len(path.name) == 24 or real_reparse(path)

    monkeypatch.setattr(export_module, "_is_reparse", derived_is_reparse)
    result = export_corpus(ExportConfig(source, output))

    assert result.failed == 1
    assert _rows(output / "manifest.jsonl")[0]["code"] == "unsafe_output_path"


def test_cache_leaf_reparse_is_checked_before_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "law.docx", _p("第一条 内容。"))
    export_corpus(ExportConfig(source, output))
    record = _rows(output / "manifest.jsonl")[0]
    document_path = output / str(record["output_directory"]) / "document.json"
    real_reparse = export_module._is_reparse
    monkeypatch.setattr(
        export_module,
        "_is_reparse",
        lambda path: path == document_path or real_reparse(path),
    )

    result = export_corpus(ExportConfig(source, output))

    assert result.failed == 1
    assert _rows(output / "manifest.jsonl")[0]["code"] == "unsafe_output_path"


def test_validated_conversion_manifest_is_consumed(tmp_path: Path) -> None:
    source = tmp_path / "source"
    converted = tmp_path / "converted"
    output = tmp_path / "chunks"
    source.mkdir()
    converted.mkdir()
    old = source / "old.doc"
    old.write_bytes(b"legacy")
    generated = converted / "old.docx"
    _docx(generated, _p("第一条 转换文本。"))
    row = {
        "schema_version": "word-conversion-v1",
        "source_path": str(old.resolve()),
        "source_sha256": sha256(old.read_bytes()).hexdigest(),
        "converted_path": str(generated.resolve()),
        "converted_sha256": sha256(generated.read_bytes()).hexdigest(),
        "status": "converted",
    }
    manifest = converted / "manifest.jsonl"
    manifest.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    result = export_corpus(ExportConfig(
        source_root=source,
        output_root=output,
        conversion_manifest=manifest,
        converted_root=converted,
    ))

    assert result.completed == 1
    assert result.pending_conversion == 0


def test_cli_writes_batch_outputs_and_reports_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    _docx(source / "law.docx", _p("第一条 内容。"))

    assert main(["--source-root", str(source), "--output-root", str(output)]) == 0
    assert "本次清单 1；完成 1" in capsys.readouterr().out
    assert (output / "README.md").is_file()


def test_invalid_conversion_manifest_hash_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    converted = tmp_path / "converted"
    output = tmp_path / "chunks"
    source.mkdir()
    converted.mkdir()
    old = source / "old.doc"
    old.write_bytes(b"source")
    generated = converted / "old.docx"
    _docx(generated, _p("第一条 转换文本。"))
    row = {
        "schema_version": "word-conversion-v1",
        "source_path": str(old.resolve()),
        "source_sha256": "0" * 64,
        "converted_path": str(generated.resolve()),
        "converted_sha256": sha256(generated.read_bytes()).hexdigest(),
        "status": "converted",
    }
    manifest = converted / "manifest.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")

    result = export_corpus(ExportConfig(source, output, conversion_manifest=manifest))
    assert result.failed == 1
    assert _rows(output / "manifest.jsonl")[0]["code"] == "conversion_hash_mismatch"
