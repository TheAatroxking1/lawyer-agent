"""Bounded cleanup without leaking vendor failures or hiding in-flight work."""
import logging
from typing import Protocol

_logger = logging.getLogger(__name__)


class EmbeddingLifetime(Protocol):
    async def aclose(self, *, timeout_seconds: float = 5.0) -> bool: ...


async def close_embedding_provider(provider: EmbeddingLifetime) -> None:
    try:
        drained = await provider.aclose(timeout_seconds=5.0)
    except Exception:  # noqa: BLE001 - preserve original operation error and sanitize cleanup
        _logger.warning("embedding_provider_close_failed")
        return
    if not drained:
        _logger.warning("embedding_provider_close_not_drained")
