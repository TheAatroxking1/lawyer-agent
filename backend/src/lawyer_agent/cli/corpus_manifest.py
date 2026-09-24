"""Prepare local, unconfirmed corpus metadata candidates without importing data."""

from __future__ import annotations

import argparse
from pathlib import Path

from lawyer_agent.application.legal_corpus_manifest import prepare_corpus_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成待核对语料元数据候选，不导入或发布")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    try:
        result = prepare_corpus_manifest(
            source_root=args.source_root, manifest_path=args.manifest,
            output_path=args.output, limit=args.limit,
        )
    except (OSError, ValueError):
        # Do not echo manifest fields, validation payloads or document content.
        print("候选生成失败：输入或输出边界无效，未生成可供确认的完整结果。")
        return 2
    print(
        f"生成 {result.selected_sources}/{result.manifest_sources} 份待核对候选；"
        f"其中 {result.incomplete_sources} 份来源尚未完成导出。"
        f"原件哈希与正式元数据尚未核验。输出：{result.output_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
