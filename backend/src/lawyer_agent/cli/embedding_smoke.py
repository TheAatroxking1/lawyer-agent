"""Exercise the actual local embedding provider without business data or services."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import platform
import re
import sys
from contextlib import redirect_stdout
from importlib.metadata import PackageNotFoundError, version
from time import perf_counter

from lawyer_agent.application.model_gateway import (
    ModelGateway,
    ModelGatewayError,
    ModelProviderInvalidResponse,
)
from lawyer_agent.domain.model_gateway import CallLimits, ModelCallRecord
from lawyer_agent.infrastructure.providers.embedding import (
    LocalSentenceTransformerEmbeddingProvider,
)
from lawyer_agent.infrastructure.providers.lifecycle import close_embedding_provider

_TEXTS = (
    "合成测试：合同约定服务期限。",
    "合成测试：当事人核对合同条款。",
    "合成测试：今天阳光明媚。",
)


class _Recorder:
    def __init__(self) -> None:
        self.records: list[ModelCallRecord] = []

    async def append(self, record: ModelCallRecord) -> None:
        self.records.append(record)


def _versions() -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for name in ("torch", "sentence-transformers", "transformers", "numpy", "huggingface-hub"):
        try:
            result[name] = version(name)
        except PackageNotFoundError:
            result[name] = None
    return result


async def _run(args: argparse.Namespace) -> dict[str, object]:
    recorder = _Recorder()
    provider = LocalSentenceTransformerEmbeddingProvider(
        model_name_or_path=args.model_ref,
        device=args.device,
        local_files_only=True,
        normalize_embeddings=True,
    )
    try:
        gateway = ModelGateway(
            provider,
            recorder,
            limits=CallLimits(
                timeout_seconds=300.0,
                max_attempts=1,
            ),
        )
        started = perf_counter()
        vectors = await gateway.embed(
            model_ref=args.model_ref, texts=_TEXTS, dimension=args.dimension
        )
        if len(vectors) != len(_TEXTS):
            raise ModelProviderInvalidResponse("smoke row count mismatch")
        norms = tuple(math.sqrt(sum(value * value for value in row.values)) for row in vectors)
        if any(
            not math.isfinite(norm) or not math.isclose(norm, 1.0, abs_tol=1e-6) for norm in norms
        ):
            raise ModelProviderInvalidResponse("smoke normalization mismatch")
        return {
            "schema_version": "embedding-smoke-v1",
            "passed": True,
            "model_ref": args.model_ref,
            "device": args.device,
            "local_files_only": True,
            "dimension": args.dimension,
            "row_count": len(vectors),
            "normalization": "l2",
            "norm_min": min(norms),
            "norm_max": max(norms),
            "elapsed_seconds": round(perf_counter() - started, 3),
            "gateway_call_count": len(recorder.records),
            "packages": _versions(),
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "platform": platform.system(),
        }
    finally:
        await close_embedding_provider(provider)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="离线验证本地 Embedding，不连接业务服务")
    parser.add_argument("--model-ref", required=True)
    parser.add_argument("--dimension", required=True, type=int)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)
    if (
        not args.model_ref.strip()
        or args.dimension <= 0
        or re.fullmatch(r"cpu|mps|cuda(?::[0-9]+)?", args.device) is None
    ):
        print(json.dumps({"passed": False, "code": "smoke_input_invalid"}))
        return 2
    try:
        # Keep stdout machine-readable even when a third-party loader prints progress.
        with redirect_stdout(sys.stderr):
            report = asyncio.run(_run(args))
    except Exception as exc:  # noqa: BLE001 - never expose vendor failures or fabricate success
        code = exc.code if isinstance(exc, ModelGatewayError) else "embedding_smoke_failed"
        print(json.dumps({"schema_version": "embedding-smoke-v1", "passed": False, "code": code}))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
