from __future__ import annotations

from datetime import UTC, datetime

import pytest

from lawyer_agent.domain.common import new_uuid7
from lawyer_agent.domain.matter_documents import (
    DocumentKind,
    DocumentUploadStatus,
    DocumentVersion,
    Matter,
    MatterKind,
    MatterParty,
    MatterStatus,
    MatterStatusTransitionInvalid,
    ReviewStatus,
    require_matter_status_transition,
)


def _matter() -> Matter:
    return Matter(
        id=new_uuid7(),
        tenant_id=new_uuid7(),
        title="某房屋租赁合同审查",
        kind=MatterKind.CONTRACT_REVIEW,
        status=MatterStatus.OPEN,
        created_by_user_id=new_uuid7(),
        created_by_membership_id=new_uuid7(),
    )


def _version(matter: Matter) -> DocumentVersion:
    return DocumentVersion(
        id=new_uuid7(),
        tenant_id=matter.tenant_id,
        document_id=new_uuid7(),
        version_no=1,
        kind=DocumentKind.ORIGINAL,
        object_key=f"tenant_{matter.tenant_id}/doc/{new_uuid7()}",
        sha256=bytes(32),
        upload_status=DocumentUploadStatus.ACCEPTED,
        file_name="lease.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size_bytes=1024,
        parser_version="docx-zip-v1",
        review_status=ReviewStatus.DRAFT,
        uploaded_at=datetime.now(UTC),
    )


def test_matter_round_trip() -> None:
    matter = _matter()
    assert matter.kind is MatterKind.CONTRACT_REVIEW
    assert matter.status is MatterStatus.OPEN
    assert matter.version == 1


def test_matter_rejects_blank_title() -> None:
    base = _matter()
    with pytest.raises(ValueError, match="title"):
        Matter(
            id=base.id,
            tenant_id=base.tenant_id,
            title="   ",
            kind=base.kind,
            status=base.status,
            created_by_user_id=base.created_by_user_id,
            created_by_membership_id=base.created_by_membership_id,
        )


def test_party_must_match_tenant_and_matter() -> None:
    matter = _matter()
    party = MatterParty(
        id=new_uuid7(),
        tenant_id=matter.tenant_id,
        matter_id=matter.id,
        display_name="出租人甲公司",
        kind="counterparty",
    )
    assert party.matter_id == matter.id


def test_version_round_trip() -> None:
    matter = _matter()
    version = _version(matter)
    assert version.upload_status is DocumentUploadStatus.ACCEPTED
    assert version.kind is DocumentKind.ORIGINAL
    assert version.object_key.startswith(f"tenant_{matter.tenant_id}")


def test_version_rejects_bad_sha_and_overlong_key() -> None:
    matter = _matter()
    base = _version(matter)
    with pytest.raises(ValueError, match="sha256"):
        DocumentVersion(
            id=base.id,
            tenant_id=base.tenant_id,
            document_id=base.document_id,
            version_no=base.version_no,
            kind=base.kind,
            object_key=base.object_key,
            sha256=b"short",
            upload_status=base.upload_status,
        )
    with pytest.raises(ValueError, match="object key"):
        DocumentVersion(
            id=base.id,
            tenant_id=base.tenant_id,
            document_id=base.document_id,
            version_no=base.version_no,
            kind=base.kind,
            object_key="x" * 1000,
            sha256=bytes(32),
            upload_status=base.upload_status,
        )


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (MatterStatus.OPEN, MatterStatus.ACTIVE),
        (MatterStatus.OPEN, MatterStatus.CLOSED),
        (MatterStatus.ACTIVE, MatterStatus.CLOSED),
        (MatterStatus.CLOSED, MatterStatus.ARCHIVED),
    ],
)
def test_status_transition_accepts_forward_set(
    source: MatterStatus, target: MatterStatus
) -> None:
    require_matter_status_transition(source, target)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (MatterStatus.OPEN, MatterStatus.ARCHIVED),
        (MatterStatus.ACTIVE, MatterStatus.ARCHIVED),
        (MatterStatus.ACTIVE, MatterStatus.OPEN),
        (MatterStatus.CLOSED, MatterStatus.OPEN),
        (MatterStatus.CLOSED, MatterStatus.ACTIVE),
        (MatterStatus.ARCHIVED, MatterStatus.OPEN),
        (MatterStatus.ARCHIVED, MatterStatus.ACTIVE),
        (MatterStatus.ARCHIVED, MatterStatus.CLOSED),
        (MatterStatus.ARCHIVED, MatterStatus.ARCHIVED),
    ],
)
def test_status_transition_rejects_backward_and_jumps(
    source: MatterStatus, target: MatterStatus
) -> None:
    with pytest.raises(MatterStatusTransitionInvalid):
        require_matter_status_transition(source, target)


def test_status_transition_rejects_untyped_endpoints() -> None:
    with pytest.raises(ValueError, match="strongly typed"):
        require_matter_status_transition(MatterStatus.OPEN, "active")  # type: ignore[arg-type]
