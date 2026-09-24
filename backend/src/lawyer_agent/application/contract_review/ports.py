"""Explicit ports: no SDK, ORM, implicit providers or default authorization."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from lawyer_agent.application.contract_review.contracts import (
    BlocksResult,
    EvidenceInput,
    JobInput,
    JobResult,
    LegalEvidence,
    ReadBlocksInput,
    RegionInput,
    ReviewCandidate,
    ReviewRequest,
    ReviewResult,
    ReviewScope,
    SearchInput,
    SearchResult,
    StructureInput,
    StructureResult,
    VersionInput,
)
from lawyer_agent.domain.model_gateway import ChatMessage


class ScopeProvider(Protocol):
    async def current(self) -> ReviewScope: ...


class Authorizer(Protocol):
    async def require(self, scope: ReviewScope, action: str, resource_id: str) -> None: ...


class DocumentService(Protocol):
    async def parse(self, scope: ReviewScope, request: VersionInput) -> JobResult: ...
    async def job_status(self, scope: ReviewScope, request: JobInput) -> JobResult: ...
    async def structure(self, scope: ReviewScope, request: StructureInput) -> StructureResult: ...
    async def read_blocks(self, scope: ReviewScope, request: ReadBlocksInput) -> BlocksResult: ...
    async def inspect_region(self, scope: ReviewScope, request: RegionInput) -> JobResult: ...


class LegalService(Protocol):
    async def search(self, scope: ReviewScope, request: SearchInput) -> SearchResult: ...
    async def read(self, scope: ReviewScope, request: EvidenceInput) -> LegalEvidence: ...


class ReviewModel(Protocol):
    async def chat(self, messages: Sequence[ChatMessage]) -> str: ...


class ReviewGate(Protocol):
    async def validate(
        self,
        scope: ReviewScope,
        request: ReviewRequest,
        candidate: ReviewCandidate,
    ) -> ReviewResult:
        """Re-read authoritative coverage, anchors and legal support; fail closed."""
        ...


class ReviewStore(Protocol):
    async def save(self, scope: ReviewScope, result: ReviewResult) -> None:
        """Atomically/idempotently store by tenant + run; conflicting replay must fail."""
        ...
