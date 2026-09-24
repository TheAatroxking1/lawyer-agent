"""The live checker reports metadata, never contract text or credentials."""

from lawyer_agent.application.contract_review.research import (
    ContractBrief,
    LegalCandidate,
    LegalDocument,
    ResearchResult,
    serialize_research_documents,
)
from lawyer_agent.cli.contract_review_check import research_report


def test_research_report_omits_contract_and_legal_body() -> None:
    documents = (LegalDocument(document_id="law-1", title="测试法规", version="v1",
                               content_hash="a" * 64, text="PRIVATE LEGAL BODY",
                               source_ref="local:test"),)
    result = ResearchResult(
        status="ready",
        brief=ContractBrief(
            is_contract=True, contract_type="租赁合同",
            fact_summary="PRIVATE CONTRACT FACTS", queries=("租赁",),
        ),
        candidates=(LegalCandidate(document_id="law-1", title="测试法规",
                                   snippets=("PRIVATE SNIPPET",)),),
        evidence_ids=("law-1",), evidence_titles=("测试法规",),
        documents=documents,
        context=serialize_research_documents(documents),
    )
    report = research_report(result)
    assert report["status"] == "ready"
    assert report["candidate_count"] == 1
    assert report["full_documents_read"] == 1
    assert "PRIVATE" not in str(report)
    assert report["documents"][0]["content_hash"] == "a" * 64


def test_empty_research_report_is_not_a_completed_review() -> None:
    result = ResearchResult(
        status="no_evidence",
        brief=ContractBrief(is_contract=True, contract_type="租赁合同",
                            fact_summary="测试摘要", queries=("租赁",)),
        context="我没有找到相关的法律文档，暂时无法提供相关服务",
    )
    report = research_report(result)
    assert report["review_completed"] is False
    assert report["full_documents_read"] == 0
