"""Command-line entry point for the offline legal-corpus export."""

from __future__ import annotations

import argparse
from pathlib import Path

from lawyer_agent.application.legal_corpus_export import ExportConfig, ExportError, export_corpus


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="只读扫描法规 Word 文件并导出可核验 JSONL 切块")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--limit", type=int, help="试点上限；summary 会明确记录")
    parser.add_argument(
        "--docx-only",
        action="store_true",
        help="仅枚举 DOCX（在 --limit 前过滤，适合 DOCX 试点）",
    )
    parser.add_argument("--conversion-manifest", type=Path)
    parser.add_argument("--converted-root", type=Path)
    parser.add_argument("--max-leaf-chars", type=int, default=600)
    parser.add_argument("--window-chars", type=int, default=400)
    parser.add_argument("--overlap-chars", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = export_corpus(
            ExportConfig(
                source_root=args.source_root,
                output_root=args.output_root,
                limit=args.limit,
                conversion_manifest=args.conversion_manifest,
                converted_root=args.converted_root,
                max_leaf_chars=args.max_leaf_chars,
                window_chars=args.window_chars,
                overlap_chars=args.overlap_chars,
                docx_only=args.docx_only,
            )
        )
    except (ExportError, FileNotFoundError, NotADirectoryError) as exc:
        print(f"导出配置错误: {exc}")
        return 2
    print(
        f"本次清单 {summary.total}；完成 {summary.completed}；"
        f"待转换 {summary.pending_conversion}；失败 {summary.failed}；"
        f"结构待复核 {summary.structure_review_required}"
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
