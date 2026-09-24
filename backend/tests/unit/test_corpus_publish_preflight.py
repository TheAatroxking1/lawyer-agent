import json
from dataclasses import replace
from hashlib import sha256
from zipfile import ZipFile

import pytest

from lawyer_agent.cli import corpus_publish as cli
from lawyer_agent.infrastructure.documents.parsers import ParsedArticle
from lawyer_agent.infrastructure.documents.word_conversion import ConversionRecord


def _docx(path, *, body=None, header=False):
    body = body or "<w:p><w:r><w:t>第一条 合成甲。</w:t></w:r></w:p>"
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml", f"<w:document {ns}><w:body>{body}</w:body></w:document>"
        )
        if header:
            archive.writestr(
                "word/header1.xml",
                f"<w:hdr {ns}><w:p><w:r><w:t>第九十九条 页眉。</w:t></w:r></w:p></w:hdr>",
            )


def _args(path, *extra):
    return [
        "--source",
        str(path),
        "--instrument-title",
        "合成示例法",
        "--issuing-authority",
        "示例机关",
        *extra,
    ]


@pytest.fixture
def native(tmp_path):
    path = tmp_path / "合成.docx"
    _docx(path, header=True)
    return path


@pytest.fixture
def converted(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    source = root / "合成.doc"
    source.write_bytes(b"synthetic legacy source")
    output = tmp_path / "derived"
    output.mkdir()
    target = output / "converted.docx"
    _docx(target, header=True)
    record = ConversionRecord(
        source_path=str(source),
        source_sha256=sha256(source.read_bytes()).hexdigest(),
        status="converted",
        converted_path=str(target),
        converted_sha256=sha256(target.read_bytes()).hexdigest(),
        converter_version="ms-word-16/worker-v1",
        converter_fingerprint="a" * 64,
    )
    manifest = output / "manifest.jsonl"
    manifest.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    return source, root, output, target, manifest, record


def test_preflight_is_json_and_never_enters_write_or_settings(native, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        pytest.fail("preflight entered service configuration or writing")

    monkeypatch.setattr(cli, "Settings", forbidden)
    monkeypatch.setattr(cli, "_write", forbidden, raising=False)
    assert cli.main(_args(native, "--preflight")) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["schema_version"] == "corpus-preflight-v1"
    assert (
        report["source_sha256"] == report["input_sha256"] == sha256(native.read_bytes()).hexdigest()
    )
    assert report["source_ref"] == native.as_uri()
    assert report["loader_version"] == "docx-export-reader-v2"
    assert report["parser_version"] == "corpus-docx-v3"
    assert report["article_count"] == 1 and report["auxiliary_paragraph_count"] == 1
    assert report["metadata_review_status"] == "not_verified"
    assert report["database_provenance_persisted"] is False
    assert report["published_on"] is report["effective_on"] is None
    assert report["status"] == "status_unknown"
    assert "合成甲" not in json.dumps(report, ensure_ascii=False)


def test_preflight_and_import_only_are_mutually_exclusive(native):
    with pytest.raises(SystemExit) as exc:
        cli.build_parser().parse_args(_args(native, "--preflight", "--import-only"))
    assert exc.value.code == 2


def test_legacy_preparation_keeps_original_identity(converted):
    source, root, output, target, manifest, record = converted
    args = cli.build_parser().parse_args(
        _args(
            source,
            "--source-root",
            str(root),
            "--conversion-manifest",
            str(manifest),
            "--converted-root",
            str(output),
            "--source-sha256",
            record.source_sha256,
        )
    )
    command, articles = cli._prepare_import(args)
    assert command.source_ref == source.as_uri()
    assert command.version_label == "source-sha256:" + record.source_sha256
    assert record.source_sha256 != sha256(target.read_bytes()).hexdigest()
    assert len(articles) == len(command.provisions) == 1
    assert all("页眉" not in draft.full_text for draft in command.provisions)


def test_default_reader_preserves_inline_tab_and_carriage_return(tmp_path):
    path = tmp_path / "inline.docx"
    _docx(
        path,
        body="<w:p><w:r><w:t>第一条</w:t><w:tab/><w:t>甲。</w:t><w:cr/><w:t>乙。</w:t></w:r></w:p>",
    )
    command, _ = cli._prepare_import(cli.build_parser().parse_args(_args(path)))
    assert command.provisions[0].full_text == "第一条\t甲。\n乙。"


def test_structure_fingerprint_includes_identity_path_and_paragraph_boundaries():
    first = ParsedArticle("第一条", ("第一章",), "第一条甲乙", 0, 5, ("第一条甲", "乙"))
    digest = cli._structure_sha256((first,))
    for changed in (
        replace(first, provision_no="第二条"),
        replace(first, structure_path=("第二章",)),
        replace(first, paragraphs=("第一条", "甲乙")),
    ):
        assert cli._structure_sha256((changed,)) != digest
    assert cli._structure_sha256((first,)) == digest


def test_static_recovery_is_preflight_only_until_quality_approval(converted, monkeypatch, capsys):
    source, root, output, _, manifest, record = converted
    static = record.model_copy(
        update={
            "converter_version": "binary-word-static-text-v1",
            "recovery_reason": "office_validation_failed",
        }
    )
    manifest.write_text(static.model_dump_json() + "\n", encoding="utf-8")
    args = _args(
        source,
        "--source-root",
        str(root),
        "--conversion-manifest",
        str(manifest),
        "--converted-root",
        str(output),
    )
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("must reject before settings"))
    assert cli.main([*args, "--preflight"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert "legacy_binary_static_text_recovery" in report["quality_flags"]
    assert report["import_blockers"] == ["static_recovery_requires_quality_review"]
    assert cli.main([*args, "--import-only"]) == 2
    assert "static_recovery_requires_quality_review" in capsys.readouterr().err


def test_preflight_wrong_expected_source_hash_fails_before_settings(native, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("must not configure services"))
    assert cli.main(_args(native, "--preflight", "--source-sha256", "0" * 64)) == 2
    assert "source" in capsys.readouterr().err


def test_preflight_rejects_duplicate_articles_before_services(tmp_path, monkeypatch, capsys):
    path = tmp_path / "duplicate.docx"
    _docx(
        path, body="".join(f"<w:p><w:r><w:t>第一条 {t}</w:t></w:r></w:p>" for t in ("甲。", "乙。"))
    )
    monkeypatch.setattr(cli, "Settings", lambda: pytest.fail("must not configure services"))
    assert cli.main(_args(path, "--preflight")) == 2
    assert "duplicate" in capsys.readouterr().err


def test_persisted_proof_normalizes_verified_hex_fingerprint(converted):
    source, root, output, _, manifest, record = converted
    upper = record.model_copy(update={"converter_fingerprint": "A" * 64})
    manifest.write_text(upper.model_dump_json() + "\n", encoding="utf-8")
    args = cli.build_parser().parse_args(
        _args(
            source,
            "--source-root",
            str(root),
            "--conversion-manifest",
            str(manifest),
            "--converted-root",
            str(output),
        )
    )
    proof = cli._source_proof(cli._prepare(args))
    assert proof.converter_fingerprint == "a" * 64
