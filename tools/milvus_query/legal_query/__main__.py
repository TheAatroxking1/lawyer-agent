"""命令行：status、hybrid、dense、bm25、exact。所有数据库操作只读。"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any, NoReturn

from pymilvus import MilvusClient

from legal_query.config import EmbeddingConfig, QueryError
from legal_query.search import COLLECTION, SearchOptions, exact, preflight, search


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse默认回显错误参数；查询工具不在错误日志复述用户输入。
        print(json.dumps({"status": "error", "code": "invalid_arguments"}), file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    parser = ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="检查集合、索引及记录数，不调用Embedding")
    for name in ("hybrid", "dense", "bm25"):
        cmd = sub.add_parser(name)
        cmd.add_argument("query", help="自然语言问题")
        cmd.add_argument("--document-id", action="append", default=[])
        cmd.add_argument("--top-k", type=int, default=10)
        cmd.add_argument("--candidate-k", type=int, default=50)
        cmd.add_argument("--recall-k", type=int, default=100)
        cmd.add_argument("--ranker", choices=["rrf", "weighted"], default="rrf")
        cmd.add_argument("--raw-bm25", action="store_true", help="BM25使用原问题，用于清理前后对照")
    cmd = sub.add_parser("exact", help="按文书ID和条号查询，不调用Embedding")
    cmd.add_argument("document_id")
    cmd.add_argument("article_no")
    cmd.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    client = None
    exit_code = 0
    try:
        # SDK异常日志可能含查询正文；CLI统一输出稳定错误码。
        logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
        client = MilvusClient(
            uri=os.environ.get("MILVUS_URI", "http://standalone:19530"),
            db_name=os.environ.get("MILVUS_DB", "blog"),
            token=os.environ.get("MILVUS_TOKEN", ""),
            timeout=15,
        )
        info = preflight(client)
        result: dict[str, Any]
        if args.command == "status":
            rows = client.query(
                COLLECTION,
                filter="",
                output_fields=["count(*)"],
                consistency_level="Strong",
                timeout=15,
            )
            result = {"status": "ok", **info, "rows": rows[0]["count(*)"]}
        elif args.command == "exact":
            result = exact(client, args.document_id, args.article_no, args.limit)
        else:
            options = SearchOptions(
                mode=args.command,
                top_k=args.top_k,
                candidate_k=args.candidate_k,
                recall_k=args.recall_k,
                document_ids=tuple(args.document_id),
                ranker=args.ranker,
                raw_bm25=args.raw_bm25,
            )
            result = search(client, args.query, options, lambda q: EmbeddingConfig.load().embed(q))
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    except QueryError as error:
        print(json.dumps({"status": "error", "code": str(error)}), file=sys.stderr)
        exit_code = 2
    except Exception:
        print(json.dumps({"status": "error", "code": "query_failed"}), file=sys.stderr)
        exit_code = 3
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                print(
                    json.dumps({"status": "error", "code": "client_close_failed"}), file=sys.stderr
                )
                exit_code = 3
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
