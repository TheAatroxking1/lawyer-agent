import hashlib
import importlib.util
import json
from pathlib import Path
from zipfile import ZipFile

import pytest


def test_source_preparation_entrypoint_exists() -> None:
    assert (
        importlib.util.find_spec("lawyer_agent.infrastructure.documents.corpus_source") is not None
    )


def write_docx(
    path: Path, body: str = "<w:p><w:r><w:t>第一条 正文</w:t></w:r></w:p>", auxiliary: bool = False
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + body
            + "</w:body></w:document>",
        )
        if auxiliary:
            archive.writestr(
                "word/header1.xml",
                '<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>页眉</w:t></w:r></w:p></w:hdr>',
            )
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def converted(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, object]]:
    source = tmp_path / "sources" / "原件.doc"
    source.parent.mkdir()
    source.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"synthetic")
    target = write_docx(tmp_path / "derived" / "原件.docx")
    record: dict[str, object] = dict(
        schema_version="word-conversion-v1",
        source_path=str(source),
        source_sha256=digest(source),
        status="converted",
        converted_path=str(target),
        converted_sha256=digest(target),
        converter_version="word-saveas2-v1",
        converter_fingerprint="a" * 64,
    )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return source, target, manifest, record


def prepare(source: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    from lawyer_agent.infrastructure.documents.corpus_source import (
        CorpusSourceRequest,
        prepare_corpus_source,
    )

    return prepare_corpus_source(
        CorpusSourceRequest(source=source, source_root=source.parent, **kwargs)
    )  # type: ignore[arg-type]


@pytest.mark.parametrize("suffix", [".docx", ".docm"])
def test_native_preserves_original_controls_tables_and_auxiliary(
    tmp_path: Path, suffix: str
) -> None:
    source = write_docx(
        tmp_path / ("中文" + suffix),
        "<w:p><w:r><w:t>甲</w:t><w:tab/><w:t>乙</w:t><w:br/><w:t>丙</w:t><w:cr/><w:t>丁</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表格</w:t></w:r></w:p></w:tc></w:tr></w:tbl>",
        True,
    )
    result = prepare(source)
    assert [p.text for p in result.document.paragraphs] == ["甲\t乙\n丙\n丁", "表格"]
    assert [p.text for p in result.auxiliary_paragraphs] == ["页眉"]
    assert result.document.source_ref == source.as_uri()
    assert result.document.source_sha256 == bytes.fromhex(digest(source))
    assert result.source_sha256 == result.input_sha256 == digest(source)
    assert result.conversion_provenance is None
    assert "word_control_characters_preserved" in result.quality_flags
    if suffix == ".docm":
        assert "macro_enabled_container_no_execution" in result.quality_flags


@pytest.mark.parametrize("value", ["bad", "g" * 64, " " + "a" * 64, "a" * 64])
def test_expected_source_hash_rejected(tmp_path: Path, value: str) -> None:
    with pytest.raises(ValueError, match="source_sha256"):
        prepare(write_docx(tmp_path / "test.docx"), expected_source_sha256=value)


@pytest.mark.parametrize("body", ["", '<!DOCTYPE x [<!ENTITY x "bad">]>'])
def test_empty_and_dtd_rejected(tmp_path: Path, body: str) -> None:
    with pytest.raises(ValueError):
        prepare(write_docx(tmp_path / "test.docx", body, True))


@pytest.mark.parametrize("suffix", [".doc", ".docx"])
def test_conversion_preserves_original_identity(tmp_path: Path, suffix: str) -> None:
    source, target, manifest, record = converted(tmp_path)
    if suffix == ".docx":
        source = source.rename(source.with_suffix(suffix))
        record["source_path"] = str(source)
        manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
    result = prepare(source, conversion_manifest=manifest, converted_root=target.parent)
    assert result.input_path == target
    assert result.document.source_sha256 == bytes.fromhex(digest(source))
    assert result.source_sha256 != result.input_sha256
    assert result.document.source_ref == source.as_uri()


@pytest.mark.parametrize(
    "change",
    [
        "source_hash",
        "input_hash",
        "duplicate",
        "missing",
        "failed",
        "outside",
        "unknown",
        "static_fingerprint",
        "line_limit",
    ],
)
def test_invalid_conversion_rejected(tmp_path: Path, change: str) -> None:
    source, target, manifest, record = converted(tmp_path)
    if change == "source_hash":
        source.write_bytes(source.read_bytes() + b"changed")
    elif change == "input_hash":
        target.write_bytes(target.read_bytes() + b"changed")
    elif change == "failed":
        record["status"] = "failed"
    elif change == "outside":
        record["converted_path"] = str(tmp_path / "outside.docx")
    elif change == "unknown":
        record["unexpected"] = True
    elif change == "static_fingerprint":
        record.update(converter_version="binary-word-static-text-v1", converter_fingerprint=None)
    payload = json.dumps(record) + "\n"
    if change == "duplicate":
        payload *= 2
    elif change == "missing":
        payload = ""
    elif change == "line_limit":
        payload = " " * (1024 * 1024 + 1)
    manifest.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError):
        prepare(source, conversion_manifest=manifest, converted_root=target.parent)


def test_static_provenance_and_unrelated_failure(tmp_path: Path) -> None:
    source, target, manifest, record = converted(tmp_path)
    record.update(
        converter_version="binary-word-static-text-v1", recovery_reason="office_validation_failed"
    )
    failure = dict(
        record,
        source_path=str(source.parent / "other.doc"),
        status="failed",
        converted_path=None,
        converted_sha256=None,
    )
    manifest.write_text(json.dumps(failure) + "\n" + json.dumps(record) + "\n", encoding="utf-8")
    result = prepare(source, conversion_manifest=manifest, converted_root=target.parent)
    assert "legacy_binary_static_text_recovery" in result.quality_flags
    assert "original_office_validation_failed" in result.quality_flags


def test_rejects_source_outside_root_and_overlap(tmp_path: Path) -> None:
    from lawyer_agent.infrastructure.documents.corpus_source import (
        CorpusSourceRequest,
        prepare_corpus_source,
    )

    source = write_docx(tmp_path / "sources" / "test.docx")
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ValueError, match="source_outside_root"):
        prepare_corpus_source(CorpusSourceRequest(source, other))
    with pytest.raises(ValueError):
        prepare(source, converted_root=source.parent)


@pytest.mark.parametrize("changed", ["source", "input"])
def test_detects_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader

    source, target, manifest, _ = converted(tmp_path)
    original = DocxExportReader.read_bytes

    def read(self: DocxExportReader, payload: bytes, *, source_suffix: str = ".docx"):  # type: ignore[no-untyped-def]
        result = original(self, payload, source_suffix=source_suffix)
        selected = source if changed == "source" else target
        selected.write_bytes(selected.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(DocxExportReader, "read_bytes", read)
    with pytest.raises(ValueError, match="changed_during_read"):
        prepare(source, conversion_manifest=manifest, converted_root=target.parent)


def test_rejects_non_word_and_ole_docm(tmp_path: Path) -> None:
    for suffix in (".txt", ".docm"):
        source = tmp_path / ("test" + suffix)
        source.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1"))
        with pytest.raises(ValueError):
            prepare(source)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", "unexpected"),
        ("source_path", "relative.doc"),
        ("source_sha256", "x" * 64),
        ("converter_version", "   "),
        ("converter_fingerprint", ""),
        ("converted_sha256", "z" * 64),
    ],
)
def test_rejects_invalid_typed_record(tmp_path: Path, field: str, value: str) -> None:
    source, target, manifest, record = converted(tmp_path)
    record[field] = value
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        prepare(source, conversion_manifest=manifest, converted_root=target.parent)


@pytest.mark.parametrize("redirected", ["source", "root", "input", "derived_root", "manifest"])
def test_rejects_windows_reparse_before_content_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, redirected: str
) -> None:
    import stat
    from types import SimpleNamespace

    from lawyer_agent.infrastructure.documents import corpus_source

    source, target, manifest, _ = converted(tmp_path)
    blocked = {
        "source": source,
        "root": source.parent,
        "input": target,
        "derived_root": target.parent,
        "manifest": manifest,
    }[redirected]
    original = Path.lstat

    def lstat(path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        result = original(path)
        if path == blocked:
            return SimpleNamespace(
                st_mode=result.st_mode, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
        return result

    monkeypatch.setattr(Path, "lstat", lstat)
    with pytest.raises(corpus_source.CorpusSourceError, match="unsafe_reparse_path"):
        prepare(source, conversion_manifest=manifest, converted_root=target.parent)


def test_native_docm_does_not_consult_conversion_manifest(tmp_path: Path) -> None:
    source = write_docx(tmp_path / "source" / "原件.docm")
    root = tmp_path / "derived"
    root.mkdir()
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("not json", encoding="utf-8")
    result = prepare(source, conversion_manifest=manifest, converted_root=root)
    assert result.input_path == source
    assert result.conversion_provenance is None


def test_hash_case_and_explicit_configuration(tmp_path: Path) -> None:
    source = write_docx(tmp_path / "source" / "原件.docx")
    assert prepare(source, expected_source_sha256=digest(source).upper()).source_sha256 == digest(
        source
    )
    with pytest.raises(ValueError, match="incomplete_conversion_configuration"):
        prepare(source, converted_root=tmp_path / "derived")


def test_conversion_roots_cannot_overlap(tmp_path: Path) -> None:
    source = write_docx(tmp_path / "source" / "原件.docx")
    with pytest.raises(ValueError, match="source_converted_roots_overlap"):
        prepare(source, converted_root=source.parent, conversion_manifest=tmp_path / "manifest")


@pytest.mark.parametrize("fingerprint", [None, "", "   ", "invalid", "g" * 64])
def test_converted_record_requires_sha256_fingerprint(
    tmp_path: Path, fingerprint: str | None
) -> None:
    source, target, manifest, record = converted(tmp_path)
    record["converter_fingerprint"] = fingerprint
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        prepare(source, conversion_manifest=manifest, converted_root=target.parent)


@pytest.mark.parametrize("legacy", [False, True])
def test_aba_replacement_cannot_change_prepared_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    from lawyer_agent.infrastructure.documents.export_reader import DocxExportReader

    source, target, manifest, _ = converted(tmp_path)
    selected = target if legacy else write_docx(tmp_path / "native" / "原件.docx")
    original_bytes = selected.read_bytes()
    replacement = write_docx(
        tmp_path / "other.docx", "<w:p><w:r><w:t>替换正文</w:t></w:r></w:p>"
    ).read_bytes()
    old_read = DocxExportReader.read
    old_read_bytes = getattr(DocxExportReader, "read_bytes", None)

    def read(self: DocxExportReader, path: Path):  # type: ignore[no-untyped-def]
        selected.write_bytes(replacement)
        try:
            return old_read(self, path)
        finally:
            selected.write_bytes(original_bytes)

    def read_bytes(self: DocxExportReader, payload: bytes, *, source_suffix: str = ".docx"):  # type: ignore[no-untyped-def]
        selected.write_bytes(replacement)
        try:
            return old_read_bytes(self, payload, source_suffix=source_suffix)
        finally:
            selected.write_bytes(original_bytes)

    monkeypatch.setattr(DocxExportReader, "read", read)
    if old_read_bytes is not None:
        monkeypatch.setattr(DocxExportReader, "read_bytes", read_bytes)
    result = (
        prepare(source, conversion_manifest=manifest, converted_root=target.parent)
        if legacy
        else prepare(selected)
    )
    assert result.document.paragraphs[0].text == "第一条 正文"
    assert result.input_sha256 == hashlib.sha256(original_bytes).hexdigest()


def test_snapshot_input_size_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lawyer_agent.infrastructure.documents import corpus_source

    source = write_docx(tmp_path / "原件.docx")
    monkeypatch.setattr(corpus_source, "_MAX_INPUT_BYTES", source.stat().st_size - 1, raising=False)
    with pytest.raises(ValueError, match="corpus_input_size_limit"):
        prepare(source)
