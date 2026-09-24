import pytest
from pydantic import ValidationError

from lawyer_agent.config import Settings


def test_embedding_runtime_defaults_are_cpu_and_offline(monkeypatch):
    monkeypatch.delenv("LAWYER_EMBEDDING_DEVICE", raising=False)
    monkeypatch.delenv("LAWYER_EMBEDDING_LOCAL_FILES_ONLY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.embedding_device == "cpu"
    assert settings.embedding_local_files_only is True


@pytest.mark.parametrize("device", ["cpu", "cuda", "cuda:0", "cuda:12", "mps"])
def test_embedding_runtime_accepts_explicit_supported_device(device):
    assert Settings(_env_file=None, embedding_device=device).embedding_device == device


@pytest.mark.parametrize("device", ["", "auto", "CUDA", "cpu:0", "cuda:-1", "cuda:", "cpu\n"])
def test_embedding_runtime_rejects_unrecognized_device(device):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, embedding_device=device)


def test_embedding_runtime_settings_read_operator_environment(monkeypatch):
    monkeypatch.setenv("LAWYER_EMBEDDING_DEVICE", "cuda:2")
    monkeypatch.setenv("LAWYER_EMBEDDING_LOCAL_FILES_ONLY", "false")
    settings = Settings(_env_file=None)
    assert settings.embedding_device == "cuda:2"
    assert settings.embedding_local_files_only is False
