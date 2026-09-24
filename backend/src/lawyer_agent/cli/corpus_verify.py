"""Verify exported files and write a read-only-source delivery report."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from lawyer_agent.application.legal_export_verification import verify_export


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对照原件核验切块文件、来源清单与全文覆盖")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--conversion-manifest", type=Path)
    parser.add_argument("--converted-root", type=Path)
    parser.add_argument("--allow-subset", action="store_true", help="仅验证试点，不声明全量完成")
    args = parser.parse_args(argv)
    source = args.source_root.resolve(strict=True)
    output = args.output_root.resolve(strict=True)
    if source.is_relative_to(output) or output.is_relative_to(source):
        print("核验配置错误: source_output_overlap")
        return 2
    report = verify_export(
        source_root=args.source_root, output_root=args.output_root,
        require_all_sources=not args.allow_subset,
        conversion_manifest=args.conversion_manifest, converted_root=args.converted_root,
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".verification-", dir=output)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(asdict(report), stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output / "verification.json")
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(
        f"核验通过 {report.verified_documents} 份 / {report.verified_chunks} 块；"
        f"问题 {len(report.errors)} 项；全量完成 {report.complete}"
    )
    return 0 if not report.errors and (args.allow_subset or report.complete) else 1


if __name__ == "__main__":
    raise SystemExit(main())
