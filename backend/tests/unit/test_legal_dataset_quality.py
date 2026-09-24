from dataclasses import asdict, replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from lawyer_agent.application.legal_dataset_quality import ReleaseQualityService, validate_review
from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.legal_corpus import (
    ChunkQuality,
    ChunkType,
    LegalCategory,
    LegalChunk,
    LegalInstrument,
    LegalVersion,
    LegalVersionStatus,
    Provision,
    ProvisionLevel,
    content_sha256,
)
from lawyer_agent.domain.legal_dataset_quality import (
    ArticleNumberingReview,
    DatasetQualityError,
    ReleaseConfiguration,
    ReleaseSelection,
)
from lawyer_agent.domain.legal_source_proof import LegalSourceProof


def fixture(text="第一条 合成测试条文。"):
    version_id, instrument_id, provision_id = new_uuid7(), new_uuid7(), new_uuid7()
    instrument = LegalInstrument(
        instrument_id, "合成法", "合成机关", "national", category=LegalCategory.LAW
    )
    version = LegalVersion(
        version_id,
        instrument_id,
        "synthetic",
        LegalVersionStatus.CURRENT,
        date(2020, 1, 1),
        date(2020, 2, 1),
        None,
        content_hash=content_sha256(text),
        parser_version="parser",
    )
    provision = Provision(
        provision_id,
        version_id,
        "第一条",
        ProvisionLevel.ARTICLE,
        ("第一条",),
        None,
        text,
        content_sha256(text),
        0,
        len(text),
    )
    chunk = LegalChunk(
        new_uuid7(),
        version_id,
        provision_id,
        ChunkType.PROVISION,
        ChunkQuality.OK,
        text,
        content_sha256(text),
        parser_version="parser/hierarchical-v1",
    )
    proof = LegalSourceProof(
        "file:///C:/synthetic.docx",
        "file:///C:/synthetic.docx",
        b"s" * 32,
        b"s" * 32,
        b"t" * 32,
        "loader",
        "parser",
    )
    selection = ReleaseSelection(
        version_id,
        proof.source_ref,
        proof.source_sha256.hex(),
        proof.input_sha256.hex(),
        proof.structure_sha256.hex(),
        "review:metadata",
        1,
        1,
        instrument_id,
    )
    configuration = ReleaseConfiguration("laws", "fake", 3, "parser/hierarchical-v1", "a" * 64)
    return SimpleNamespace(
        version=version,
        instrument=instrument,
        provisions=(provision,),
        chunks=(chunk,),
        proof=proof,
        selection=selection,
        config=configuration,
    )


async def check(data):
    service = ReleaseQualityService(
        SimpleNamespace(
            version_with_instrument=AsyncMock(return_value=(data.version, data.instrument)),
            provisions_for_version=AsyncMock(return_value=data.provisions),
        ),
        SimpleNamespace(chunks_for_version=AsyncMock(return_value=data.chunks)),
        SimpleNamespace(find_for_version=AsyncMock(return_value=data.proof)),
    )
    return await service.check((data.selection,), data.config)


async def _imported_quality_fixture(*, body_mode=False):
    from lawyer_agent.application.legal_corpus_import import LegalCorpusImportService
    from tests.unit.test_v4_exact_provision_text import _command, _draft, _Repo

    drafts = (
        replace(_draft("正文", "  合成完整批复正文。\u200b"), level=ProvisionLevel.PARAGRAPH),
    ) if body_mode else (
        _draft("第一条", "第一条 甲。\u200b", path=("第一章", "第一条")),
        replace(_draft("第二条", "\u200b第二条 乙。"), title="合成条文标题"),
    )
    command = replace(_command(drafts), category=LegalCategory.LAW,
                      published_on=date(2020, 1, 1), effective_on=date(2020, 2, 1))
    repo = _Repo()
    service = LegalCorpusImportService(repo)
    imported = await service.import_version(command)
    assert (await service.import_version(command)).replayed
    data = fixture()
    data.version, data.instrument = repo.versions[0], repo.instrument
    data.provisions = tuple(repo.provisions)
    data.chunks = tuple(replace(
        data.chunks[0], id=new_uuid7(), version_id=imported.version_id,
        provision_id=p.id, content=p.full_text, content_hash=p.content_hash,
        parser_version="corpus-docx-v4/hierarchical-v1",
    ) for p in data.provisions)
    data.proof = replace(data.proof, parser_version="corpus-docx-v4",
                         quality_flags=("non_article_document",) if body_mode else ())
    data.selection = replace(data.selection, version_id=imported.version_id,
                             expected_instrument_id=imported.instrument_id,
                             expected_article_count=0 if body_mode else len(drafts),
                             expected_provision_count=len(drafts), expected_chunk_count=len(drafts),
                             content_mode="non_article_document" if body_mode else "articles")
    data.config = replace(data.config, parser_version="corpus-docx-v4/hierarchical-v1")
    return data


@pytest.mark.parametrize("body_mode", [False, True])
async def test_real_v4_import_and_replay_pass_release_quality(body_mode):
    data = await _imported_quality_fixture(body_mode=body_mode)
    report = await check(data)
    assert report.passed, report.versions[0].blockers


@pytest.mark.parametrize("mutation", ["text", "path", "title", "number", "level", "order", "hash"])
async def test_v4_release_rejects_changed_imported_content_identity(mutation):
    data = await _imported_quality_fixture()
    assert (await check(data)).passed
    first, second = data.provisions
    if mutation == "text":
        text = first.full_text.replace("甲", "丙")
        first = replace(first, full_text=text, content_hash=content_sha256(text))
        data.chunks = (replace(data.chunks[0], content=text, content_hash=content_sha256(text)),
                       data.chunks[1])
    elif mutation == "path":
        first = replace(first, structure_path=("第二章", "第一条"))
    elif mutation == "title":
        first = replace(first, title="")
    elif mutation == "number":
        first = replace(first, provision_no="第三条")
    elif mutation == "level":
        first = replace(first, level=ProvisionLevel.PARAGRAPH)
    elif mutation == "order":
        second = replace(second, char_start=0, char_end=len(second.full_text))
        first = replace(first, char_start=second.char_end,
                        char_end=second.char_end + len(first.full_text))
    else:
        data.version = replace(data.version, content_hash=content_sha256(
            "".join(p.full_text for p in data.provisions)))
    data.provisions = (first, second)
    report = await check(data)
    assert not report.passed
    assert "version_content_hash_mismatch" in report.versions[0].blockers


async def test_v4_release_uses_authoritative_offsets_not_repository_return_order():
    data = await _imported_quality_fixture()
    before = await check(data)
    assert before.passed
    data.provisions = tuple(reversed(data.provisions))
    after = await check(data)
    assert after.passed and after.digest == before.digest


async def test_v4_release_rejects_resegmentation_even_when_concatenated_text_is_unchanged():
    data = await _imported_quality_fixture()
    first, second = data.provisions
    original = first.full_text + second.full_text
    first_text, second_text = first.full_text[:-1], first.full_text[-1:] + second.full_text
    data.provisions = (
        replace(first, full_text=first_text, content_hash=content_sha256(first_text),
                char_end=len(first_text)),
        replace(second, full_text=second_text, content_hash=content_sha256(second_text),
                char_start=len(first_text)),
    )
    data.chunks = tuple(replace(c, content=p.full_text, content_hash=p.content_hash)
                        for c, p in zip(data.chunks, data.provisions, strict=True))
    assert "".join(p.full_text for p in data.provisions) == original
    report = await check(data)
    assert report.versions[0].blockers == ("version_content_hash_mismatch",)


async def test_v4_non_article_release_rejects_rehashed_body_tampering():
    data = await _imported_quality_fixture(body_mode=True)
    assert (await check(data)).passed
    text = data.provisions[0].full_text.replace("批复", "决定")
    data.provisions = (replace(data.provisions[0], full_text=text,
                               content_hash=content_sha256(text)),)
    data.chunks = (replace(data.chunks[0], content=text, content_hash=content_sha256(text)),)
    report = await check(data)
    assert report.versions[0].blockers == ("version_content_hash_mismatch",)


async def test_quality_report_binds_numbering_review_and_legacy_omits_absent_field():
    data = fixture("第三条 合成测试条文。")
    data.provisions = (
        replace(data.provisions[0], provision_no="第三条", structure_path=("第三条",)),
    )
    legacy = await check(data)
    assert "article_sequence_invalid" in legacy.versions[0].blockers
    assert "numbering_review" not in legacy.to_dict()["versions"][0]["source"]
    review = ArticleNumberingReview(
        3, 3, "review:numbering", "https://official.example/evidence", "a" * 64
    )
    data.selection = replace(data.selection, numbering_review=review)
    reviewed = await check(data)
    assert reviewed.passed
    assert "article_numbering_explicitly_reviewed" in reviewed.versions[0].manual_review
    assert reviewed.to_dict()["versions"][0]["source"]["numbering_review"] == asdict(review)
    assert reviewed.digest != legacy.digest
    data.selection = replace(
        data.selection,
        numbering_review=replace(review, review_ref="review:numbering:changed"),
    )
    assert (await check(data)).digest != reviewed.digest


@pytest.mark.parametrize("profile, expected_digest", [
    ("parser", "5e8cab839ad3d38363cb17a15f1514dd1490c24b7116a9f5126c435f9e465f02"),
    ("corpus-docx-v3", "2d959967a6767c2726a8abc35b568df33f0e4eaf02fbd4235c7a49ec521bcf34"),
    ("corpus-docx-v4-extra", "8a4bc2eb8f991e558c2b3a3835b9f22aec8cdbc77cb6266d62a87b336413f2ee"),
])
async def test_legacy_digest_is_deterministic_with_fixed_uuid_fixture(profile, expected_digest):
    data = fixture()
    instrument_id = UUID("01890f15-0000-7000-8000-000000000001")
    version_id = UUID("01890f15-0000-7000-8000-000000000002")
    provision_id = UUID("01890f15-0000-7000-8000-000000000003")
    chunk_id = UUID("01890f15-0000-7000-8000-000000000004")
    data.instrument = replace(data.instrument, id=instrument_id)
    data.version = replace(data.version, id=version_id, instrument_id=instrument_id,
                           parser_version=profile)
    data.provisions = (replace(data.provisions[0], id=provision_id, version_id=version_id),)
    data.chunks = (
        replace(
            data.chunks[0], id=chunk_id, version_id=version_id, provision_id=provision_id,
            parser_version=profile + "/hierarchical-v1",
        ),
    )
    data.selection = replace(
        data.selection, version_id=version_id, expected_instrument_id=instrument_id
    )
    data.proof = replace(data.proof, parser_version=profile)
    data.config = replace(data.config, parser_version=profile + "/hierarchical-v1")
    assert (await check(data)).digest == expected_digest


async def test_non_article_requires_matching_proof_mode_and_body_identity():
    data = fixture("合成完整批复正文。")
    data.selection = replace(data.selection, content_mode="non_article_document",
                             expected_article_count=0, expected_provision_count=1)
    data.provisions = (replace(data.provisions[0], level=ProvisionLevel.PARAGRAPH,
                              provision_no="正文", structure_path=("正文",)),)
    assert not (await check(data)).passed
    data.proof = replace(data.proof, quality_flags=("non_article_document",))
    report = await check(data)
    assert report.passed
    assert report.versions[0].source.content_mode == "non_article_document"
    data.provisions = (replace(data.provisions[0], provision_no="第一条"),)
    assert not (await check(data)).passed
    data.provisions = (replace(data.provisions[0], provision_no="正文"),)
    data.selection = replace(data.selection, content_mode="articles", expected_article_count=1)
    assert not (await check(data)).passed


async def test_repeated_leaf_text_uses_exact_verified_positions():
    data = fixture("重复重复")
    root = replace(data.chunks[0], parser_version="parser/hierarchical-v2",
                   parent_relative_char_start=0, parent_relative_char_end=4)
    child = replace(root, id=new_uuid7(), parent_chunk_id=root.id, chunk_type=ChunkType.PARAGRAPH,
                    content="重复", content_hash=content_sha256("重复"), parent_relative_char_end=2)
    second = replace(child, id=new_uuid7(), parent_relative_char_start=2,
                     parent_relative_char_end=4)
    data.chunks = (root, child, second)
    data.selection = replace(data.selection, expected_chunk_count=3)
    data.config = replace(data.config, parser_version="parser/hierarchical-v2")
    report = await check(data)
    assert report.passed
    data.chunks = (root, child, replace(second, parent_relative_char_start=0,
                                        parent_relative_char_end=2))
    invalid = await check(data)
    assert not invalid.passed and invalid.digest != report.digest


async def test_v2_missing_or_mismatched_positions_are_rejected():
    data = fixture()
    data.config = replace(data.config, parser_version="parser/hierarchical-v2")
    data.chunks = (replace(data.chunks[0], parser_version=data.config.parser_version),)
    assert not (await check(data)).passed


async def test_current_date_exception_is_visible_and_digest_bound():
    data = fixture()
    data.version = replace(data.version, published_on=None, effective_on=None)
    assert "legal_dates_missing" in (await check(data)).versions[0].blockers
    data.selection = replace(data.selection, date_review_ref="review:dates")
    report = await check(data)
    assert report.passed
    assert "date_unknown_user_reviewed" in report.versions[0].manual_review
    assert report.versions[0].metadata.published_on is None
    assert report.versions[0].source.date_review_ref == "review:dates"
    data.selection = replace(data.selection, date_review_ref="review:other")
    assert (await check(data)).digest != report.digest
    data.instrument = replace(data.instrument, jurisdiction="unsupported")
    assert "jurisdiction_unsupported" in (await check(data)).versions[0].blockers


@pytest.mark.parametrize("status", [
    LegalVersionStatus.DRAFT, LegalVersionStatus.REPEALED, LegalVersionStatus.STATUS_UNKNOWN,
])
async def test_date_exception_does_not_bypass_other_statuses(status):
    data = fixture()
    data.selection = replace(data.selection, date_review_ref="review:dates")
    data.version = replace(data.version, status=status, published_on=None, effective_on=None)
    report = await check(data)
    assert "legal_dates_missing" in report.versions[0].blockers
    assert "date_unknown_user_reviewed" not in report.versions[0].manual_review


@pytest.mark.parametrize("ref", ["", " ", "x" * 513, True])
def test_date_review_selection_rejects_invalid_refs(ref):
    with pytest.raises(DatasetQualityError):
        replace(fixture().selection, date_review_ref=ref)


async def test_selection_counts_must_match_database():
    data = fixture()
    data.selection = replace(data.selection, expected_article_count=2)
    assert not (await check(data)).passed
    data.selection = replace(data.selection, expected_article_count=1, expected_chunk_count=2)
    assert not (await check(data)).passed


async def test_report_instrument_id_must_match_authoritative_version():
    data = fixture()
    data.selection = replace(data.selection, expected_instrument_id=new_uuid7())
    report = await check(data)
    assert not report.passed
    assert "version_scope_invalid" in str(report.to_dict())


async def test_digest_binds_edges_between_semantically_identical_parents():
    data = fixture("甲乙")
    root = data.chunks[0]
    left = replace(root, id=new_uuid7(), parent_chunk_id=root.id, chunk_type=ChunkType.PARAGRAPH)
    right = replace(left, id=new_uuid7())
    a = replace(
        left,
        id=new_uuid7(),
        parent_chunk_id=left.id,
        content="甲",
        content_hash=content_sha256("甲"),
    )
    b = replace(
        a, id=new_uuid7(), parent_chunk_id=right.id, content="乙", content_hash=content_sha256("乙")
    )
    data.chunks = (root, left, right, a, b)
    data.selection = replace(data.selection, expected_chunk_count=5)
    first = await check(data)
    data.chunks = (root, left, right, a, replace(b, parent_chunk_id=left.id))
    assert (await check(data)).digest != first.digest


async def test_valid_report_requires_source_review_and_contains_no_body():
    data = fixture()
    report = await check(data)
    assert report.passed
    assert len(report.digest) == 64
    assert "requires_source_review" in str(report.to_dict())
    assert data.provisions[0].full_text not in str(report.to_dict())
    assert "parse_failures" not in str(report.to_dict())
    summary = report.to_dict()["versions"][0]
    assert summary["metadata"]["title"] == data.instrument.title
    assert summary["metadata"]["effective_on"] == "2020-02-01"
    assert summary["source"]["source_ref"] == data.selection.source_ref
    assert summary["source"]["source_sha256"] == data.selection.source_sha256
    assert summary["source"]["metadata_review_ref"] == "review:metadata"
    assert report.to_dict()["configuration"] == {
        "alias": "laws",
        "model_ref": "fake",
        "dimension": 3,
        "parser_version": "parser/hierarchical-v1",
        "selection_sha256": "a" * 64,
        "normalization": "l2",
    }
    validate_review(report, report.digest, "review:lawyer")


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_proof",
        "source_hash",
        "source_ref",
        "parser",
        "unknown_status",
        "draft",
        "unknown_category",
        "date",
        "local_region",
        "repealed_date",
        "version_hash",
        "missing_chunks",
        "missing_provisions",
    ],
)
async def test_missing_or_conflicting_facts_block(mutation):
    data = fixture()
    if mutation == "missing_proof":
        data.proof = None
    elif mutation == "source_hash":
        data.selection = replace(data.selection, source_sha256="b" * 64)
    elif mutation == "source_ref":
        data.selection = replace(data.selection, source_ref="file:///C:/other.docx")
    elif mutation == "parser":
        data.proof = replace(data.proof, parser_version="different")
    elif mutation == "unknown_status":
        data.version = replace(data.version, status=LegalVersionStatus.STATUS_UNKNOWN)
    elif mutation == "draft":
        data.version = replace(data.version, status=LegalVersionStatus.DRAFT)
    elif mutation == "unknown_category":
        data.instrument = replace(data.instrument, category=LegalCategory.UNKNOWN)
    elif mutation == "date":
        data.version = replace(data.version, effective_on=None)
    elif mutation == "local_region":
        data.instrument = replace(data.instrument, category=LegalCategory.LOCAL_REGULATION)
    elif mutation == "repealed_date":
        data.version = replace(data.version, status=LegalVersionStatus.REPEALED)
    elif mutation == "version_hash":
        data.version = replace(data.version, content_hash=b"z" * 32)
    elif mutation == "missing_chunks":
        data.chunks = ()
    elif mutation == "missing_provisions":
        data.provisions = ()
    report = await check(data)
    assert not report.passed
    with pytest.raises(DatasetQualityError, match="quality_checks_failed"):
        validate_review(report, report.digest, "review")


async def test_historical_and_retroactive_effect_are_valid():
    data = fixture()
    data.version = replace(
        data.version, status=LegalVersionStatus.HISTORICAL, effective_on=date(2019, 1, 1)
    )
    assert (await check(data)).passed


@pytest.mark.parametrize("jurisdiction", ["unknown", "foreign", "US", " "])
async def test_unsupported_jurisdiction_is_blocked(jurisdiction):
    data = fixture()
    if jurisdiction.strip():
        data.instrument = replace(data.instrument, jurisdiction=jurisdiction)
    else:
        object.__setattr__(data.instrument, "jurisdiction", jurisdiction)
    report = await check(data)
    assert not report.passed
    assert "jurisdiction_unsupported" in str(report.to_dict())


@pytest.mark.parametrize("jurisdiction", ["national", "CN"])
async def test_known_mainland_public_corpus_markers_are_supported(jurisdiction):
    data = fixture()
    data.instrument = replace(data.instrument, jurisdiction=jurisdiction)
    assert (await check(data)).passed


async def test_degraded_and_source_flags_are_explicit_manual_review():
    data = fixture()
    data.chunks = (replace(data.chunks[0], quality=ChunkQuality.DEGRADED),)
    data.proof = replace(data.proof, quality_flags=("table_layout_changed",))
    report = await check(data)
    assert report.passed
    assert "degraded_chunks" in str(report.to_dict())
    assert "table_layout_changed" in str(report.to_dict())


async def test_static_recovery_cannot_be_overridden_by_review():
    data = fixture()
    data.proof = replace(
        data.proof,
        converter_version="binary-word-static-text-v1",
        converter_fingerprint="a" * 64,
        recovery_reason="office_validation_failed",
    )
    report = await check(data)
    assert not report.passed


async def test_static_recovery_requires_verified_proof_and_matching_selection():
    from lawyer_agent.domain.legal_source_proof import reviewed_static_proof

    data = fixture()
    data.proof = reviewed_static_proof(replace(
        data.proof, converter_version="binary-word-static-text-v1",
        converter_fingerprint="a" * 64, recovery_reason="office_validation_failed",
    ), "e" * 64)
    assert not (await check(data)).passed
    data.selection = replace(data.selection, static_review_sha256="e" * 64)
    assert (await check(data)).passed
    data.selection = replace(data.selection, static_review_sha256="f" * 64)
    assert not (await check(data)).passed


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_parent",
        "extra_root",
        "foreign_provision",
        "failed",
        "child_outside_parent",
        "leaf_gap",
        "ambiguous",
    ],
)
async def test_invalid_chunk_graph_or_coverage_blocks(mutation):
    data = fixture("甲乙甲乙丙")
    root = data.chunks[0]
    child = replace(
        root,
        id=new_uuid7(),
        chunk_type=ChunkType.PARAGRAPH,
        parent_chunk_id=root.id,
        content="甲乙甲乙丙",
        content_hash=content_sha256("甲乙甲乙丙"),
    )
    data.chunks = (root, child)
    if mutation == "missing_parent":
        data.chunks = (child,)
    elif mutation == "extra_root":
        data.chunks += (replace(root, id=new_uuid7()),)
    elif mutation == "foreign_provision":
        data.chunks = (root, replace(child, provision_id=new_uuid7()))
    elif mutation == "failed":
        data.chunks = (replace(root, quality=ChunkQuality.FAILED),)
    elif mutation == "child_outside_parent":
        data.chunks = (root, replace(child, content="外文", content_hash=content_sha256("外文")))
    elif mutation == "leaf_gap":
        data.chunks = (root, replace(child, content="丙", content_hash=content_sha256("丙")))
    elif mutation == "ambiguous":
        data.chunks = (root, replace(child, content="甲乙", content_hash=content_sha256("甲乙")))
    assert not (await check(data)).passed


async def test_overlapping_leaf_union_covers_article():
    data = fixture("甲乙丙丁戊")
    root = data.chunks[0]
    data.chunks = (root,) + tuple(
        replace(
            root,
            id=new_uuid7(),
            parent_chunk_id=root.id,
            chunk_type=ChunkType.PARAGRAPH,
            content=text,
            content_hash=content_sha256(text),
        )
        for text in ("甲乙丙", "丙丁戊")
    )
    data.selection = replace(data.selection, expected_chunk_count=3)
    assert (await check(data)).passed


async def test_digest_stable_under_chunk_uuid_replay_and_order_changes():
    data = fixture("甲乙丙丁戊")
    root = data.chunks[0]
    children = tuple(
        replace(
            root,
            id=new_uuid7(),
            parent_chunk_id=root.id,
            chunk_type=ChunkType.PARAGRAPH,
            content=text,
            content_hash=content_sha256(text),
        )
        for text in ("甲乙丙", "丙丁戊")
    )
    data.chunks = (root,) + children
    data.selection = replace(data.selection, expected_chunk_count=3)
    first = await check(data)
    new_root_id = new_uuid7()
    data.chunks = tuple(
        replace(c, id=new_uuid7(), parent_chunk_id=new_root_id) for c in children[::-1]
    ) + (replace(root, id=new_root_id),)
    assert (await check(data)).digest == first.digest


@pytest.mark.parametrize(
    "mutation",
    [
        "metadata",
        "proof_flag",
        "model",
        "normalization_manifest",
        "review_ref",
        "chunk_quality",
        "multiplicity",
    ],
)
async def test_digest_binds_facts_and_configuration(mutation):
    data = fixture()
    before = await check(data)
    if mutation == "metadata":
        data.instrument = replace(data.instrument, title="另一合成法")
    elif mutation == "proof_flag":
        data.proof = replace(data.proof, quality_flags=("review_me",))
    elif mutation == "model":
        data.config = replace(data.config, model_ref="other")
    elif mutation == "normalization_manifest":
        data.config = replace(data.config, selection_sha256="b" * 64)
    elif mutation == "review_ref":
        data.selection = replace(data.selection, metadata_review_ref="other-review")
    elif mutation == "chunk_quality":
        data.chunks = (replace(data.chunks[0], quality=ChunkQuality.DEGRADED),)
    elif mutation == "multiplicity":
        data.chunks += (replace(data.chunks[0], id=new_uuid7()),)
    assert (await check(data)).digest != before.digest


async def test_digest_binds_release_report_member_provenance():
    from lawyer_agent.domain.legal_dataset_quality import ReleaseReportMember
    data = fixture()
    data.config = replace(data.config, report_members=(
        ReleaseReportMember("C:/one.jsonl", "a" * 64, 0, 1),
    ))
    before = await check(data)
    assert before.to_dict()["configuration"]["report_members"][0]["report_sha256"] == "a" * 64
    data.config = replace(data.config, report_members=(
        ReleaseReportMember("C:/one.jsonl", "b" * 64, 0, 1),
    ))
    assert (await check(data)).digest != before.digest


async def test_review_must_match_digest_and_reference():
    report = await check(fixture())
    for digest, reference in (("a" * 64, "review"), (report.digest, " ")):
        with pytest.raises(DatasetQualityError):
            validate_review(report, digest, reference)


async def test_digest_binds_actual_text_after_hash_only_fingerprinting():
    data = fixture()
    original = await check(data)
    text = "第一条 修改后的合成条文。"
    data.version = replace(data.version, content_hash=content_sha256(text))
    data.provisions = (
        replace(
            data.provisions[0],
            full_text=text,
            content_hash=content_sha256(text),
            char_end=len(text),
        ),
    )
    data.chunks = (replace(data.chunks[0], content=text, content_hash=content_sha256(text)),)
    changed = await check(data)
    assert changed.passed
    assert changed.digest != original.digest
    assert text not in str(changed.to_dict())


@pytest.mark.parametrize("count", [True, 0, -1, "1"])
def test_selection_expected_counts_are_positive_integers(count):
    with pytest.raises(DatasetQualityError):
        replace(fixture().selection, expected_chunk_count=count)


@pytest.mark.parametrize(
    "changes",
    [
        {"dimension": True},
        {"alias": "a" * 65},
        {"normalization": "none"},
        {"parser_version": "x" * 65},
        {"selection_sha256": "oops"},
    ],
)
def test_configuration_rejects_invalid_values(changes):
    with pytest.raises(DatasetQualityError):
        replace(fixture().config, **changes)
