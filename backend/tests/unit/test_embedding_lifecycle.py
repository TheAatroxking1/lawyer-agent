import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from lawyer_agent.infrastructure.providers.lifecycle import close_embedding_provider


@pytest.mark.parametrize(
    "outcome,log",
    [
        (True, ""),
        (False, "embedding_provider_close_not_drained"),
        (RuntimeError("vendor secret"), "embedding_provider_close_failed"),
    ],
)
async def test_close_logs_only_safe_state_and_preserves_original_error(caplog, outcome, log):
    close = AsyncMock(
        side_effect=outcome if isinstance(outcome, Exception) else None, return_value=outcome
    )
    with pytest.raises(ValueError, match="original"):
        try:
            raise ValueError("original")
        finally:
            await close_embedding_provider(SimpleNamespace(aclose=close))
    close.assert_awaited_once_with(timeout_seconds=5.0)
    assert "vendor secret" not in caplog.text
    if log:
        assert log in caplog.text


async def test_close_cancellation_propagates():
    with pytest.raises(asyncio.CancelledError):
        await close_embedding_provider(
            SimpleNamespace(
                aclose=AsyncMock(side_effect=asyncio.CancelledError),
            )
        )
