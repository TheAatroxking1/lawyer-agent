from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def source_row(root, relative, *, status="completed", digest="a" * 64):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"synthetic source; content must not be read")
    return {
        "schema_version": "legal-corpus-export-v2", "id_namespace": "offline-export-v1",
        "document_id": "synthetic-offline-id", "source_path": str(path),
        "source_relative_path": relative, "source_sha256": digest,
        "status": status, "code": status,
    }


@pytest.fixture
def paths(tmp_path):
    root = tmp_path / "来源"
    root.mkdir()
    return root, tmp_path / "export/manifest.jsonl", tmp_path / "candidates/清单.jsonl"


def generate(paths, rows, **kwargs):
    from lawyer_agent.application.legal_corpus_manifest import prepare_corpus_manifest

    root, manifest, output = paths
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                        encoding="utf-8")
    return prepare_corpus_manifest(source_root=root, manifest_path=manifest,
                                   output_path=output, **kwargs)


def read_candidates(output):
    return [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]


def test_filename_metadata_stays_unconfirmed_and_utf8_roundtrips(paths):
    root, _, output = paths
    row = source_row(root, "司法解释/最高人民法院+最高人民检察院合成解释_20240229.docx")
    summary = generate(paths, [row])
    assert summary.selected_sources == 1
    candidate = read_candidates(output)[0]
    assert candidate["title_candidate"] == "最高人民法院、最高人民检察院合成解释"
    assert candidate["category_candidate"] == "judicial_interpretation"
    assert candidate["issuing_authority_candidate"] == "最高人民法院、最高人民检察院"
    assert candidate["filename_date_candidate"] == "2024-02-29"
    assert candidate["published_on"] is candidate["effective_on"] is None
    assert candidate["region_code"] is None
    assert candidate["status"] == "status_unknown"
    assert candidate["review_status"] == candidate["hash_verification"] == "pending"
    assert candidate["source_sha256"] == "a" * 64
    assert candidate["source_filename"] == Path(row["source_path"]).name
    assert "document_id" not in candidate and "version_id" not in candidate
    assert "最高人民法院" in output.read_text(encoding="utf-8")


@pytest.mark.parametrize(("name", "date", "issue"), [
    ("合成_20230229.docx", None, "invalid_filename_date"),
    ("合成_20241301.doc", None, "invalid_filename_date"),
    ("合成_2024010.docm", None, "filename_date_missing"),
    ("合成.docx", None, "filename_date_missing"),
    ("合成_20240101.docx", "2024-01-01", None),
])
def test_dates_are_strict_candidates_only(paths, name, date, issue):
    generate(paths, [source_row(paths[0], "法律/" + name)])
    candidate = read_candidates(paths[2])[0]
    assert candidate["filename_date_candidate"] == date
    if issue:
        assert issue in candidate["issues"]


def test_uncompleted_sources_and_missing_failed_hash_are_retained(paths):
    root, _, output = paths
    rows = [source_row(root, "宪法/甲.docx"),
            source_row(root, "行政法规/乙.doc", status="pending_conversion"),
            source_row(root, "其他/丙.docx", status="failed", digest=None)]
    generate(paths, rows)
    candidates = {c["source_filename"]: c for c in read_candidates(output)}
    assert "source_not_completed" in candidates["乙.doc"]["issues"]
    assert "source_hash_missing" in candidates["丙.docx"]["issues"]
    assert candidates["丙.docx"]["category_candidate"] == "unknown"
    assert "category_unrecognized" in candidates["丙.docx"]["issues"]
    assert candidates["甲.docx"]["review_status"] == "pending"


def test_limit_selects_stable_sorted_validated_sources(paths):
    root, _, output = paths
    generate(paths, [source_row(root, "法律/乙.docx"), source_row(root, "法律/甲.docx")], limit=1)
    assert [c["source_relative_path"] for c in read_candidates(output)] == ["法律/乙.docx"]


@pytest.mark.parametrize("limit", [0, -1, True])
def test_invalid_limit_rejected_without_output(paths, limit):
    with pytest.raises(ValueError, match="limit"):
        generate(paths, [source_row(paths[0], "法律/合成.docx")], limit=limit)
    assert not paths[2].exists()


@pytest.mark.parametrize(("field", "value"), [
    ("schema_version", "future"), ("source_sha256", "invalid"),
    ("source_sha256", None), ("status", "published"),
    ("source_relative_path", "../escape.docx"),
])
def test_invalid_rows_preserve_existing_output_even_beyond_limit(paths, field, value):
    root, _, output = paths
    valid = source_row(root, "法律/A.docx")
    invalid = source_row(root, "法律/Z.docx")
    invalid[field] = value
    output.parent.mkdir()
    output.write_text("previous", encoding="utf-8")
    with pytest.raises(ValueError):
        generate(paths, [valid, invalid], limit=1)
    assert output.read_text(encoding="utf-8") == "previous"


def test_duplicate_source_rejected(paths):
    row = source_row(paths[0], "法律/合成.docx")
    with pytest.raises(ValueError, match="duplicate"):
        generate(paths, [row, row])
    assert not paths[2].exists()


def test_source_outside_root_rejected(paths):
    root, manifest, _ = paths
    row = source_row(root.parent, "outside.docx")
    with pytest.raises(ValueError, match="outside"):
        generate(paths, [row])
    assert manifest.exists() and not paths[2].exists()


@pytest.mark.parametrize("target", ["source", "manifest", "source_child"])
def test_output_cannot_overwrite_inputs(paths, target):
    root, manifest, _ = paths
    row = source_row(root, "法律/合成.docx")
    output = {"source": Path(row["source_path"]), "manifest": manifest,
              "source_child": root / "new.jsonl"}[target]
    with pytest.raises(ValueError, match="output|overlap"):
        generate((root, manifest, output), [row])
    assert Path(row["source_path"]).read_bytes() == b"synthetic source; content must not be read"


def test_does_not_read_source_content(paths, monkeypatch):
    root = paths[0]
    row = source_row(root, "法律/合成.docx")
    original = Path.open

    def checked_open(path, *args, **kwargs):
        assert not path.is_relative_to(root), "candidate generation must not read source content"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", checked_open)
    generate(paths, [row])


def test_cli_is_a_local_candidate_entrypoint(paths):
    from lawyer_agent.cli.corpus_manifest import main

    root, manifest, output = paths
    generate(paths, [source_row(root, "法律/合成.docx")])
    assert main(["--source-root", str(root), "--manifest", str(manifest),
                 "--output", str(output)]) == 0
    assert main(["--source-root", str(root), "--manifest", str(manifest),
                 "--output", str(output), "--limit", "0"]) == 2


def test_export_quality_flags_are_preserved_as_review_issues(paths):
    row = source_row(paths[0], "法律/合成.docx")
    row.update(output_directory="offline-folder", quality_flags=["structure_review_required"])
    generate(paths, [row])
    candidate = read_candidates(paths[2])[0]
    assert candidate["export_quality_flags"] == ["structure_review_required"]
    assert "export_quality:structure_review_required" in candidate["issues"]


@pytest.mark.parametrize(("directory", "expected"), [
    ("宪法", "constitution"), ("法律", "law"), ("行政法规", "administrative_regulation"),
    ("司法解释", "judicial_interpretation"), ("地方法规", "local_regulation"),
    ("监察法规", "supervisory_regulation"), ("其他/法律", "unknown"),
])
def test_only_confirmed_top_level_directories_supply_category(paths, directory, expected):
    generate(paths, [source_row(paths[0], directory + "/合成.docx")])
    assert read_candidates(paths[2])[0]["category_candidate"] == expected


def test_atomic_replace_failure_preserves_previous_file_and_removes_temporary(paths, monkeypatch):
    from lawyer_agent.application import legal_corpus_manifest

    output = paths[2]
    output.parent.mkdir()
    output.write_text("previous", encoding="utf-8")

    def fail(*args):
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(legal_corpus_manifest.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic"):
        generate(paths, [source_row(paths[0], "法律/合成.docx")])
    assert output.read_text(encoding="utf-8") == "previous"
    assert list(output.parent.iterdir()) == [output]


def test_output_hardlink_to_source_is_rejected(paths):
    root, _, output = paths
    row = source_row(root, "法律/合成.docx")
    output.parent.mkdir()
    os.link(row["source_path"], output)
    try:
        with pytest.raises(ValueError, match="output_overlaps_source"):
            generate(paths, [row])
    finally:
        output.unlink()


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse boundary")
@pytest.mark.parametrize("direction", ["source", "output", "manifest"])
def test_reparse_paths_are_rejected(paths, direction):
    root, manifest, output = paths
    row = source_row(root, "法律/合成.docx")
    external = root.parent / "external"
    external.mkdir()
    link = root.parent / "redirected"
    executable = shutil.which("powershell.exe")
    assert executable
    result = subprocess.run(  # noqa: S603 - fixed command, only synthetic test paths in env
        [executable, "-NoProfile", "-Command",
         "New-Item -ItemType Junction -Path $env:TEST_MANIFEST_LINK "
         "-Target $env:TEST_MANIFEST_TARGET | Out-Null"],
        env=dict(os.environ, TEST_MANIFEST_LINK=str(link), TEST_MANIFEST_TARGET=str(external)),
        capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0
    try:
        if direction == "source":
            (external / "合成.docx").write_bytes(b"synthetic")
            row["source_path"] = str(link / "合成.docx")
        elif direction == "output":
            output = link / "output.jsonl"
        else:
            manifest = link / "manifest.jsonl"
        with pytest.raises(ValueError, match="reparse|redirect"):
            generate((root, manifest, output), [row])
        assert not output.exists()
    finally:
        # Remove this test's junction entry only; never recurse into its target.
        os.rmdir(link)


def test_script_is_the_same_cli_entrypoint():
    script = Path(__file__).parents[3] / "scripts/prepare_corpus_manifest.py"
    result = subprocess.run(  # noqa: S603 - current interpreter and fixed local CLI help
        [sys.executable, "-X", "utf8", str(script), "--help"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0
    assert "--source-root" in result.stdout and "--manifest" in result.stdout
