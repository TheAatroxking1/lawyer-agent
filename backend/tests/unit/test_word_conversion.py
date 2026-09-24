from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from lawyer_agent.infrastructure.documents.word_conversion import convert_document


@dataclass
class FakeConverter:
    action: Callable[[Path, Path], str]
    fingerprint: str = "synthetic-converter-v1"

    def __call__(self, source: Path, target: Path) -> str:
        return self.action(source, target)


def _docx() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r>'
            "<w:t>第一条 合成转换内容。</w:t></w:r></w:p></w:body></w:document>",
        )
    return stream.getvalue()


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "合成.doc"
    source.write_bytes(b"synthetic legacy input")
    return source, source_root, tmp_path / "converted"


def test_conversion_records_original_and_derived_hashes_and_resumes(paths: tuple[Path, Path, Path]):
    source, root, output = paths
    calls: list[Path] = []

    def driver(input_path: Path, target: Path) -> str:
        calls.append(input_path)
        target.write_bytes(_docx())
        return "test-word-16"

    result = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver)
    )
    assert result.status == "converted"
    assert result.source_path == str(source.resolve())
    assert result.source_sha256 != result.converted_sha256
    assert result.schema_version == "word-conversion-v1"
    assert result.converter_version == "test-word-16"
    assert Path(result.converted_path).is_relative_to(output)
    assert source.read_bytes() == b"synthetic legacy input"
    assert (
        convert_document(
            source, source_root=root, output_root=output, converter=FakeConverter(driver)
        )
        == result
    )
    assert calls == [source]


def test_corrupt_output_is_rebuilt(paths: tuple[Path, Path, Path]):
    source, root, output = paths

    def driver(input_path: Path, target: Path) -> str:
        target.write_bytes(_docx())
        return "test"

    first = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver)
    )
    Path(first.converted_path).write_bytes(b"corrupt")
    second = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver)
    )
    assert second.status == "converted"
    assert Path(second.converted_path).read_bytes() == _docx()


def test_recovery_reason_is_persisted_and_part_of_cache_identity(paths):
    source, root, output = paths
    calls = []

    def driver(input_path, target):
        calls.append(input_path)
        target.write_bytes(_docx())
        return "binary-word-static-text-v1"

    converter = FakeConverter(driver)
    recovered = convert_document(
        source, source_root=root, output_root=output, converter=converter,
        recovery_reason="office_validation_failed",
    )
    assert recovered.recovery_reason == "office_validation_failed"
    assert convert_document(
        source, source_root=root, output_root=output, converter=converter,
        recovery_reason="office_validation_failed",
    ) == recovered
    ordinary = convert_document(source, source_root=root, output_root=output, converter=converter)
    assert ordinary.recovery_reason is None
    assert len(calls) == 2


def test_invalid_recovery_reason_is_rejected_before_conversion(paths):
    source, root, output = paths
    with pytest.raises(ValueError, match="recovery_reason"):
        convert_document(
            source, source_root=root, output_root=output, converter=None,
            recovery_reason="unverified_guess",
        )
    assert not output.exists()


def test_failed_static_attempt_keeps_recovery_reason(paths):
    source, root, output = paths

    def driver(input_path, target):
        raise ValueError("unsupported static text")

    result = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver),
        recovery_reason="office_validation_failed",
    )
    assert result.status == "failed"
    assert result.recovery_reason == "office_validation_failed"


def test_static_recovery_cache_rejects_an_ordinary_converter_version(paths):
    source, root, output = paths
    calls = []

    def driver(input_path, target):
        calls.append(input_path)
        target.write_bytes(_docx())
        return "binary-word-static-text-v1"

    converter = FakeConverter(driver)
    recovered = convert_document(
        source, source_root=root, output_root=output, converter=converter,
        recovery_reason="office_validation_failed",
    )
    path = Path(recovered.converted_path).parent / "record.json"
    tampered = recovered.model_copy(update={"converter_version": "ms-word-16"})
    path.write_text(tampered.model_dump_json(), encoding="utf-8")
    rebuilt = convert_document(
        source, source_root=root, output_root=output, converter=converter,
        recovery_reason="office_validation_failed",
    )
    assert rebuilt.converter_version == "binary-word-static-text-v1"
    assert len(calls) == 2


def test_office_rejection_reason_cannot_label_an_ordinary_conversion(paths):
    source, root, output = paths

    def driver(input_path, target):
        target.write_bytes(_docx())
        return "ms-word-16"

    result = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver),
        recovery_reason="office_validation_failed",
    )
    assert result.status == "failed"
    assert result.converted_path is None


@pytest.mark.parametrize("relative", [".", "nested"])
def test_output_inside_source_is_rejected_before_driver(paths, relative):
    source, root, _ = paths
    with pytest.raises(ValueError, match="overlap"):
        convert_document(source, source_root=root, output_root=root / relative, converter=None)


def test_source_outside_root_is_rejected(paths):
    source, root, output = paths
    with pytest.raises(ValueError, match="source"):
        convert_document(source, source_root=root / "child", output_root=output, converter=None)


def test_failed_conversion_never_records_success(paths):
    source, root, output = paths

    def driver(input_path: Path, target: Path) -> str:
        target.write_bytes(b"partial")
        raise TimeoutError("unsafe details must not be persisted")

    result = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver)
    )
    assert result.status == "failed"
    assert result.error_code == "conversion_timeout"
    assert result.converted_path is None
    assert not list(output.rglob("*.docx"))
    assert "unsafe details" not in "".join(p.read_text() for p in output.rglob("*.json"))


def test_invalid_docx_rejected(paths):
    source, root, output = paths

    def driver(input_path: Path, target: Path) -> str:
        target.write_bytes(b"not docx")
        return "test"

    result = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver)
    )
    assert result.status == "failed"
    assert result.error_code == "converted_document_invalid"


def test_input_changed_during_conversion_is_not_published(paths):
    source, root, output = paths

    def driver(input_path: Path, target: Path) -> str:
        source.write_bytes(b"concurrent changed input")
        target.write_bytes(_docx())
        return "test"

    result = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(driver)
    )
    assert result.status == "failed"
    assert result.error_code == "source_changed"
    assert result.converted_path is None


def test_non_doc_is_rejected(paths):
    source, root, output = paths
    other = source.with_suffix(".zip")
    other.write_bytes(b"zip")
    with pytest.raises(ValueError, match="source"):
        convert_document(other, source_root=root, output_root=output, converter=None)


def test_cache_rebuilds_when_converter_fingerprint_changes(paths):
    source, root, output = paths
    calls = []

    def action(input_path, target):
        calls.append(input_path)
        target.write_bytes(_docx())
        return "same-runtime-version"

    first = convert_document(
        source,
        source_root=root,
        output_root=output,
        converter=FakeConverter(action, "worker-hash-A/config-A"),
    )
    second = convert_document(
        source,
        source_root=root,
        output_root=output,
        converter=FakeConverter(action, "worker-hash-B/config-A"),
    )
    assert len(calls) == 2
    assert first.converter_fingerprint != second.converter_fingerprint
    convert_document(
        source,
        source_root=root,
        output_root=output,
        converter=FakeConverter(action, "worker-hash-B/config-A"),
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "xml",
    [
        b"<root/>",
        b"<document><body><p><t>wrong namespace</t></p></body></document>",
        b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>',
        (
            '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY e "text">]>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>&e;</w:t></w:r></w:p></w:body></w:document>"
        ).encode("utf-16"),
    ],
)
def test_invalid_word_structure_or_dtd_never_converted(paths, xml):
    source, root, output = paths

    def action(input_path, target):
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("word/document.xml", xml)
        return "synthetic"

    result = convert_document(
        source, source_root=root, output_root=output, converter=FakeConverter(action)
    )
    assert result.status == "failed"
    assert result.error_code == "converted_document_invalid"
    assert result.converted_path is None


def test_legacy_record_without_fingerprint_is_rebuilt(paths):
    source, root, output = paths
    calls = []

    def action(input_path, target):
        calls.append(input_path)
        target.write_bytes(_docx())
        return "same-version"

    converter = FakeConverter(action)
    convert_document(source, source_root=root, output_root=output, converter=converter)
    record_path = next(output.glob("*/record.json"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.pop("converter_fingerprint")
    record_path.write_text(json.dumps(record), encoding="utf-8")
    result = convert_document(source, source_root=root, output_root=output, converter=converter)
    assert len(calls) == 2
    assert result.converter_fingerprint == converter.fingerprint


def test_converter_without_fingerprint_is_rejected_before_output(paths):
    source, root, output = paths
    with pytest.raises(ValueError, match="fingerprint"):
        convert_document(source, source_root=root, output_root=output, converter=lambda a, b: "x")
    assert not output.exists()
