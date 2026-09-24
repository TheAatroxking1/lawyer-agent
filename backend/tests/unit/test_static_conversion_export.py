from __future__ import annotations

import csv
import json
import runpy
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from lawyer_agent.application.legal_corpus_export import ExportConfig, ExportError, export_corpus
from lawyer_agent.application.legal_export_verification import verify_export

STATIC_VERSION = "binary-word-static-text-v1"
STATIC_FLAGS = {
    "legacy_binary_static_text_recovery", "original_format_not_preserved",
    "field_results_static_not_recalculated",
    "header_section_layout_not_preserved",
    "original_layout_and_visibility_not_preserved",
    "images_objects_and_automatic_numbering_not_recovered",
}
REASON_FLAG = "original_office_validation_failed"


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture(tmp_path, *, version=STATIC_VERSION, reason="office_validation_failed"):
    source = tmp_path / "source"
    source.mkdir()
    original = source / "合成.doc"
    original.write_bytes(b"synthetic legacy bytes")
    converted = tmp_path / "converted"
    converted.mkdir()
    derived = converted / "合成.docx"
    with ZipFile(derived, "w") as archive:
        archive.writestr("word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            '第一条 合成正文。</w:t></w:r></w:p></w:body></w:document>')
    record = {
        "schema_version": "word-conversion-v1", "status": "converted",
        "source_path": str(original), "source_sha256": sha256(original.read_bytes()).hexdigest(),
        "converted_path": str(derived),
        "converted_sha256": sha256(derived.read_bytes()).hexdigest(),
    }
    if version is not None:
        record.update(converter_version=version, converter_fingerprint="a" * 64)
    if reason is not None:
        record["recovery_reason"] = reason
    manifest = converted / "manifest.jsonl"
    _write(manifest, record)
    config = ExportConfig(source_root=source, output_root=tmp_path / "export",
                          converted_root=converted, conversion_manifest=manifest)
    assert export_corpus(config).completed == 1
    return config, next(config.output_root.glob("*/document.json"))


def _verify(config):
    return verify_export(source_root=config.source_root, output_root=config.output_root,
        conversion_manifest=config.conversion_manifest, converted_root=config.converted_root)


def test_static_export_records_provenance_and_required_quality_flags(tmp_path):
    config, metadata_path = _fixture(tmp_path)
    metadata = _read(metadata_path)
    assert metadata["conversion_provenance"] == {
        "converter_version": STATIC_VERSION, "converter_fingerprint": "a" * 64,
        "recovery_reason": "office_validation_failed",
    }
    assert STATIC_FLAGS | {REASON_FLAG} <= set(metadata["quality_flags"])
    manifest = _read(config.output_root / "manifest.jsonl")
    assert manifest["quality_flags"] == metadata["quality_flags"]
    assert _verify(config).complete


@pytest.mark.parametrize("version", [None, "ms-word-16.0/batch-v1"])
def test_ordinary_conversion_is_not_mislabeled_and_old_manifest_is_compatible(tmp_path, version):
    config, metadata_path = _fixture(tmp_path, version=version, reason=None)
    metadata = _read(metadata_path)
    assert metadata["conversion_provenance"] == {
        "converter_version": version,
        "converter_fingerprint": "a" * 64 if version else None, "recovery_reason": None,
    }
    assert not (STATIC_FLAGS | {REASON_FLAG}) & set(metadata["quality_flags"])
    assert _verify(config).complete


@pytest.mark.parametrize("flag", sorted(STATIC_FLAGS | {REASON_FLAG}))
def test_missing_static_flag_is_rejected_and_export_cache_rebuilds(tmp_path, flag):
    config, metadata_path = _fixture(tmp_path)
    metadata = _read(metadata_path)
    metadata["quality_flags"] = [item for item in metadata["quality_flags"] if item != flag]
    _write(metadata_path, metadata)
    manifest = _read(config.output_root / "manifest.jsonl")
    manifest["quality_flags"] = metadata["quality_flags"]
    _write(config.output_root / "manifest.jsonl", manifest)
    assert not _verify(config).complete
    assert export_corpus(config).completed == 1
    assert flag in _read(metadata_path)["quality_flags"]
    assert _verify(config).complete


@pytest.mark.parametrize("mutation", ["missing", "version", "fingerprint", "reason"])
def test_tampered_provenance_is_rejected_and_cache_rebuilt(tmp_path, mutation):
    config, metadata_path = _fixture(tmp_path)
    metadata = _read(metadata_path)
    expected = {"converter_version": STATIC_VERSION, "converter_fingerprint": "a" * 64,
                "recovery_reason": "office_validation_failed"}
    altered = dict(expected)
    if mutation == "missing":
        metadata.pop("conversion_provenance", None)
    else:
        key = {"version": "converter_version", "fingerprint": "converter_fingerprint",
               "reason": "recovery_reason"}[mutation]
        altered[key] = None
        metadata["conversion_provenance"] = altered
    _write(metadata_path, metadata)
    assert not _verify(config).complete
    assert export_corpus(config).completed == 1
    assert _read(metadata_path)["conversion_provenance"] == expected
    assert _verify(config).complete


def test_conversion_reason_change_invalidates_cache_without_changing_input_hash(tmp_path):
    config, metadata_path = _fixture(tmp_path, reason=None)
    metadata = _read(metadata_path)
    assert REASON_FLAG not in metadata["quality_flags"]
    record = _read(config.conversion_manifest)
    record["recovery_reason"] = "office_validation_failed"
    _write(config.conversion_manifest, record)
    assert not _verify(config).complete
    assert export_corpus(config).completed == 1
    updated = _read(metadata_path)
    assert updated["input_sha256"] == metadata["input_sha256"]
    assert updated["conversion_provenance"]["recovery_reason"] == "office_validation_failed"
    assert REASON_FLAG in updated["quality_flags"]
    assert _verify(config).complete


def test_manifest_quality_flags_must_match_metadata(tmp_path):
    config, _ = _fixture(tmp_path)
    manifest_path = config.output_root / "manifest.jsonl"
    manifest = _read(manifest_path)
    manifest["quality_flags"] = []
    _write(manifest_path, manifest)
    assert not _verify(config).complete


def test_csv_delivery_index_exposes_static_visibility_and_content_limits(tmp_path, monkeypatch):
    config, _ = _fixture(tmp_path)
    script = Path(__file__).parents[3] / "scripts/build_corpus_delivery_index.py"
    monkeypatch.setattr("sys.argv", [str(script), "--output-root", str(config.output_root)])
    assert runpy.run_path(str(script))["main"]() == 0
    with (config.output_root / "文件清单.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert STATIC_FLAGS | {REASON_FLAG} <= set(rows[0]["质量标记"].split(" / "))


def test_ordinary_export_rejects_forged_recovery_flags(tmp_path):
    config, metadata_path = _fixture(tmp_path, version="ms-word-16.0/batch-v1", reason=None)
    metadata = _read(metadata_path)
    metadata["quality_flags"].append("legacy_binary_static_text_recovery")
    _write(metadata_path, metadata)
    manifest_path = config.output_root / "manifest.jsonl"
    manifest = _read(manifest_path)
    manifest["quality_flags"] = metadata["quality_flags"]
    _write(manifest_path, manifest)
    assert not _verify(config).complete
    export_corpus(config)
    assert not STATIC_FLAGS & set(_read(metadata_path)["quality_flags"])


def test_removed_recovery_reason_removes_old_quality_flag_after_reexport(tmp_path):
    config, metadata_path = _fixture(tmp_path)
    record = _read(config.conversion_manifest)
    record["recovery_reason"] = None
    _write(config.conversion_manifest, record)
    assert not _verify(config).complete
    export_corpus(config)
    assert REASON_FLAG not in _read(metadata_path)["quality_flags"]
    assert _verify(config).complete


@pytest.mark.parametrize("change", [
    {"converter_version": 123}, {"converter_fingerprint": {}},
    {"recovery_reason": "unknown"}, {"converter_version": "ms-word-16.0/batch-v1"},
])
def test_invalid_conversion_provenance_is_rejected(tmp_path, change):
    config, _ = _fixture(tmp_path)
    record = _read(config.conversion_manifest)
    record.update(change)
    _write(config.conversion_manifest, record)
    assert not _verify(config).complete
    with pytest.raises(ExportError, match="invalid_conversion_provenance"):
        export_corpus(config)


@pytest.mark.parametrize("fingerprint", ["missing", None, "", "   "])
def test_static_conversion_requires_policy_fingerprint(tmp_path, fingerprint):
    config, _ = _fixture(tmp_path)
    record = _read(config.conversion_manifest)
    if fingerprint == "missing":
        record.pop("converter_fingerprint")
    else:
        record["converter_fingerprint"] = fingerprint
    _write(config.conversion_manifest, record)
    with pytest.raises(ExportError, match="invalid_conversion_provenance"):
        export_corpus(config)
    assert not _verify(config).complete


def test_native_docx_old_cache_without_provenance_remains_valid(tmp_path, monkeypatch):
    config, _ = _fixture(tmp_path)
    source = tmp_path / "native-source"
    source.mkdir()
    native = source / "合成.docx"
    native.write_bytes((config.converted_root / "合成.docx").read_bytes())
    native_config = ExportConfig(source, tmp_path / "native-export")
    assert export_corpus(native_config).completed == 1
    metadata_path = next(native_config.output_root.glob("*/document.json"))
    metadata = _read(metadata_path)
    assert metadata.pop("conversion_provenance") is None
    _write(metadata_path, metadata)

    def no_rebuild(*args):
        raise AssertionError("unchanged native cache should not be rebuilt")

    with monkeypatch.context() as context:
        context.setattr(
            "lawyer_agent.application.legal_corpus_export.DocxExportReader.read", no_rebuild,
        )
        assert export_corpus(native_config).completed == 1
    assert verify_export(source_root=source, output_root=native_config.output_root).complete
