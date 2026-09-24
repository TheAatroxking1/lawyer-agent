import asyncio
from types import SimpleNamespace

from lawyer_agent.application.legal_corpus_import import LegalImportResult
from lawyer_agent.cli import corpus_publish
from lawyer_agent.cli.corpus_publish import build_parser
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalCategory,
    LegalChunk,
    Provision,
    ProvisionLevel,
    content_sha256,
)


def test_category_defaults_to_unknown_and_accepts_only_approved_values() -> None:
    parser = build_parser()
    base = [
        "--docx",
        "sample.docx",
        "--instrument-title",
        "示例法",
        "--issuing-authority",
        "示例机关",
    ]
    assert parser.parse_args(base).category == LegalCategory.UNKNOWN.value
    assert parser.parse_args([*base, "--category", "law"]).category == "law"

    try:
        parser.parse_args([*base, "--category", "foreign_law"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("invalid category must be rejected by argparse")


def test_import_prepared_replay_reads_existing_chunks_without_replacing(
    monkeypatch,
) -> None:
    version_id, provision_id = new_uuid7(), new_uuid7()
    provision = Provision(
        id=provision_id, version_id=version_id, provision_no="第一条",
        level=ProvisionLevel.ARTICLE, structure_path=("第一条",), title=None,
        full_text=" \t第一条 甲。\r\n", content_hash=content_sha256(" \t第一条 甲。\r\n"),
        char_start=0, char_end=10,
    )
    expected = LegalChunk(
        id=new_uuid7(), version_id=version_id, provision_id=provision_id,
        chunk_type=ChunkType.PROVISION, quality=ChunkQuality.OK,
        content=provision.full_text, content_hash=content_sha256(provision.full_text),
        parser_version="corpus-docx-v4/hierarchical-v2", parent_relative_char_start=0,
        parent_relative_char_end=10,
    )
    existing = LegalChunk(
        id=new_uuid7(), version_id=version_id, provision_id=provision_id,
        chunk_type=expected.chunk_type, quality=expected.quality,
        content=expected.content, content_hash=expected.content_hash,
        parser_version=expected.parser_version, parent_relative_char_start=0,
        parent_relative_char_end=10,
    )
    replaced: list[tuple[LegalChunk, ...]] = []

    class _Context:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        def begin(self):
            return _Context()

    class _ImportService:
        def __init__(self, repository):
            pass

        async def import_version(self, command):
            return LegalImportResult(new_uuid7(), version_id, replayed=True)

    class _ProofService:
        def __init__(self, repository):
            pass

        async def ensure(self, imported, proof, **kwargs):
            return None

    class _CorpusRepository:
        def __init__(self, session):
            pass

        async def provisions_for_version(self, target):
            return (provision,)

    class _ChunkRepository:
        def __init__(self, session):
            pass

        async def chunks_for_version(self, target):
            return (existing,)

        async def replace_chunks_for_version(self, target, chunks):
            replaced.append(chunks)

    import lawyer_agent.application.legal_chunk_structure as structure
    import lawyer_agent.application.legal_corpus_import as corpus_import
    import lawyer_agent.application.legal_source_proof as source_proof
    import lawyer_agent.infrastructure.persistence.repositories.legal_corpus as repositories

    monkeypatch.setattr(corpus_publish, "_require_importable", lambda prepared: None)
    monkeypatch.setattr(corpus_publish, "_source_proof", lambda prepared: object())
    monkeypatch.setattr(corpus_import, "LegalCorpusImportService", _ImportService)
    monkeypatch.setattr(source_proof, "LegalSourceProofService", _ProofService)
    monkeypatch.setattr(
        repositories, "SqlAlchemyLegalCorpusImportRepository", lambda session: object()
    )
    monkeypatch.setattr(repositories, "SqlAlchemyLegalCorpusRepository", _CorpusRepository)
    monkeypatch.setattr(repositories, "SqlAlchemyLegalCorpusChunkRepository", _ChunkRepository)
    monkeypatch.setattr(structure, "derive_hierarchical_chunks", lambda **kwargs: (expected,))
    prepared = SimpleNamespace(
        command=SimpleNamespace(provisions=(SimpleNamespace(
            provision_no=provision.provision_no, level=provision.level,
            structure_path=provision.structure_path, title=provision.title,
            full_text=provision.full_text,
        ),), parser_version="corpus-docx-v4"),
        articles=(), content_mode="articles", static_review_sha256=None,
    )

    result = asyncio.run(corpus_publish._import_prepared(lambda: _Context(), prepared))

    assert result.chunk_count == 1
    assert replaced == []
