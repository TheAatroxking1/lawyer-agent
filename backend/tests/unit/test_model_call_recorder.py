from __future__ import annotations

import logging

import pytest

from lawyer_agent.application.model_gateway import ModelGateway
from lawyer_agent.domain.model_gateway import (
    CallLimits,
    ModelCallRecord,
    ModelOperation,
    TokenUsage,
)
from lawyer_agent.infrastructure.providers.recorder import LoggingModelCallRecorder


def _record(**overrides: object) -> ModelCallRecord:
    values: dict[str, object] = {
        "operation": ModelOperation.CHAT,
        "model_ref": "deepseek-chat",
        "status": "success",
        "latency_ms": 12,
        "error_code": None,
        "usage": TokenUsage(prompt_tokens=2, completion_tokens=3, total_tokens=5),
        "vector_count": None,
    }
    values.update(overrides)
    return ModelCallRecord(**values)


async def test_recorder_logs_sanitised_fields(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="lawyer_agent.model_gateway"):
        await LoggingModelCallRecorder().append(_record())
    text = caplog.text
    assert "model_call" in text
    assert "deepseek-chat" in text
    assert "prompt_tokens=2" in text
    assert "total_tokens=5" in text


async def test_recorder_rejects_untyped_record() -> None:
    recorder = LoggingModelCallRecorder()
    try:
        await recorder.append("raw")  # type: ignore[arg-type]
    except ValueError as exc:
        assert "strongly typed" in str(exc)
    else:
        raise AssertionError("expected typed record rejection")


def test_gateway_accepts_logging_recorder_as_port() -> None:
    recorder = LoggingModelCallRecorder()

    class _Provider:
        async def embed(self, **_: object) -> object:
            return ()

        async def rerank(self, **_: object) -> object:
            return ()

        async def chat(self, **_: object) -> object:
            return "", TokenUsage()

    gateway = ModelGateway(provider=_Provider(), recorder=recorder)
    assert gateway is not None
    assert CallLimits(timeout_seconds=1.0).max_attempts >= 1
