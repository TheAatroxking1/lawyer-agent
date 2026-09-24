"""同事侧客户端示例：仅需Python标准库；不需要PyMilvus、模型或数据库密码。"""

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 不把调用凭据转发到重定向地址。


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", default="用人单位违法解除劳动合同，应如何支付赔偿金？")
    parser.add_argument("--mode", choices=["hybrid", "bm25", "dense"], default="hybrid")
    parser.add_argument(
        "--base-url", default=os.getenv("RAG_BASE_URL", "http://192.168.31.14:8088")
    )
    parser.add_argument("--key-file", default=os.getenv("RAG_API_KEY_FILE", "rag-api-key.txt"))
    args = parser.parse_args()
    try:
        endpoint = urlsplit(args.base_url)
        if (
            endpoint.scheme not in ("http", "https")
            or not endpoint.hostname
            or endpoint.username
            or endpoint.password
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("invalid_endpoint")
        key = Path(args.key_file).read_text(encoding="utf-8-sig").strip()
        # 内网请求不走系统代理；请求超时不表示服务端工作一定终止。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(  # noqa: S310 -- 上方已限制HTTP(S)，目标由调用者指定。
            args.base_url.rstrip("/") + "/api/v1/rag/search",
            data=json.dumps(
                {"query": args.query, "mode": args.mode, "top_k": 5}, ensure_ascii=False
            ).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        )
        with opener.open(request, timeout=75) as response:
            result = json.load(response)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except urllib.error.HTTPError as error:
        print(
            json.dumps(
                {"status": error.code, "message": "接口请求失败；请检查密钥、参数或服务状态"},
                ensure_ascii=False,
            )
        )
    except (OSError, ValueError):
        print(json.dumps({"message": "无法读取密钥或连接RAG服务"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
