import pytest

from lawyer_agent.cli.corpus_convert import conversion_lock, main


def test_conversion_lock_rejects_concurrent_batch(tmp_path):
    with conversion_lock(tmp_path):
        with pytest.raises(ValueError, match="conversion_batch_busy"):
            with conversion_lock(tmp_path):
                pytest.fail("concurrent conversion accepted")
    with conversion_lock(tmp_path):
        pass


def test_cli_rejects_unbounded_negative_limit(tmp_path):
    assert (
        main(
            [
                "--source-root",
                str(tmp_path),
                "--output-root",
                str(tmp_path / "out"),
                "--limit",
                "-1",
            ]
        )
        == 2
    )


def test_cli_explicit_source_outside_root_rejected(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    other = tmp_path / "other.doc"
    other.write_bytes(b"fake")
    assert (
        main(
            [
                "--source-root",
                str(root),
                "--output-root",
                str(tmp_path / "out"),
                "--source",
                str(other),
            ]
        )
        == 2
    )
