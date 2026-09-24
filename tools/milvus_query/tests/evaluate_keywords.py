"""手动运行的真实只读对照，不由unittest执行；每题最多一次付费Embedding。"""

import hashlib
import json
import logging
import os
import sys
from pathlib import Path

from legal_query.config import (
    DIMENSIONS,
    MODEL,
    EmbeddingConfig,
    QueryError,
    require,
    validate_vector,
)
from legal_query.keywords import bm25_query
from legal_query.search import COLLECTION, SearchOptions, preflight, search
from pymilvus import MilvusClient

# 运行前固定，覆盖宽泛问题、具体权利、否定、金额/期限、引号与不应扩展的公租房。
# 不是法律质量金标集；人工应逐条检查正文支持和地域，不能只按法名打分。
QUESTIONS = [
    "北京租房的时候，有什么需要注意的",
    "上海租房的时候，有什么需要注意的",
    "深圳租房的时候，有什么需要注意的",
    "租房押金不退，有什么规定？",
    "请问，用人单位违法解除劳动合同，应如何支付赔偿金？",
    "请问，未签劳动合同超过一年，可以要求双倍工资吗？",
    "请问，加班工资如何计算？",
    "职工带薪年休假，有哪些规定？",
    "网购七日无理由退货，有哪些规定？",
    "个人信息处理需要取得同意，有什么规定？",
    "酒后驾驶机动车，有哪些规定？",
    "未成年人保护，有什么规定？",
    "用人单位违法解除劳动合同，应如何支付赔偿金？",
    "借条没有约定利息，可以要求借款人支付利息吗？",
    "收到处罚决定后60日内能否申请行政复议，罚款5000元",
    "不需要审批的建设项目如何备案？",
    "未成年人能不能签合同？",
    "公租房和廉租房的申请条件",
    "出租房安全管理责任",
    "民法典第一千一百九十八条安全保障义务与注意义务",
    "个人信息保护法第十三条规定的处理条件",
    "查询《北京市住房租赁条例》第十条",
    "刑事诉讼法第一百四十六条鉴定",
    "不得解除劳动合同的情形",
]


def main() -> int:
    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    require(len(sys.argv) == 2, "evaluation_output_required")
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    config = EmbeddingConfig.load()
    client = MilvusClient(
        uri=os.environ["MILVUS_URI"], db_name=os.getenv("MILVUS_DB", "blog"), timeout=15
    )
    try:
        info = preflight(client)

        def count():
            return client.query(
                COLLECTION,
                filter="",
                output_fields=["count(*)"],
                consistency_level="Strong",
                timeout=15,
            )[0]["count(*)"]

        before_count = count()
        calls = 0
        cases = []
        for index, question in enumerate(QUESTIONS, 1):
            # 同一问题仅编码一次，新旧两组复用；失败重跑可复用已校验的查询向量。
            key = hashlib.sha256(question.encode("utf-8")).hexdigest()
            cache = output / (key + ".vector.json")
            if cache.exists():
                saved = json.loads(cache.read_text(encoding="utf-8"))
                require(
                    saved["question"] == question
                    and saved["model"] == MODEL
                    and saved["dimensions"] == DIMENSIONS,
                    "evaluation_cache_mismatch",
                )
                vector = validate_vector(saved["vector"])
            else:
                vector = config.embed(question)
                calls += 1
                cache.write_text(
                    json.dumps(
                        {
                            "question": question,
                            "model": MODEL,
                            "dimensions": DIMENSIONS,
                            "vector": vector,
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            case = {"number": index, "question": question, "bm25_query": bm25_query(question)}
            for label, raw in (("before", True), ("after", False)):
                case[label] = {
                    mode: search(
                        client,
                        question,
                        SearchOptions(mode=mode, top_k=5, raw_bm25=raw),
                        lambda _q, vector=vector: vector,
                    )
                    for mode in ("bm25", "hybrid")
                }
            cases.append(case)
            (output / f"case-{index:02d}.json").write_text(
                json.dumps(case, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps({"completed": index, "total": len(QUESTIONS)}, ensure_ascii=False),
                flush=True,
            )
        after_count = count()
        require(before_count == after_count, "evaluation_row_count_changed")
        result = {
            "schema": info,
            "before_rows": before_count,
            "after_rows": after_count,
            "embedding_calls_this_run": calls,
            "rules_sha256": hashlib.sha256(
                Path(__file__).parents[1].joinpath("legal_query/keywords.py").read_bytes()
            ).hexdigest(),
            "cases": cases,
        }
        (output / "comparison.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(
            json.dumps(
                {"error": str(error) if isinstance(error, QueryError) else "evaluation_failed"}
            ),
            file=sys.stderr,
        )
        sys.exit(1)
