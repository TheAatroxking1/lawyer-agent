import hashlib
import json
from pathlib import Path

import pytest

from lawyer_agent.application.conversion_provenance import ConversionProvenance
from lawyer_agent.infrastructure.documents import corpus_source
from lawyer_agent.infrastructure.documents.word_conversion import ConversionRecord
from tests.unit.test_corpus_source import converted, prepare, write_docx


def test_catalog_snapshot_is_read_once_and_immutable(tmp_path: Path, monkeypatch) -> None:
    source, target, manifest, record = converted(tmp_path)
    second = source.with_name("第二份.doc")
    second.write_bytes(source.read_bytes())
    manifest.write_text(
        json.dumps(record) + "\n" + json.dumps(dict(record, source_path=str(second))) + "\n",
        encoding="utf-8",
    )
    payload = manifest.read_bytes()
    original = Path.open
    reads = []

    def opened(path, *args, **kwargs):
        if path == manifest:
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", opened)
    catalog = corpus_source.load_conversion_catalog(manifest, source.parent, target.parent)
    assert catalog.manifest_sha256 == hashlib.sha256(payload).hexdigest()
    assert catalog.manifest_path == manifest
    for selected in (source, second):
        assert (
            prepare(
                selected,
                conversion_manifest=manifest,
                converted_root=target.parent,
                conversion_catalog=catalog,
            ).input_path
            == target
        )
    assert reads == [manifest]
    with pytest.raises(TypeError):
        catalog.records[source] = catalog.records[source]
    with pytest.raises(ValueError):
        catalog.records[source][0].source_sha256 = "0" * 64


@pytest.mark.parametrize("changed", ["manifest", "source", "input"])
def test_catalog_binds_manifest_but_rechecks_files(tmp_path: Path, changed: str) -> None:
    source, target, manifest, _ = converted(tmp_path)
    catalog = corpus_source.load_conversion_catalog(manifest, source.parent, target.parent)
    selected = {"manifest": manifest, "source": source, "input": target}[changed]
    selected.write_bytes(selected.read_bytes() + b"changed")
    if changed == "manifest":
        assert (
            prepare(
                source,
                conversion_manifest=manifest,
                converted_root=target.parent,
                conversion_catalog=catalog,
            ).input_path
            == target
        )
    else:
        with pytest.raises(ValueError, match=f"conversion_{changed}_sha256_mismatch"):
            prepare(
                source,
                conversion_manifest=manifest,
                converted_root=target.parent,
                conversion_catalog=catalog,
            )


@pytest.mark.parametrize("field", ["conversion_manifest", "source_root", "converted_root"])
def test_catalog_request_configuration_must_match(tmp_path: Path, field: str) -> None:
    source, target, manifest, _ = converted(tmp_path)
    catalog = corpus_source.load_conversion_catalog(manifest, source.parent, target.parent)
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    kwargs = dict(
        source=source,
        source_root=source.parent,
        conversion_manifest=manifest,
        converted_root=target.parent,
        conversion_catalog=catalog,
    )
    kwargs[field] = alternate
    with pytest.raises(ValueError):
        corpus_source.prepare_corpus_source(corpus_source.CorpusSourceRequest(**kwargs))


@pytest.mark.parametrize("change", ["duplicate", "unknown", "size", "line", "count"])
def test_catalog_rejects_invalid_or_unbounded_manifest(tmp_path: Path, monkeypatch, change) -> None:
    source, target, manifest, record = converted(tmp_path)
    if change == "duplicate":
        manifest.write_text((json.dumps(record) + "\n") * 2, encoding="utf-8")
    elif change == "unknown":
        record["unknown"] = True
        manifest.write_text(json.dumps(record), encoding="utf-8")
    else:
        limit = {
            "size": "_MAX_MANIFEST_BYTES",
            "line": "_MAX_MANIFEST_LINE",
            "count": "_MAX_MANIFEST_ROWS",
        }[change]
        monkeypatch.setattr(corpus_source, limit, 0, raising=False)
    with pytest.raises(ValueError):
        corpus_source.load_conversion_catalog(manifest, source.parent, target.parent)


def test_catalog_is_optional_for_native_input(tmp_path: Path) -> None:
    source = write_docx(tmp_path / "sources" / "原件.docm")
    derived = tmp_path / "derived"
    derived.mkdir()
    assert prepare(source, conversion_manifest=tmp_path / "absent", converted_root=derived)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_path", "relative.doc"),
        ("source_sha256", "x" * 64),
        ("converted_path", "relative.docx"),
        ("converted_sha256", "z" * 64),
        ("converter_fingerprint", None),
        ("converter_version", "   "),
        ("recovery_reason", "unapproved"),
        ("status", "unrecognized"),
        ("error_code", "unexpected"),
    ],
)
def test_catalog_validates_unselected_records(tmp_path: Path, field, value) -> None:
    source, target, manifest, record = converted(tmp_path)
    other = dict(record, source_path=str(source.with_name("其他.doc")))
    other[field] = value
    manifest.write_text(json.dumps(record) + "\n" + json.dumps(other), encoding="utf-8")
    with pytest.raises(ValueError):
        corpus_source.load_conversion_catalog(manifest, source.parent, target.parent)


def test_catalog_rejects_duplicate_unselected_source(tmp_path: Path) -> None:
    source, target, manifest, record = converted(tmp_path)
    other = dict(record, source_path=str(source.with_name("其他.doc")))
    manifest.write_text(
        json.dumps(record) + "\n" + (json.dumps(other) + "\n") * 2, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="duplicate_conversion_source"):
        prepare(source, conversion_manifest=manifest, converted_root=target.parent)


@pytest.mark.parametrize("changed", ["source", "input"])
def test_catalog_still_rejects_mutation_during_parsing(
    tmp_path: Path, monkeypatch, changed
) -> None:
    from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader

    source, target, manifest, _ = converted(tmp_path)
    catalog = corpus_source.load_conversion_catalog(manifest, source.parent, target.parent)
    original = DocxExportReader.read_bytes

    def read(self, payload, *, source_suffix=".docx"):
        result = original(self, payload, source_suffix=source_suffix)
        selected = source if changed == "source" else target
        selected.write_bytes(selected.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(DocxExportReader, "read_bytes", read)
    with pytest.raises(ValueError, match="changed_during_read"):
        prepare(
            source,
            conversion_manifest=manifest,
            converted_root=target.parent,
            conversion_catalog=catalog,
        )


def test_handmade_catalog_cannot_read_outside_converted_root(tmp_path: Path) -> None:
    source, target, manifest, record = converted(tmp_path)
    outside = write_docx(tmp_path / "outside.docx")
    record.update(
        converted_path=str(outside),
        converted_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
    )
    typed = ConversionRecord.model_validate(record)
    catalog = corpus_source.CorpusConversionCatalog(
        manifest,
        "a" * 64,
        source.parent,
        target.parent,
        {source: (typed, outside, ConversionProvenance.from_record(record))},
    )
    with pytest.raises(ValueError, match="conversion_input_outside_root"):
        prepare(
            source,
            conversion_manifest=manifest,
            converted_root=target.parent,
            conversion_catalog=catalog,
        )


def test_handmade_catalog_cannot_remove_static_quality_flags(tmp_path: Path) -> None:
    source, target, manifest, record = converted(tmp_path)
    record.update(
        converter_version="binary-word-static-text-v1", recovery_reason="office_validation_failed"
    )
    typed = ConversionRecord.model_validate(record)
    fake_provenance = ConversionProvenance("word-saveas2-v1", "a" * 64, None)
    catalog = corpus_source.CorpusConversionCatalog(
        manifest,
        "a" * 64,
        source.parent,
        target.parent,
        {source: (typed, target, fake_provenance)},
    )
    result = prepare(
        source,
        conversion_manifest=manifest,
        converted_root=target.parent,
        conversion_catalog=catalog,
    )
    assert "legacy_binary_static_text_recovery" in result.quality_flags
    assert "original_office_validation_failed" in result.quality_flags


@pytest.mark.parametrize("change", ["source", "fingerprint", "status", "error"])
def test_handmade_catalog_revalidates_selected_record(tmp_path: Path, change: str) -> None:
    source, target, manifest, record = converted(tmp_path)
    typed = ConversionRecord.model_validate(record)
    updates = {
        "source": {"source_path": str(source.with_name("其他.doc"))},
        "fingerprint": {"converter_fingerprint": None},
        "status": {"status": "unknown"},
        "error": {"error_code": "unexpected"},
    }[change]
    corrupted = typed.model_copy(update=updates)
    catalog = corpus_source.CorpusConversionCatalog(
        manifest,
        "a" * 64,
        source.parent,
        target.parent,
        {source: (corrupted, target, ConversionProvenance.from_record(record))},
    )
    with pytest.raises(ValueError):
        prepare(
            source,
            conversion_manifest=manifest,
            converted_root=target.parent,
            conversion_catalog=catalog,
        )
