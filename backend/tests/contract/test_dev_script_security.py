from __future__ import annotations

import json
import os
import shutil
import subprocess
from base64 import b64encode
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).parents[3]


def _encoded_key(byte: int) -> str:
    return b64encode(bytes([byte]) * 32).decode("ascii")


def _development_values(environment_lines: list[str]) -> dict[str, str]:
    return {
        "environment": "\n".join(environment_lines),
        "app": "s" * 32,
        "data": _encoded_key(11),
        "blind": _encoded_key(12),
        "refresh": _encoded_key(13),
        "csrf": _encoded_key(14),
        "jwt": json.dumps({"development": _encoded_key(15)}),
    }


def _render_environment(values: dict[str, str]) -> str:
    return "\n".join(
        [
            values["environment"],
            f"LAWYER_SECRET_KEY={values['app']}",
            f"LAWYER_DATA_ENCRYPTION_KEY_B64={values['data']}",
            f"LAWYER_BLIND_INDEX_KEY_B64={values['blind']}",
            f"LAWYER_REFRESH_TOKEN_KEY_B64={values['refresh']}",
            f"LAWYER_CSRF_KEY_B64={values['csrf']}",
            f"LAWYER_JWT_ED25519_KEY_RING={values['jwt']}",
        ]
    )


def _configured_raw_values(values: dict[str, str]) -> set[str]:
    environment_values = {
        line.partition("=")[2]
        for line in values["environment"].splitlines()
        if "=" in line
    }
    return {
        values["app"],
        values["data"],
        values["blind"],
        values["refresh"],
        values["csrf"],
        values["jwt"],
        *environment_values,
    } - {""}


def _prepare_isolated_script(
    tmp_path: Path,
    environment_text: str,
) -> tuple[Path, Path, Path]:
    root = tmp_path / "isolated-repository"
    scripts = root / "scripts"
    deploy = root / "deploy"
    fake_bin = root / "fake-bin"
    scripts.mkdir(parents=True)
    deploy.mkdir()
    fake_bin.mkdir()
    shutil.copy2(REPOSITORY_ROOT / "scripts/dev.ps1", scripts / "dev.ps1")
    (deploy / ".env").write_text(environment_text + "\n", encoding="utf-8")
    (deploy / "compose.env.example").write_text(
        environment_text + "\n",
        encoding="utf-8",
    )
    (deploy / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    (fake_bin / "docker.cmd").write_text("@exit /b 0\n", encoding="ascii")
    fake_docker = fake_bin / "docker"
    fake_docker.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    fake_docker.chmod(0o755)
    return root, scripts / "dev.ps1", fake_bin


def _run_script(script: Path, fake_bin: Path) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("pwsh") or shutil.which("powershell.exe")
    if executable is None:
        pytest.skip("PowerShell is required to verify scripts/dev.ps1")
    child_environment = os.environ.copy()
    child_environment["PATH"] = f"{fake_bin}{os.pathsep}{child_environment['PATH']}"
    return subprocess.run(  # noqa: S603 - executable is resolved locally
        [executable, "-NoProfile", "-File", str(script)],
        cwd=script.parent.parent,
        env=child_environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )


def test_dev_script_rejects_production_before_overwriting_existing_secrets(
    tmp_path: Path,
) -> None:
    values = _development_values(["LAWYER_ENVIRONMENT=production"])
    root, script, fake_bin = _prepare_isolated_script(
        tmp_path,
        _render_environment(values),
    )
    secret_directory = root / "deploy/secrets"
    secret_directory.mkdir()
    for name in (
        "lawyer_app_secret_key",
        "lawyer_data_encryption_key_ring",
        "lawyer_blind_index_key_ring",
        "lawyer_refresh_token_key",
        "lawyer_csrf_key",
        "lawyer_jwt_ed25519_key_ring",
    ):
        (secret_directory / name).write_bytes(f"sentinel-{name}".encode("ascii"))
    before = {path.name: path.read_bytes() for path in secret_directory.iterdir()}

    completed = _run_script(script, fake_bin)

    after = {path.name: path.read_bytes() for path in secret_directory.iterdir()}
    combined_output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert after == before
    assert all(value not in combined_output for value in _configured_raw_values(values))


@pytest.mark.parametrize(
    "environment_lines",
    [
        [],
        ["LAWYER_ENVIRONMENT=development", "LAWYER_ENVIRONMENT=development"],
        ["lawyer_environment=development"],
        ["LAWYER_ENVIRONMENT=Development"],
    ],
    ids=("missing", "duplicate", "ambiguous-name-case", "ambiguous-value-case"),
)
def test_dev_script_fails_closed_on_ambiguous_environment_before_writing(
    tmp_path: Path,
    environment_lines: list[str],
) -> None:
    values = _development_values(environment_lines)
    root, script, fake_bin = _prepare_isolated_script(
        tmp_path,
        _render_environment(values),
    )

    completed = _run_script(script, fake_bin)

    combined_output = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert not (root / "deploy/secrets").exists()
    assert all(value not in combined_output for value in _configured_raw_values(values))


def test_dev_script_converts_development_single_keys_to_version_one_rings(
    tmp_path: Path,
) -> None:
    values = _development_values(["LAWYER_ENVIRONMENT=development"])
    root, script, fake_bin = _prepare_isolated_script(
        tmp_path,
        _render_environment(values),
    )

    completed = _run_script(script, fake_bin)

    combined_output = completed.stdout + completed.stderr
    assert completed.returncode == 0, combined_output
    secret_directory = root / "deploy/secrets"
    assert json.loads(
        (secret_directory / "lawyer_data_encryption_key_ring").read_text("utf-8")
    ) == {"1": values["data"]}
    assert json.loads(
        (secret_directory / "lawyer_blind_index_key_ring").read_text("utf-8")
    ) == {"1": values["blind"]}
    assert all(value not in combined_output for value in _configured_raw_values(values))
