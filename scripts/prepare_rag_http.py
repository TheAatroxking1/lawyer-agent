"""准备内网配置与独立调用密钥；不覆盖现有文件，不输出任何密钥。"""

import argparse
import ipaddress
import secrets
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind-ip", default="127.0.0.1")
    args = parser.parse_args()
    address = ipaddress.IPv4Address(args.bind_ip)
    if (
        not address.is_private
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
    ):
        parser.error("必须选择实际的内网IPv4或回环地址")
    root = Path(__file__).resolve().parents[1]
    folder = root / "deploy" / "secrets"
    folder.mkdir(exist_ok=True)
    for name, value in (
        ("rag-api-key.txt", secrets.token_urlsafe(32)),
        ("rag-http.env", f"RAG_BIND_IP={address}\nRAG_PORT=8088\n"),
    ):
        path = folder / name
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(value)
            print(f"已创建：{path}")
        except FileExistsError:
            print(f"保留已有文件：{path}")


if __name__ == "__main__":
    main()
