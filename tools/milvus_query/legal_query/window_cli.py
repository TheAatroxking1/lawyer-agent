"""第二版窗口唯一操作入口：import/status/search，保持旧查询服务独立。"""

import argparse
import json
import logging
from pathlib import Path

from pymilvus import MilvusClient, MilvusException

from legal_query.config import QueryError, require
from legal_query.window_corpus import (
    COLLECTION,
    Release,
    import_release,
    validate_ready,
)
from legal_query.window_retrieval import Options, search_windows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", default="http://localhost:19530")
    parser.add_argument("--db", default="blog")
    parser.add_argument("--state", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import")
    imp.add_argument("--report", type=Path, required=True)
    sub.add_parser("status")
    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--config", type=Path, required=True)
    search.add_argument("--region", default="")
    search.add_argument("--clean-only", action="store_true")
    search.add_argument("--rrf-only", action="store_true")
    search.add_argument("--top-k", type=int, default=7)
    search.add_argument("--output", type=Path)
    args = parser.parse_args()
    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    client = None
    try:
        client = MilvusClient(uri=args.uri, db_name=args.db, timeout=30)
        if args.command == "import":
            import_release(
                client,
                Release(args.report),
                args.state,
                target={"uri": args.uri, "database": args.db},
            )
            return 0
        if args.command == "status":
            result = {
                "ready": validate_ready(
                    client,
                    args.state / "ready.json",
                    target={"uri": args.uri, "database": args.db},
                ),
                "rows": client.query(
                    COLLECTION,
                    filter="",
                    output_fields=["count(*)"],
                    consistency_level="Strong",
                )[0]["count(*)"],
            }
            require(result["rows"] == result["ready"]["rows"], "milvus_total_mismatch")
        else:
            result = search_windows(
                client,
                args.query,
                args.state / "ready.json",
                Options(
                    top_k=args.top_k, region=args.region, clean_only=args.clean_only
                ),
                args.config,
                args.rrf_only,
                target={"uri": args.uri, "database": args.db},
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as f:
                    json.dump(result, f, ensure_ascii=False, indent=2)
                result = {
                    "status": result["status"],
                    "collection": COLLECTION,
                    "output": str(args.output),
                    "hit_count": len(result["hits"]),
                    "rerank": result["rerank"],
                }
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except QueryError as exc:
        print(json.dumps({"status": "error", "code": str(exc)}, ensure_ascii=False))
        return 1
    except (MilvusException, OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"status": "error", "type": type(exc).__name__}))
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
