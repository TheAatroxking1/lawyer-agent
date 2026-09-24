from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from lawyer_agent.application.legal_corpus_export import ExportConfig, export_corpus
from lawyer_agent.application.legal_export_verification import verify_export


@pytest.fixture
def exported(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    output = tmp_path / "output"
    source.mkdir()
    with zipfile.ZipFile(source / "合成.docx", "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body>'
            '<w:p><w:r><w:t>合成法规</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>第一条 合成甲。</w:t></w:r></w:p>'
            '<w:p><w:r><w:t>第二条 合成乙。</w:t></w:r></w:p>'
            '</w:body></w:document>',
        )
    export_corpus(ExportConfig(source, output))
    return source, output


def test_verifier_checks_current_sources_and_content(exported):
    source, output = exported
    report = verify_export(source_root=source, output_root=output)
    assert report.complete
    assert report.verified_documents == 1
    assert report.verified_chunks == 3
    assert report.errors == ()


def test_source_changed_invalidates_old_export(exported):
    source, output = exported
    (source / "合成.docx").write_bytes(b"changed")
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert any(error.code == "source_hash_mismatch" for error in report.errors)


def test_new_source_prevents_all_complete(exported):
    source, output = exported
    (source / "新增.doc").write_bytes(b"legacy")
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert any(error.code == "source_missing_from_manifest" for error in report.errors)


def test_chunk_tampering_is_rejected_even_when_rehashing_file(exported):
    from hashlib import sha256

    source, output = exported
    metadata_path = next(output.glob("*/document.json"))
    chunks_path = metadata_path.parent / "chunks.jsonl"
    chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines()]
    chunks[0]["text"] = "被替换的正文"
    chunks[0]["content_sha256"] = sha256(chunks[0]["text"].encode()).hexdigest()
    text = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks)
    chunks_path.write_text(text, encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["output_hashes"]["chunks.jsonl"] = sha256(chunks_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert any(error.code == "chunk_source_content_mismatch" for error in report.errors)


def test_missing_completion_file_is_failure(exported):
    source, output = exported
    next(output.glob("*/document.json")).unlink()
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert report.verified_documents == 0


def test_output_path_escape_rejected_without_reading_outside(exported):
    source, output = exported
    manifest = output / "manifest.jsonl"
    row = json.loads(manifest.read_text(encoding="utf-8"))
    row["output_directory"] = "../source"
    manifest.write_text(json.dumps(row), encoding="utf-8")
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert any(error.code == "output_path_outside_root" for error in report.errors)


def test_verification_cli_writes_report_and_rejects_source_output_overlap(exported):
    from lawyer_agent.cli.corpus_verify import main

    source, output = exported
    assert main(["--source-root", str(source), "--output-root", str(output)]) == 0
    assert json.loads((output / "verification.json").read_text(encoding="utf-8"))["complete"]
    assert main(["--source-root", str(source), "--output-root", str(source)]) == 2
    assert not (source / "verification.json").exists()


def test_subset_verification_never_claims_all_complete(exported):
    source, output = exported
    report = verify_export(source_root=source, output_root=output, require_all_sources=False)
    assert report.verified_documents == 1
    assert not report.scope_complete
    assert not report.complete


def test_forged_article_identity_and_range_are_rejected(exported):
    from hashlib import sha256

    source, output = exported
    metadata_path = next(output.glob("*/document.json"))
    chunks_path = metadata_path.parent / "chunks.jsonl"
    chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines()]
    provision = next(row for row in chunks if row["chunk_type"] == "provision")
    provision.update(
        article_no="第九百九十九条", structure_path=["伪造章"], source_char_range=[-99, -1],
    )
    chunks_path.write_bytes("".join(json.dumps(c) + "\n" for c in chunks).encode())
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["output_hashes"]["chunks.jsonl"] = sha256(chunks_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert any(error.code == "article_structure_mismatch" for error in report.errors)


def test_conversion_source_overlap_is_rejected_even_with_matching_bytes(exported):
    from hashlib import sha256

    source, output = exported
    legacy = source / "旧版.doc"
    legacy.write_bytes(b"synthetic legacy")
    converted = source.parent / "converted"
    converted.mkdir()
    result = converted / "result.docx"
    result.write_bytes((source / "合成.docx").read_bytes())
    row = {
        "schema_version": "word-conversion-v1", "status": "converted",
        "source_path": str(legacy), "source_sha256": sha256(legacy.read_bytes()).hexdigest(),
        "converted_path": str(result), "converted_sha256": sha256(result.read_bytes()).hexdigest(),
    }
    manifest = converted / "manifest.jsonl"
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    export_corpus(
        ExportConfig(source, output, conversion_manifest=manifest, converted_root=converted)
    )
    hidden_payload = source / "payload.bin"
    hidden_payload.write_bytes(result.read_bytes())
    row["converted_path"] = str(hidden_payload)
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")
    report = verify_export(
        source_root=source, output_root=output,
        conversion_manifest=manifest, converted_root=source,
    )
    assert not report.complete
    assert any(error.code == "source_conversion_overlap" for error in report.errors)


def test_child_locator_must_identify_the_actual_parent_substring(exported):
    from hashlib import sha256

    source, output = exported
    original = source / "合成.docx"
    with zipfile.ZipFile(original) as archive:
        xml = archive.read("word/document.xml").decode()
    xml = xml.replace("第一条 合成甲。", "第一条 " + "合成甲。" * 300)
    with zipfile.ZipFile(original, "w") as archive:
        archive.writestr("word/document.xml", xml)
    export_corpus(ExportConfig(source, output))
    manifest = json.loads((output / "manifest.jsonl").read_text(encoding="utf-8"))
    metadata_path = output / manifest["output_directory"] / "document.json"
    chunks_path = metadata_path.parent / "chunks.jsonl"
    chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines()]
    child = next(row for row in chunks if row["parent_chunk_id"] is not None)
    child["parent_relative_char_span"] = [-99, -1]
    chunks_path.write_bytes("".join(json.dumps(c) + "\n" for c in chunks).encode())
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["output_hashes"]["chunks.jsonl"] = sha256(chunks_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert any(error.code == "child_source_location_mismatch" for error in report.errors)


def test_equal_repeated_text_cannot_hide_a_shifted_window(exported):
    from hashlib import sha256

    source, output = exported
    original = source / "合成.docx"
    with zipfile.ZipFile(original) as archive:
        xml = archive.read("word/document.xml").decode()
    xml = xml.replace("第一条 合成甲。", "第一条 " + "甲" * 1200)
    with zipfile.ZipFile(original, "w") as archive:
        archive.writestr("word/document.xml", xml)
    export_corpus(ExportConfig(source, output))
    manifest = json.loads((output / "manifest.jsonl").read_text(encoding="utf-8"))
    metadata_path = output / manifest["output_directory"] / "document.json"
    chunks_path = metadata_path.parent / "chunks.jsonl"
    chunks = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines()]
    child = next(row for row in chunks if row["parent_chunk_id"] is not None)
    child["parent_relative_char_span"] = [n + 1 for n in child["parent_relative_char_span"]]
    child["source_char_range"] = [n + 1 for n in child["source_char_range"]]
    chunks_path.write_bytes("".join(json.dumps(c) + "\n" for c in chunks).encode())
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["output_hashes"]["chunks.jsonl"] = sha256(chunks_path.read_bytes()).hexdigest()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    report = verify_export(source_root=source, output_root=output)
    assert not report.complete
    assert any(error.code == "chunk_derivation_mismatch" for error in report.errors)
