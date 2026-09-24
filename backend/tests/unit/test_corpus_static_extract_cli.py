import hashlib
import json
import zipfile

import pytest

from lawyer_agent.cli import corpus_static_extract as cli
from lawyer_agent.infrastructure.documents.word_conversion import ConversionRecord


@pytest.fixture
def selected(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "合成.doc"
    source.write_bytes(b"synthetic legacy")
    output = tmp_path / "converted"
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    identity = hashlib.sha256((source.name + "\0" + digest).encode()).hexdigest()
    folder = output / identity
    folder.mkdir(parents=True)
    prior = ConversionRecord(
        source_path=str(source),
        source_sha256=digest,
        status="failed",
        error_code="word_conversion_failed",
    )
    (folder / "record.json").write_text(prior.model_dump_json(), encoding="utf-8")
    args = [
        "--source-root",
        str(source_root),
        "--output-root",
        str(output),
        "--source",
        str(source),
        "--reason",
        "office_validation_failed",
    ]
    return source, output, folder, prior, args


class FakeStaticConverter:
    fingerprint = "synthetic-static-policy-v1"

    def __call__(self, source, target):
        with zipfile.ZipFile(target, "x") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
                "第一条 合成文本。</w:t></w:r></w:p></w:body></w:document>",
            )
        return "binary-word-static-text-v1"


def test_explicit_static_extract_preserves_failed_origin_and_replays(selected, monkeypatch):
    source, output, folder, prior, args = selected
    monkeypatch.setattr(cli, "_converter", FakeStaticConverter)
    assert cli.main(args) == 0
    record = ConversionRecord.model_validate_json(
        (folder / "record.json").read_text(encoding="utf-8")
    )
    assert record.status == "converted"
    assert record.recovery_reason == "office_validation_failed"
    assert record.converter_version == "binary-word-static-text-v1"
    assert source.read_bytes() == b"synthetic legacy"
    assert (
        ConversionRecord.model_validate_json(
            (folder / "recovery_origin.json").read_text(encoding="utf-8")
        )
        == prior
    )
    assert (
        json.loads((output / "manifest.jsonl").read_text(encoding="utf-8"))["recovery_reason"]
        == "office_validation_failed"
    )
    assert cli.main(args) == 0
    assert (
        ConversionRecord.model_validate_json(
            (folder / "recovery_origin.json").read_text(encoding="utf-8")
        )
        == prior
    )


def test_static_cli_requires_explicit_source_and_reason(selected):
    *_, args = selected
    for omitted in ("--source", "--reason"):
        index = args.index(omitted)
        with pytest.raises(SystemExit) as exc:
            cli.main(args[:index] + args[index + 2 :])
        assert exc.value.code == 2


@pytest.mark.parametrize(
    "change", ["missing", "hash", "already_word_converted", "unrelated_failure"]
)
def test_static_cli_rejects_unestablished_origin_before_converter(selected, monkeypatch, change):
    _, _, folder, prior, args = selected
    path = folder / "record.json"
    if change == "missing":
        path.unlink()
    else:
        updates = {
            "hash": {"source_sha256": "0" * 64},
            "already_word_converted": {"status": "converted", "converter_version": "ms-word-16"},
            "unrelated_failure": {"error_code": "word_busy"},
        }[change]
        path.write_text(prior.model_copy(update=updates).model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(cli, "_converter", lambda: pytest.fail("converter must not be created"))
    assert cli.main(args) == 2
    assert not (folder / "recovery_origin.json").exists()


def test_static_cli_fails_unsupported_text_without_success(selected, monkeypatch):
    _, output, folder, prior, args = selected

    class Unsupported(FakeStaticConverter):
        def __call__(self, source, target):
            raise ValueError("unsupported binary text")

    monkeypatch.setattr(cli, "_converter", Unsupported)
    assert cli.main(args) == 1
    result = ConversionRecord.model_validate_json(
        (folder / "record.json").read_text(encoding="utf-8")
    )
    assert result.status == "failed"
    assert result.recovery_reason == "office_validation_failed"
    assert not list(output.rglob("*.docx"))
    assert (
        ConversionRecord.model_validate_json(
            (folder / "recovery_origin.json").read_text(encoding="utf-8")
        )
        == prior
    )


def test_static_cli_rejects_source_outside_root(selected, monkeypatch, tmp_path):
    _, _, _, _, args = selected
    other = tmp_path / "outside.doc"
    other.write_bytes(b"outside")
    args[args.index("--source") + 1] = str(other)
    monkeypatch.setattr(cli, "_converter", lambda: pytest.fail("source boundary must fail first"))
    assert cli.main(args) == 2
