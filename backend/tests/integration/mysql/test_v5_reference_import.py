"""v5 real CLI persistence/replay for repeated item references; isolated MySQL."""

import asyncio
import json
from hashlib import sha256
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from test_legal_dataset_publication_persistence import publication_mysql_url  # noqa: F401

from lawyer_agent.cli import corpus_import_batch, corpus_publish_set
from lawyer_agent.config import Settings
from lawyer_agent.infrastructure.persistence.models.legal_corpus import (
    LegalChunkModel,
    LegalProvisionModel,
    LegalVersionModel,
)

pytestmark = [pytest.mark.integration, pytest.mark.mysql]


def test_v5_cli_import_replay_and_quality_preserve_reference(
    publication_mysql_url,  # noqa: F811
    tmp_path,
    monkeypatch,
    capsys,
):
    import lawyer_agent.config as config_module
    import lawyer_agent.infrastructure.providers.embedding as embedding_module

    settings = Settings(
        environment="test",
        database_url=publication_mysql_url.render_as_string(hide_password=False),
        embedding_model_ref="synthetic",
        embedding_dimension=3,
    )
    monkeypatch.setattr(config_module, "Settings", lambda: settings)
    monkeypatch.setattr(corpus_publish_set, "Settings", lambda: settings)
    monkeypatch.setattr(
        embedding_module,
        "LocalSentenceTransformerEmbeddingProvider",
        lambda **kw: pytest.fail("quality must not load a model"),
    )
    reference = "第一条第（八）、第（九）、第（十）、第（十一）项规定的事项，依照有关规定处理。"
    texts = ["第一条 合成适用范围。", "第二条 合成管理要求。", reference, "第三条 合成监督要求。"]
    sources = tmp_path / "sources"
    sources.mkdir()
    source = sources / "合成引用条例.docx"
    with ZipFile(source, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            + "".join(f"<w:p><w:r><w:t>{t}</w:t></w:r></w:p>" for t in texts)
            + "</w:body></w:document>",
        )
    row = dict(
        schema_version="legal-corpus-import-v1",
        review_status="reviewed",
        metadata_review_ref="synthetic-review",
        source_path=str(source),
        source_sha256=sha256(source.read_bytes()).hexdigest(),
        title="合成引用条例",
        issuing_authority="合成机关",
        jurisdiction="CN",
        category="law",
        published_on="2020-01-01",
        effective_on="2020-02-01",
        status="current",
        version_label="synthetic-v5",
    )
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    old_report, report, replay = (
        tmp_path / name for name in ("old.jsonl", "v5.jsonl", "replay.jsonl")
    )

    def run(profile, target):
        return corpus_import_batch.main(
            [
                "--manifest",
                str(manifest),
                "--source-root",
                str(sources),
                "--parser-version",
                profile,
                "--report",
                str(target),
            ]
        )

    assert run("corpus-docx-v4", old_report) != 0
    assert run("corpus-docx-v5", report) == 0

    async def facts():
        engine = create_async_engine(publication_mysql_url)
        try:
            async with engine.connect() as connection:
                versions = (
                    (await connection.execute(select(LegalVersionModel.__table__))).mappings().all()
                )
                provisions = (
                    (
                        await connection.execute(
                            select(LegalProvisionModel.__table__).order_by(
                                LegalProvisionModel.char_start
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                chunks = (
                    (
                        await connection.execute(
                            select(LegalChunkModel.__table__).order_by(LegalChunkModel.id)
                        )
                    )
                    .mappings()
                    .all()
                )
                return (
                    [dict(r) for r in versions],
                    [dict(r) for r in provisions],
                    [dict(r) for r in chunks],
                )
        finally:
            await engine.dispose()

    before = asyncio.run(facts())
    assert len(before[0]) == 1 and before[0][0]["parser_version"] == "corpus-docx-v5"
    assert [p["provision_no"] for p in before[1]] == ["第一条", "第二条", "第三条"]
    assert reference in before[1][1]["full_text"]
    assert run("corpus-docx-v5", replay) == 0
    assert asyncio.run(facts()) == before
    capsys.readouterr()
    assert corpus_publish_set.main(["--import-report", str(report), "--check"]) == 0
    quality = json.loads(capsys.readouterr().out.splitlines()[0])
    assert quality["passed"] is True
    assert quality["configuration"]["parser_version"] == "corpus-docx-v5/hierarchical-v2"
