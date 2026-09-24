"""Build a human-readable file index for an already generated corpus manifest."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from pathlib import Path


def _text(value: object) -> str:
    text = str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@")) else text


def main() -> int:
    parser = argparse.ArgumentParser(description="生成切块文件中文查找清单")
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.output_root.resolve(strict=True)
    entries = [
        json.loads(line)
        for line in (root / "manifest.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    descriptor, temporary = tempfile.mkstemp(prefix=".file-index-", dir=root)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([
                "原始文件", "处理状态", "块数", "子块数", "质量标记",
                "切块文件绝对路径", "段落文件绝对路径", "元数据文件绝对路径", "来源SHA256",
            ])
            for row in entries:
                count = child_count = ""
                flags = row.get("code", "")
                chunks = paragraphs = metadata_file = ""
                if row["status"] == "completed":
                    folder = root / row["output_directory"]
                    if folder.is_symlink() or folder.is_junction() or folder.parent != root:
                        raise ValueError("unsafe export folder")
                    for filename in ("document.json", "chunks.jsonl", "paragraphs.jsonl"):
                        target = folder / filename
                        if target.is_symlink() or target.is_junction():
                            raise ValueError("unsafe export file")
                        if not target.resolve(strict=True).is_relative_to(root):
                            raise ValueError("export file outside root")
                    metadata = json.loads((folder / "document.json").read_text(encoding="utf-8"))
                    count, child_count = metadata["chunk_count"], metadata["child_count"]
                    flags = " / ".join(metadata["quality_flags"])
                    chunks, paragraphs, metadata_file = (
                        str(folder / name)
                        for name in ("chunks.jsonl", "paragraphs.jsonl", "document.json")
                    )
                writer.writerow([_text(value) for value in (
                    row["source_path"], row["status"], count, child_count, flags,
                    chunks, paragraphs, metadata_file, row.get("source_sha256", ""),
                )])
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, root / "文件清单.csv")
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"已写入 {len(entries)} 条文件位置记录：{root / '文件清单.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
