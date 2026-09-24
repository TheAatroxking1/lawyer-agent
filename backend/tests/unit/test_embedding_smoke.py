from importlib import import_module

import pytest

from lawyer_agent.domain.model_gateway import EmbeddingVector


@pytest.mark.parametrize("mode", ["success", "dimension", "rows", "norm", "exception"])
def test_smoke_uses_offline_gateway_and_reports_only_safe_metrics(monkeypatch, capsys, mode):
    cli = import_module("lawyer_agent.cli.embedding_smoke")
    calls = []
    closed = []

    class Provider:
        def __init__(self, **kwargs):
            assert kwargs == {
                "model_name_or_path": "local-model",
                "device": "cpu",
                "local_files_only": True,
                "normalize_embeddings": True,
            }

        async def embed(self, *, texts, dimension, timeout_seconds):
            calls.append(tuple(texts))
            if mode == "exception":
                raise RuntimeError("provider-private-detail")
            size = 3 if mode == "dimension" else dimension
            row = (2.0 if mode == "norm" else 1.0, *(0.0 for _ in range(size - 1)))
            return tuple(
                EmbeddingVector(row, size)
                for _ in range(
                    1 if mode == "rows" else len(texts),
                )
            )

        async def aclose(self, *, timeout_seconds=5.0):
            closed.append(timeout_seconds)
            return True

    monkeypatch.setattr(cli, "LocalSentenceTransformerEmbeddingProvider", Provider)
    status = cli.main(["--model-ref", "local-model", "--dimension", "4"])
    import json

    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["passed"] is (mode == "success")
    assert status == (0 if mode == "success" else 1)
    assert len(calls) == 1 and len(calls[0]) == 3
    assert closed == [5.0]
    assert "provider-private-detail" not in output.out + output.err
    assert "values" not in report and "vectors" not in report and "texts" not in report
    if mode == "success":
        assert report["dimension"] == 4 and report["row_count"] == 3
        assert report["local_files_only"] is True
        assert report["norm_min"] == report["norm_max"] == 1.0
        assert report["gateway_call_count"] == 1


@pytest.mark.parametrize("dimension", ["0", "-1"])
def test_invalid_smoke_dimension_does_not_load_model(monkeypatch, capsys, dimension):
    cli = import_module("lawyer_agent.cli.embedding_smoke")
    monkeypatch.setattr(
        cli,
        "LocalSentenceTransformerEmbeddingProvider",
        lambda **_: pytest.fail("invalid dimensions must not load model"),
    )
    assert cli.main(["--model-ref", "local-model", "--dimension", dimension]) == 2
    assert "smoke_input_invalid" in capsys.readouterr().out


async def test_smoke_cancellation_closes_provider_and_propagates(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    cli = import_module("lawyer_agent.cli.embedding_smoke")
    close = AsyncMock(return_value=True)
    monkeypatch.setattr(
        cli,
        "LocalSentenceTransformerEmbeddingProvider",
        lambda **_: SimpleNamespace(
            embed=AsyncMock(side_effect=asyncio.CancelledError), aclose=close
        ),
    )
    with pytest.raises(asyncio.CancelledError):
        await cli._run(SimpleNamespace(model_ref="fake", device="cpu", dimension=4))
    close.assert_awaited_once_with(timeout_seconds=5.0)
