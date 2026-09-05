from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from lawyer_agent.config import Settings

_FAKE_KEY = "sk-deepseek-test-key-value-1234567890"


def _write_config(tmp_path: Path, payload: str, name: str = "deepseek.json") -> Path:
    path = tmp_path / name
    path.write_text(payload, encoding="utf-8")
    return path


def test_deepseek_api_key_read_from_json_file(tmp_path: Path) -> None:
    path = _write_config(tmp_path, json.dumps({"api_key": _FAKE_KEY}))
    settings = Settings(environment="test", deepseek_api_key_file=path)
    assert settings.deepseek_api_key == _FAKE_KEY


def test_deepseek_api_key_blank_value_counts_as_unconfigured(tmp_path: Path) -> None:
    path = _write_config(tmp_path, json.dumps({"api_key": "   "}))
    settings = Settings(environment="test", deepseek_api_key_file=path)
    assert settings.deepseek_api_key is None


def test_deepseek_api_key_can_come_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAWYER_DEEPSEEK_API_KEY", _FAKE_KEY)
    settings = Settings(environment="test")
    assert settings.deepseek_api_key == _FAKE_KEY


def test_deepseek_api_key_blank_environment_counts_as_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LAWYER_DEEPSEEK_API_KEY", "   ")
    settings = Settings(environment="test")
    assert settings.deepseek_api_key is None


def test_deepseek_api_key_file_and_value_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    path = _write_config(tmp_path, json.dumps({"api_key": _FAKE_KEY}))
    with pytest.raises(ValidationError, match="cannot both be configured"):
        Settings(
            environment="test",
            deepseek_api_key_file=path,
            deepseek_api_key=_FAKE_KEY,
        )


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        '{"api_key": 42}',
        '{"api_key": "ok", "extra": "nope"}',
        '{"other": "nope"}',
        '{"api_key": "a", "api_key": "b"}',
    ],
)
def test_deepseek_api_key_file_rejects_malformed_json(
    tmp_path: Path, payload: str
) -> None:
    path = _write_config(tmp_path, payload)
    with pytest.raises(ValidationError):
        Settings(environment="test", deepseek_api_key_file=path)


def test_deepseek_api_key_file_must_be_absolute(tmp_path: Path) -> None:
    path = _write_config(tmp_path, json.dumps({"api_key": _FAKE_KEY}))
    relative = Path(path.name)
    with pytest.raises(ValidationError, match="absolute path"):
        Settings(environment="test", deepseek_api_key_file=relative)


def test_deepseek_api_key_file_must_be_regular_file(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="regular file"):
        Settings(environment="test", deepseek_api_key_file=tmp_path)


def test_deepseek_api_key_file_missing_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ValidationError, match="cannot be read"):
        Settings(environment="test", deepseek_api_key_file=missing)


def test_deepseek_api_key_never_appears_in_repr_or_dump(tmp_path: Path) -> None:
    path = _write_config(tmp_path, json.dumps({"api_key": _FAKE_KEY}))
    settings = Settings(environment="test", deepseek_api_key_file=path)
    assert _FAKE_KEY not in repr(settings)
    assert _FAKE_KEY not in str(settings.model_dump())
    assert _FAKE_KEY not in str(settings.model_dump_json())


def test_deepseek_config_template_is_committed_with_blank_key() -> None:
    template = (
        Path(__file__).resolve().parents[3] / "deploy" / "deepseek.config.example.json"
    )
    assert template.is_file()
    payload = json.loads(template.read_text(encoding="utf-8"))
    assert set(payload) == {"api_key"}
    assert payload["api_key"] == ""
