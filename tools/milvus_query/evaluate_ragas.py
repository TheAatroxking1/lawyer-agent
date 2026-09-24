"""固定12题的第二版窗口检索评测：RAGAS RRF Top7 / rerank Top7对照。"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import importlib.metadata
import json
import logging
import math
import os
import re
import statistics
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from legal_query.window_corpus import digest, load, save, sha

CIVIL = "中华人民共和国民法典"
LABOR = "中华人民共和国劳动合同法"
CASES = [
    (
        "maintenance",
        "武汉租的房子出现漏水，房东拖着不修，租客能否自己维修、让房东承担费用并减少租金？",
        CIVIL,
        ["第七百一十二条", "第七百一十三条"],
    ),
    (
        "lease_term",
        "武汉房屋租赁合同约定租期三十年，超过二十年的部分有效吗？到期续租最长可以约定多久？",
        CIVIL,
        ["第七百零五条"],
    ),
    (
        "written_lease",
        "租房一年只作口头约定，没有书面合同且租期无法确定，应当如何认定租赁期限？",
        CIVIL,
        ["第七百零七条"],
    ),
    (
        "sublease",
        "租客未经房东同意将武汉的租赁房屋转租，房东能否解除合同？经同意转租后原租赁合同是否仍有效？",
        CIVIL,
        ["第七百一十六条"],
    ),
    (
        "unpaid_rent",
        "租客没有正当理由拖欠房租，出租人可以直接解除合同吗，还是需要先催告并给合理期限？",
        CIVIL,
        ["第七百二十二条"],
    ),
    (
        "sale_lease",
        "武汉租房期间房东把房屋卖给其他人，原租赁合同会因此失效吗？",
        CIVIL,
        ["第七百二十五条"],
    ),
    (
        "renewal",
        "住房租赁到期后租客继续居住，房东没有提出异议，合同还有效吗？租客是否有同等条件优先承租权？",
        CIVIL,
        ["第七百三十四条"],
    ),
    (
        "penalty",
        "提前退租被要求支付剩余全年租金作为违约金，约定违约金明显超过损失时能否申请降低？",
        CIVIL,
        ["第五百八十五条"],
    ),
    (
        "force_majeure",
        "合同因不可抗力无法履行，应如何减免责任、通知对方和提供证明？迟延履行之后发生不可抗力能否免责？",
        CIVIL,
        ["第五百九十条"],
    ),
    (
        "standard_terms",
        "合同提供方使用格式条款减轻自身责任、加重对方责任，应履行哪些提示说明义务，哪些条款可能无效？",
        CIVIL,
        ["第四百九十六条", "第四百九十七条"],
    ),
    (
        "dismissal",
        "用人单位违法解除劳动合同，劳动者能否要求继续履行？不继续履行时赔偿金标准是什么？",
        LABOR,
        ["第四十八条", "第八十七条"],
    ),
    (
        "guarantee",
        "保证合同没有约定保证期间，或者约定早于主债务履行期限，保证期间怎样确定？是否可以中止、中断或延长？",
        CIVIL,
        ["第六百九十二条"],
    ),
]


@contextmanager
def evaluation_lock(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    with (output / "evaluation.lock").open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("evaluation_already_running") from None
        yield


def retrieval_identity(path: Path) -> dict:
    config = load(path)
    embedding = load(Path(config["embedding_config_file"]))
    return {
        "embedding": {k: embedding.get(k) for k in ("base_url", "model", "dimensions")},
        "rerank": {k: config["rerank"].get(k) for k in ("model", "endpoint")},
    }


def article_span(text: str, label: str) -> tuple[int, int]:
    matches = list(re.finditer(r"(?m)^" + re.escape(label) + r"[\t \u3000]", text))
    if len(matches) != 1:
        raise ValueError("reference_article_not_unique")
    start = matches[0].start()
    boundary = re.search(
        r"(?m)^第[〇零一二三四五六七八九十百千万]+[条章节编][\t \u3000]",
        text[matches[0].end() :],
    )
    end = matches[0].end() + boundary.start() if boundary else len(text)
    return start, start + len(text[start:end].rstrip())


def coverage(refs: list[dict], rows: list[dict]) -> dict:
    covered = total = complete = 0
    for ref in refs:
        intervals = sorted(
            (
                max(ref["start"], r["metadata"]["start_char"]),
                min(ref["end"], r["metadata"]["end_char"]),
            )
            for r in rows
            if r["metadata"]["document_id"] == ref["document_id"]
        )
        cursor, n = ref["start"], 0
        for start, end in intervals:
            if end > max(cursor, start):
                n += end - max(cursor, start)
                cursor = end
        length = ref["end"] - ref["start"]
        total += length
        covered += n
        complete += n == length
    return {
        "character_coverage": covered / total,
        "full_article_recall": complete / len(refs),
    }


def summarize(records: list[dict], case_ids: list[str]) -> dict:
    expected = {(i, s) for i in case_ids for s in ("rrf", "rerank")}
    actual = {(r["case_id"], r["stage"]) for r in records}
    if len(actual) != len(records) or not actual <= expected:
        raise ValueError("duplicate_or_unexpected_scores")
    for r in records:
        if not all(
            type(r[m]) in (int, float) and math.isfinite(r[m]) and 0 <= r[m] <= 1
            for m in ("precision", "recall")
        ):
            raise ValueError("invalid_metric_value")
    if actual != expected:
        return {
            "status": "incomplete",
            "completed": len(actual),
            "expected": len(expected),
        }
    return {
        "status": "completed",
        "cases": len(case_ids),
        "stages": {
            s: {
                "context_precision": statistics.mean(
                    r["precision"] for r in records if r["stage"] == s
                ),
                "context_recall": statistics.mean(
                    r["recall"] for r in records if r["stage"] == s
                ),
            }
            for s in ("rrf", "rerank")
        },
    }


def prepare_dataset(report: Path, completions=None) -> list[dict]:
    entries = load(report)["rows"]
    documents = {}
    for title in (CIVIL, LABOR):
        selected = [
            e
            for e in entries
            if e["title"] == title and e["source_relative_path"].startswith("法律/")
        ]
        if len(selected) != 1:
            raise ValueError("reference_document_not_unique")
        entry = selected[0]
        folder = Path(entry["directory"])
        if completions is not None:
            if sha(folder / "completed.json") != completions[entry["id"]]:
                raise ValueError("reference_snapshot_changed")
            hashes = load(folder / "completed.json")["data"]["output_hashes"]
            if any(
                sha(folder / name) != hashes[name]
                for name in ("document.json", "fulltext.txt")
            ):
                raise ValueError("reference_artifact_changed")
        meta = load(folder / "document.json")
        text = (folder / "fulltext.txt").read_text(encoding="utf-8")
        if digest(text) != meta["fulltext_sha256"]:
            raise ValueError("reference_fulltext_changed")
        documents[title] = (entry, meta, text)
    dataset = []
    for ident, query, title, labels in CASES:
        entry, meta, text = documents[title]
        refs = []
        for label in labels:
            start, end = article_span(text, label)
            refs.append(
                {
                    "document_id": meta["document_id"],
                    "source_id": entry["id"],
                    "source_sha256": entry["source_sha256"],
                    "fulltext_sha256": meta["fulltext_sha256"],
                    "title": title,
                    "article": label,
                    "start": start,
                    "end": end,
                    "text": text[start:end],
                }
            )
        dataset.append(
            {
                "id": ident,
                "question": query,
                "region": "湖北",
                "reference": title + "\n" + "\n".join(r["text"] for r in refs),
                "references": refs,
            }
        )
    return dataset


async def evaluate(args):
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    import httpx
    from legal_query.config import EmbeddingConfig
    from legal_query.window_retrieval import AlibabaReranker, Options, search_windows
    from openai import AsyncOpenAI
    from pymilvus import MilvusClient
    from ragas.llms import llm_factory
    from ragas.llms.base import InstructorBaseRagasLLM
    from ragas.metrics.collections import ContextPrecision, ContextRecall

    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    logging.getLogger("instructor").setLevel(logging.CRITICAL)
    args.output.mkdir(parents=True, exist_ok=True)
    ready = load(args.state / "ready.json")
    report = Path(ready["source_report"])
    manifest_path = args.state / "source-artifacts.json"
    if (
        sha(report) != ready["release_sha256"]
        or sha(manifest_path) != ready["identity"]["source_snapshot_sha256"]
    ):
        raise ValueError("reference_manifest_changed")
    dataset = prepare_dataset(report, load(manifest_path)["completions"])
    secret = load(args.judge_config)
    identity = {
        "dataset_sha256": digest(dataset),
        "ready_sha256": sha(args.state / "ready.json"),
        "evaluator_sha256": sha(Path(__file__)),
        "retriever_sha256": sha(
            Path(__file__).parent / "legal_query/window_retrieval.py"
        ),
        "ragas": importlib.metadata.version("ragas"),
        "judge_model": secret["LAWYER_DASHSCOPE_MODEL"],
        "judge_base_url": secret["LAWYER_DASHSCOPE_BASE_URL"],
        "temperature": 0,
        "top_k": 7,
        "metrics": ["ContextPrecision", "ContextRecall"],
        "retrieval_configuration": retrieval_identity(args.retrieval_config),
        "dependency_lock_sha256": sha(Path(__file__).parent / "uv.lock"),
        "embedding_adapter_sha256": sha(
            Path(__file__).parent / "legal_query/config.py"
        ),
        "corpus_adapter_sha256": sha(
            Path(__file__).parent / "legal_query/window_corpus.py"
        ),
        "enable_thinking": False,
        "output_token_limit": None,
    }
    identity_path = args.output / "identity.json"
    if identity_path.exists() and load(identity_path) != identity:
        raise ValueError("evaluation_identity_changed_use_new_output")
    save(identity_path, identity)
    save(args.output / "dataset.json", dataset)
    tag = contextvars.ContextVar("evaluation_tag", default="retrieval")

    def ledger(record):
        with (args.output / "usage.jsonl").open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {"time": time.time(), "task": tag.get(), **record},
                    ensure_ascii=False,
                )
                + "\n"
            )

    original_embed = EmbeddingConfig.embed_with_usage

    def logged_embed(self, query):
        attempt = uuid.uuid4().hex
        ledger({"stage": "embedding", "event": "started", "attempt": attempt})
        try:
            vector, info = original_embed(self, query)
        except Exception as exc:
            ledger(
                {
                    "stage": "embedding",
                    "event": "unknown",
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "usage": None,
                }
            )
            raise
        ledger({"stage": "embedding", "event": "response", "attempt": attempt, **info})
        return vector, info

    original_rerank = AlibabaReranker._postprocess_nodes

    def logged_rerank(self, nodes, query_bundle=None):
        attempt = uuid.uuid4().hex
        ledger({"stage": "rerank", "event": "started", "attempt": attempt})
        try:
            result = original_rerank(self, nodes, query_bundle)
        except Exception as exc:
            ledger(
                {
                    "stage": "rerank",
                    "event": "unknown",
                    "attempt": attempt,
                    "error_type": type(exc).__name__,
                    "usage": None,
                }
            )
            raise
        ledger(
            {
                "stage": "rerank",
                "event": "response",
                "attempt": attempt,
                **self.call_info,
            }
        )
        return result

    EmbeddingConfig.embed_with_usage = logged_embed
    AlibabaReranker._postprocess_nodes = logged_rerank
    milvus = MilvusClient(uri="http://localhost:19530", db_name="blog", timeout=30)
    retrieved = {}
    try:
        for case in dataset:
            path = args.output / "retrieval" / (case["id"] + ".json")
            if not path.exists():
                token = tag.set(case["id"])
                result = search_windows(
                    milvus,
                    case["question"],
                    args.state / "ready.json",
                    Options(region=case["region"]),
                    args.retrieval_config,
                )
                save(path, result)
                tag.reset(token)
                print(
                    json.dumps({"retrieved": case["id"]}, ensure_ascii=False),
                    flush=True,
                )
            retrieved[case["id"]] = load(path)
    finally:
        EmbeddingConfig.embed_with_usage = original_embed
        AlibabaReranker._postprocess_nodes = original_rerank
        milvus.close()

    async def request_hook(request):
        attempt = uuid.uuid4().hex
        request.extensions["evaluation_attempt"] = attempt
        ledger(
            {
                "stage": "judge",
                "event": "started",
                "attempt": attempt,
                "request_sha256": digest(request.content.decode("utf-8")),
            }
        )

    async def response_hook(response):
        await response.aread()
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        ledger(
            {
                "stage": "judge",
                "event": "response",
                "attempt": response.request.extensions.get("evaluation_attempt"),
                "http_status": response.status_code,
                "usage": payload.get("usage"),
                "request_id": payload.get("id"),
                "finish_reasons": [
                    c.get("finish_reason") for c in payload.get("choices", [])
                ],
            }
        )

    async with httpx.AsyncClient(
        timeout=120,
        follow_redirects=False,
        event_hooks={"request": [request_hook], "response": [response_hook]},
    ) as http:
        client = AsyncOpenAI(
            api_key=secret["DASHSCOPE_API_KEY"],
            base_url=identity["judge_base_url"],
            http_client=http,
            max_retries=0,
        )
        base = llm_factory(
            identity["judge_model"],
            client=client,
            temperature=0,
            max_retries=2,
            extra_body={"enable_thinking": False},
        )
        base.model_args.pop("max_tokens", None)
        semaphore = asyncio.Semaphore(3)
        cache_locks = {}

        class CachedJudge(InstructorBaseRagasLLM):
            def generate(self, *a, **kw):
                raise RuntimeError("async_judge_required")

            async def agenerate(self, prompt, response_model):
                key = digest([identity, prompt, response_model.model_json_schema()])
                path = args.output / "judge-cache" / (key + ".json")
                async with cache_locks.setdefault(key, asyncio.Lock()):
                    if path.exists():
                        return response_model.model_validate(load(path))
                    async with semaphore:
                        result = await base.agenerate(prompt, response_model)
                    save(path, result.model_dump())
                    return result

        judge = CachedJudge()
        records = []

        async def one(case, stage):
            tag.set(case["id"] + ":" + stage)
            path = args.output / "scores" / (case["id"] + "-" + stage + ".json")
            if path.exists():
                records.append(load(path))
                return
            rows = retrieved[case["id"]]["rrf" if stage == "rrf" else "hits"][:7]
            kwargs = {
                "user_input": case["question"],
                "reference": case["reference"],
                "retrieved_contexts": [r["embedding_text"] for r in rows],
            }
            values = {}
            for name, metric in [
                ("precision", ContextPrecision(llm=judge)),
                ("recall", ContextRecall(llm=judge)),
            ]:
                metric_path = (
                    args.output
                    / "metrics"
                    / (case["id"] + "-" + stage + "-" + name + ".json")
                )
                if metric_path.exists():
                    value = load(metric_path)["value"]
                else:
                    value = float((await metric.ascore(**kwargs)).value)
                    if not math.isfinite(value) or not 0 <= value <= 1:
                        raise ValueError("invalid_ragas_score")
                    save(metric_path, {"value": value})
                values[name] = value
            record = {
                "case_id": case["id"],
                "stage": stage,
                **values,
                **coverage(case["references"], rows),
            }
            save(path, record)
            records.append(record)
            print(json.dumps(record, ensure_ascii=False), flush=True)

        outcomes = await asyncio.gather(
            *(one(c, s) for c in dataset for s in ("rrf", "rerank")),
            return_exceptions=True,
        )
        failures = [type(r).__name__ for r in outcomes if isinstance(r, BaseException)]
    summary = summarize(records, [c["id"] for c in dataset])
    summary.update(identity=identity, failures=failures, scores=records)
    if summary["status"] == "completed":
        for stage in ("rrf", "rerank"):
            for metric in ("character_coverage", "full_article_recall"):
                summary["stages"][stage][metric] = statistics.mean(
                    r[metric] for r in records if r["stage"] == stage
                )
    save(args.output / "summary.json", summary)
    print(
        json.dumps(
            {k: v for k, v in summary.items() if k not in ("identity", "scores")},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return 0 if summary["status"] == "completed" else 1


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--retrieval-config", type=Path, required=True)
    p.add_argument("--judge-config", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    with evaluation_lock(args.output):
        return asyncio.run(evaluate(args))


if __name__ == "__main__":
    raise SystemExit(main())
