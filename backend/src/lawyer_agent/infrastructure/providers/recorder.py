"""Model call recorder that logs structured, sanitised call records.

The Model Gateway requires a ``ModelCallRecorderPort``; this adapter writes
each call to the application logger with only observability-safe fields
(model_ref, operation, status, latency_ms, usage, vector_count, error_code).
Prompt text, message bodies, API keys and provider payloads are never logged.
"""

from __future__ import annotations

import logging

from lawyer_agent.application.model_gateway import ModelCallRecorderPort
from lawyer_agent.domain.model_gateway import ModelCallRecord

_logger = logging.getLogger("lawyer_agent.model_gateway")


class LoggingModelCallRecorder(ModelCallRecorderPort):
    """Appends sanitised call records to the model gateway logger."""

    async def append(self, record: ModelCallRecord) -> None:
        if not isinstance(record, ModelCallRecord):
            raise ValueError("model call record must be strongly typed")
        _logger.info(
            "model_call operation=%s model_ref=%s status=%s latency_ms=%d "
            "error_code=%s prompt_tokens=%s completion_tokens=%s total_tokens=%s "
            "vector_count=%s",
            record.operation.value,
            record.model_ref,
            record.status,
            record.latency_ms,
            record.error_code,
            record.usage.prompt_tokens if record.usage else None,
            record.usage.completion_tokens if record.usage else None,
            record.usage.total_tokens if record.usage else None,
            record.vector_count,
        )
