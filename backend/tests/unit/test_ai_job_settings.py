from __future__ import annotations

import os
from pathlib import Path

import pytest

from lawyer_agent.runtime.settings import (
    AIJobMaintenanceSettings,
    AIJobPublisherSettings,
    AIJobTopologySettings,
    AIJobWorkerSettings,
    read_json_secret_file,
    read_secret_file,
)


def test_process_settings_never_expose_foreign_credentials() -> None:
    publisher_fields = set(AIJobPublisherSettings.model_fields)
    worker_fields = set(AIJobWorkerSettings.model_fields)
    topology_fields = set(AIJobTopologySettings.model_fields)
    maintenance_fields = set(AIJobMaintenanceSettings.model_fields)

    assert "message_private_seed_ring_file" in publisher_fields
    assert "message_private_seed_ring_file" not in worker_fields
    assert "message_private_seed_ring_file" not in topology_fields
    assert "message_private_seed_ring_file" not in maintenance_fields

    assert "message_public_verify_ring_file" in worker_fields
    assert "message_public_verify_ring_file" not in publisher_fields

    assert "rabbit_management_credential_file" in topology_fields
    assert "rabbit_management_credential_file" not in publisher_fields
    assert "rabbit_management_credential_file" not in worker_fields
    assert "rabbit_management_credential_file" not in maintenance_fields

    assert "observability_reference_key_file" in worker_fields
    assert "observability_reference_key_file" not in publisher_fields


def test_read_secret_file_rejects_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        read_secret_file(Path("relative.txt"), field_name="test")


def test_read_secret_file_rejects_directory(tmp_path: Path) -> None:
    directory = tmp_path / "adir"
    directory.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        read_secret_file(directory, field_name="test")


def test_read_secret_file_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")
    with pytest.raises(ValueError, match="regular file"):
        read_secret_file(link, field_name="test")


def test_read_secret_file_rejects_oversize(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_bytes(b"x" * 65_537)
    with pytest.raises(ValueError, match="invalid size"):
        read_secret_file(path, field_name="test")


def test_read_secret_file_rejects_non_utf8(tmp_path: Path) -> None:
    path = tmp_path / "binary.txt"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ValueError, match="UTF-8"):
        read_secret_file(path, field_name="test")


def test_read_secret_file_accepts_absolute_regular_utf8(tmp_path: Path) -> None:
    path = tmp_path / "ok.txt"
    path.write_text("key-material\n", encoding="utf-8")
    assert read_secret_file(path, field_name="test") == "key-material"


def test_read_json_secret_file_parses_mapping(tmp_path: Path) -> None:
    path = tmp_path / "ring.json"
    path.write_text('{"k2026": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="}', encoding="utf-8")
    parsed = read_json_secret_file(path, field_name="test")
    assert parsed == {"k2026": "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQE="}


def test_environment_comes_from_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAWYER_ENVIRONMENT", "staging")
    os.environ["LAWYER_ENVIRONMENT"] = "staging"
    try:
        settings = AIJobMaintenanceSettings(
            environment="staging",
            database_url="mysql+asyncmy://user:pass@host/db",  # type: ignore[arg-type]
        )
        assert settings.environment == "staging"
    finally:
        monkeypatch.delenv("LAWYER_ENVIRONMENT", raising=False)
