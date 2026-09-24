"""只读真实Milvus探针：重用库中向量，不调用Embedding，不代表问答质量评测。"""

import json
import os

from legal_query.config import require
from legal_query.search import COLLECTION, FIELDS, SearchOptions, exact, preflight, search
from pymilvus import MilvusClient

IDS = [
    "4a91a1b6de0d235047c5219984325ec2d0cc7b2be6b713b06e4e0ce2c7400d4e",
    "4efb124a124c94348e01c1a4233eef6f22321e4af4bb4537ccdf2fe61d8db778",
    "7033c91faddbf516d91d181e8f7ba8745be56255b5d6f6a666010ddcacddc8b8",
]


def main():
    client = MilvusClient(uri=os.environ["MILVUS_URI"], db_name="blog", timeout=15)
    try:
        info = preflight(client)

        def count():
            return client.query(
                COLLECTION, filter="", output_fields=["count(*)"], consistency_level="Strong"
            )[0]["count(*)"]

        before = count()
        sources = client.get(
            COLLECTION, IDS, output_fields=FIELDS + ["vector"], consistency_level="Strong"
        )
        require(len(sources) == 3, "smoke_sources_missing")
        probes = []
        for source in sources:
            for mode, ranker in (
                ("dense", "rrf"),
                ("bm25", "rrf"),
                ("hybrid", "rrf"),
                ("hybrid", "weighted"),
            ):
                result = search(
                    client,
                    source["text"],
                    SearchOptions(mode=mode, ranker=ranker),
                    lambda _q, source=source: source["vector"],
                )
                require(
                    source["chunk_id"] in {r["chunk_id"] for r in result["hits"]},
                    "source_not_in_top10",
                )
                probes.append(
                    {
                        "mode": mode,
                        "ranker": ranker,
                        "source_chunk_id": source["chunk_id"],
                        "source_in_top10": True,
                    }
                )
            scoped = search(
                client,
                source["text"],
                SearchOptions(document_ids=(source["document_id"],)),
                lambda _q, source=source: source["vector"],
            )
            require(
                scoped["hits"]
                and all(r["document_id"] == source["document_id"] for r in scoped["hits"]),
                "scope_failed",
            )
            precise = exact(client, source["document_id"], source["article_no"], 100)
            require(source["chunk_id"] in {r["chunk_id"] for r in precise["hits"]}, "exact_failed")
        empty = search(
            client,
            sources[0]["text"],
            SearchOptions(document_ids=("0" * 64,)),
            lambda _q: sources[0]["vector"],
        )
        require(empty["status"] == "empty" and not empty["hits"], "negative_scope_failed")
        after = count()
        require(before == after == 921117, "row_count_changed")
        print(
            json.dumps(
                {
                    "status": "verified",
                    **info,
                    "before_rows": before,
                    "after_rows": after,
                    "probes": probes,
                    "scoped_queries": 3,
                    "exact_queries": 3,
                    "negative_scope": "passed",
                    "embedding_calls": 0,
                },
                ensure_ascii=False,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
