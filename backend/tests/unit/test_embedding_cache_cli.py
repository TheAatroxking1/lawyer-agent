from pathlib import Path

import pytest

from lawyer_agent.cli import corpus_publish_set as cli


def test_cache_cli_is_disabled_by_default():
    args = cli.build_parser().parse_args(["--import-report", "report", "--check"])
    assert args.embedding_cache_directory is None


def test_cache_cli_accepts_explicit_absolute_cache_directory():
    path = (
        Path(cli.__file__).resolve().parents[4] / "artifacts" / "legal-corpus" / "vectors" / "test"
    )
    args = cli.build_parser().parse_args(
        [
            "--import-report",
            "report",
            "--check",
            "--embedding-cache-directory",
            str(path),
        ]
    )
    assert args.embedding_cache_directory == path


@pytest.mark.parametrize("directory", [Path("relative"), Path("F:/ai-read-only"), Path("C:/")])
def test_cache_cli_rejects_paths_outside_repository_vectors(directory):
    with pytest.raises(cli.ReleaseInputError):
        cli.require_cache_directory(directory)
