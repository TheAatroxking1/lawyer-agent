import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from lawyer_agent.infrastructure.documents.conversion_paths import ConversionPaths
from lawyer_agent.infrastructure.documents.word_conversion import WordConversionError
from lawyer_agent.infrastructure.documents.word_driver import PowerShellWordConverter


def junction(link: Path, target: Path) -> None:
    executable = shutil.which("powershell.exe")
    assert executable
    env = dict(os.environ, TEST_CONVERSION_LINK=str(link), TEST_CONVERSION_SOURCE=str(target))
    result = subprocess.run(  # noqa: S603 - resolved executable, fixed script, synthetic env paths
        [
            executable,
            "-NoProfile",
            "-Command",
            "New-Item -ItemType Junction -Path $env:TEST_CONVERSION_LINK "
            "-Target $env:TEST_CONVERSION_SOURCE | Out-Null",
        ],
        env=env,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0


def driver(tmp_path: Path) -> PowerShellWordConverter:
    value = object.__new__(PowerShellWordConverter)
    source_root = tmp_path / "source"
    source_root.mkdir()
    value._paths = ConversionPaths(source_root, tmp_path / "output")
    value._root = value._paths.output_root / "work"
    value._root.mkdir(parents=True)
    value._executable = "powershell.exe"
    value._script = Path("worker.ps1")
    value._timeout = 10
    return value


def test_unknown_timeout_retains_copy_and_blocks_next_run(tmp_path, monkeypatch):
    value = driver(tmp_path)
    source = value._paths.source_root / "source.doc"
    source.write_bytes(b"original")

    def run(command, timeout):
        raise subprocess.TimeoutExpired(command, timeout)

    monkeypatch.setattr(value, "_run", run)
    with pytest.raises(WordConversionError):
        value(source, value._paths.output_root / "target.docx")
    assert len(list(value._root.glob("*/input.doc"))) == 1
    with pytest.raises(WordConversionError):
        value.recover_pending()


def test_interruption_recovers_before_copy_cleanup(tmp_path, monkeypatch):
    value = driver(tmp_path)
    source = value._paths.source_root / "source.doc"
    source.write_bytes(b"original")
    recovered = []

    def run(command, timeout):
        control = Path(command[command.index("-ControlFile") + 1])
        nonce = command[command.index("-Nonce") + 1]
        control.write_text(json.dumps({"nonce": nonce, "setting_changed": True}))
        raise KeyboardInterrupt()

    def recover(control, nonce):
        assert (control.parent / "input.doc").exists()
        recovered.append(True)

    monkeypatch.setattr(value, "_run", run)
    monkeypatch.setattr(value, "_recover", recover)
    with pytest.raises(KeyboardInterrupt):
        value(source, value._paths.output_root / "target.docx")
    assert recovered == [True]
    assert not list(value._root.glob("*/input.doc"))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_work_junction_cannot_write_source_tree(tmp_path):
    source_root = tmp_path / "source"
    source_root.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    work = output / ".work"
    junction(work, source_root)
    script = Path(__file__).parents[3] / "scripts/word_conversion_worker.ps1"
    with pytest.raises(ValueError, match="redirect|reparse|unsafe"):
        PowerShellWordConverter(
            work_root=work, source_root=source_root, output_root=output, worker_script=script
        )
    assert list(source_root.iterdir()) == []


def test_fingerprint_changes_for_script_or_timeout(tmp_path):
    value = driver(tmp_path)
    value._script = tmp_path / "worker.ps1"
    value._script.write_text("version-A", encoding="utf-8")
    first = value.fingerprint
    assert value.fingerprint == first
    value._script.write_text("version-B", encoding="utf-8")
    second = value.fingerprint
    assert second != first
    value._timeout = 30
    assert value.fingerprint != second


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_recovery_junction_rejected_without_running_worker(tmp_path, monkeypatch):
    value = driver(tmp_path)
    link = value._root / "nonce"
    junction(link, value._paths.source_root)
    monkeypatch.setattr(value, "_run", lambda *args: pytest.fail("worker must not run"))
    with pytest.raises(ValueError, match="reparse|unsafe"):
        value.recover_pending()
    assert list(value._paths.source_root.iterdir()) == []
