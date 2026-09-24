"""Reviewed import manifests are validated completely before business work."""

import json
from datetime import date
from hashlib import sha256
from pathlib import Path

import pytest
from pydantic import ValidationError

from lawyer_agent.domain.legal_corpus import LegalCategory, LegalVersionStatus
from lawyer_agent.infrastructure.documents import import_manifest as module
from lawyer_agent.infrastructure.documents.import_manifest import (
    ImportManifestError,
    read_import_manifest,
)


@pytest.fixture(autouse=True)
def source_directory(tmp_path: Path) -> None:
    (tmp_path / "source").mkdir()


def row(root: Path, **changes: object) -> dict[str, object]:
    return {
        "schema_version": "legal-corpus-import-v1",
        "review_status": "reviewed",
        "metadata_review_ref": "人工核对记录-1",
        "source_path": str(root / "合成.docx"),
        "source_sha256": "a" * 64,
        "title": "合成法规",
        "issuing_authority": "合成机关",
        "jurisdiction": "CN",
        **changes,
    }


def write(tmp_path: Path, rows: list[dict[str, object]]) -> Path:
    target = tmp_path / "import.jsonl"
    target.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    return target


def test_reviewed_current_keeps_unknown_dates(tmp_path):
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root, status="current", date_review_ref="review:dates")])
    item = read_import_manifest(manifest, root).rows[0]
    assert item.date_review_ref == "review:dates"
    assert item.published_on is None and item.effective_on is None


@pytest.mark.parametrize("status,ref", [
    ("current", " "), ("current", "x" * 513), ("current", True),
    ("draft", "review:dates"), ("repealed", "review:dates"),
    ("status_unknown", "review:dates"),
])
def test_date_review_is_explicit_and_current_only(tmp_path, status, ref):
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root, status=status, date_review_ref=ref)])
    with pytest.raises(ImportManifestError):
        read_import_manifest(manifest, root)


def test_snapshot_keeps_unknown_metadata_and_does_not_read_source(tmp_path: Path) -> None:
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root)])
    result = read_import_manifest(manifest, root)
    assert result.path == manifest
    assert result.sha256 == sha256(manifest.read_bytes()).hexdigest()
    assert isinstance(result.rows, tuple)
    assert result.rows[0].category is LegalCategory.UNKNOWN
    assert result.rows[0].status is LegalVersionStatus.STATUS_UNKNOWN
    assert result.rows[0].effective_on is None
    with pytest.raises(ValidationError):
        result.rows[0].title = "changed"


@pytest.mark.parametrize("changes", [
    {"review_status": "pending"}, {"schema_version": "candidate-v1"},
    {"metadata_review_ref": "  "}, {"title": " "}, {"title": "x" * 513},
    {"title": 1}, {"unexpected": "sensitive-body"}, {"source_sha256": "A" * 64},
    {"status": "current"}, {"published_on": "20260906"},
    {"published_on": 1788652800}, {"published_on": "2026-09-06T00:00:00"},
    {"category": "invented"}, {"region_code": " "},
])
def test_invalid_rows_have_stable_redacted_errors(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    root = tmp_path / "source"
    manifest = write(
        tmp_path, [row(root, source_path=str(root / "first.doc")), row(root, **changes)]
    )
    with pytest.raises(ImportManifestError) as caught:
        read_import_manifest(manifest, root)
    assert str(caught.value) == caught.value.code == "invalid_import_manifest_row"
    assert "sensitive-body" not in str(caught.value)


@pytest.mark.parametrize("field", ["schema_version", "review_status", "metadata_review_ref"])
def test_review_fields_must_be_explicit(tmp_path: Path, field: str) -> None:
    root = tmp_path / "source"
    value = row(root)
    del value[field]
    with pytest.raises(ImportManifestError):
        read_import_manifest(write(tmp_path, [value]), root)


def test_strict_json_allows_iso_dates_and_domain_enums(tmp_path: Path) -> None:
    root = tmp_path / "source"
    value = row(
        root, status="current", published_on="2026-09-06",
        effective_on="2026-09-07", category="law",
    )
    result = read_import_manifest(write(tmp_path, [value]), root)
    assert result.rows[0].published_on == date(2026, 9, 6)
    assert result.rows[0].effective_on == date(2026, 9, 7)
    assert result.rows[0].category is LegalCategory.LAW


@pytest.mark.parametrize("name", ["../escape.docx", "~$temp.docx", "bad.pdf", "bad.docx:ads"])
def test_unsafe_source_paths_are_rejected(tmp_path: Path, name: str) -> None:
    root = tmp_path / "source"
    with pytest.raises(ImportManifestError):
        read_import_manifest(write(tmp_path, [row(root, source_path=str(root / name))]), root)


def test_relative_source_and_arguments_are_rejected(tmp_path: Path) -> None:
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root, source_path="relative.docx")])
    with pytest.raises(ImportManifestError):
        read_import_manifest(manifest, root)
    with pytest.raises(ImportManifestError):
        read_import_manifest(Path("import.jsonl"), root)
    with pytest.raises(ImportManifestError):
        read_import_manifest(manifest, Path("source"))


def test_windows_casefold_duplicate_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root), row(root, source_path=str(root / "合成.DOCX"))])
    with pytest.raises(ImportManifestError, match="duplicate"):
        read_import_manifest(manifest, root)


@pytest.mark.parametrize("content", [b"", b"\n \n", b"\xff", b"[]", b"{broken}"])
def test_invalid_snapshot_is_rejected(tmp_path: Path, content: bytes) -> None:
    manifest = tmp_path / "import.jsonl"
    manifest.write_bytes(content)
    with pytest.raises(ImportManifestError):
        read_import_manifest(manifest, tmp_path / "source")


@pytest.mark.parametrize(
    "limit", ["_MAX_MANIFEST_BYTES", "_MAX_MANIFEST_LINE", "_MAX_MANIFEST_ROWS"]
)
def test_limits_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root), row(root, source_path=str(root / "second.doc"))])
    monkeypatch.setattr(module, limit, 1)
    with pytest.raises(ImportManifestError, match="limit"):
        read_import_manifest(manifest, root)


def test_reparse_rejection_is_redacted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root)])

    def reject(path: Path) -> Path:
        raise ValueError("secret-path")

    monkeypatch.setattr(module, "unredirected_path", reject)
    with pytest.raises(ImportManifestError, match="unsafe_reparse_path") as caught:
        read_import_manifest(manifest, root)
    assert "secret-path" not in str(caught.value)


@pytest.mark.parametrize("target", ["manifest", "root", "source"])
def test_each_input_path_is_checked_for_redirection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root)])
    original = module.unredirected_path
    rejected = {"manifest": manifest, "root": root, "source": root / "合成.docx"}[target]

    def check(path: Path) -> Path:
        if path == rejected:
            raise ValueError("unsafe_reparse_path")
        return original(path)

    monkeypatch.setattr(module, "unredirected_path", check)
    with pytest.raises(ImportManifestError, match="unsafe_reparse_path"):
        read_import_manifest(manifest, root)


def test_snapshot_allows_blank_lines_and_all_word_extensions(tmp_path: Path) -> None:
    root = tmp_path / "source"
    manifest = write(tmp_path, [row(root, source_path=str(root / f"file{ext}"))
                                for ext in (".doc", ".docx", ".docm")])
    content = b"\n" + manifest.read_bytes() + b"\n \n"
    manifest.write_bytes(content)
    result = read_import_manifest(manifest, root)
    assert len(result.rows) == 3
    assert result.sha256 == sha256(content).hexdigest()


def test_missing_manifest_has_stable_error(tmp_path: Path) -> None:
    with pytest.raises(ImportManifestError, match="import_manifest_read_failed"):
        read_import_manifest(tmp_path / "missing.jsonl", tmp_path / "source")


@pytest.mark.parametrize("missing", ["published_on", "effective_on"])
def test_current_requires_both_legal_dates(tmp_path: Path, missing: str) -> None:
    root = tmp_path / "source"
    value = row(root, status="current", published_on="2026-09-06", effective_on="2026-09-07")
    del value[missing]
    with pytest.raises(ImportManifestError, match="invalid_import_manifest_row"):
        read_import_manifest(write(tmp_path, [value]), root)


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_source_root_must_be_existing_directory(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "invalid-root"
    if kind == "file":
        root.write_text("synthetic", encoding="utf-8")
    with pytest.raises(ImportManifestError, match="invalid_source_root"):
        read_import_manifest(write(tmp_path, [row(root)]), root)
