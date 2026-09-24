"""Locked offline DOC conversion; each artifact is verified before publication."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from lawyer_agent.infrastructure.documents.conversion_paths import (
    ConversionPaths,
    unredirected_path,
)
from lawyer_agent.infrastructure.documents.source_format import is_legacy_word_source
from lawyer_agent.infrastructure.documents.word_conversion import (
    ConversionRecord,
    convert_document,
    write_manifest,
)
from lawyer_agent.infrastructure.documents.word_driver import (
    BatchPowerShellWordConverter,
    PowerShellWordConverter,
)


@contextmanager
def conversion_lock(root: Path) -> Iterator[None]:
    root = unredirected_path(root)
    root.mkdir(parents=True, exist_ok=True)
    with unredirected_path(root / ".conversion.lock").open("a+b") as stream:
        stream.seek(0)
        stream.write(b"0")
        stream.flush()
        stream.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("conversion_batch_busy") from exc
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform == "win32":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="只读、受控转换旧 Word；失败保留记录")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument(
        "--worker-script",
        type=Path,
        default=Path(__file__).resolve().parents[4] / "scripts/word_conversion_worker.ps1",
    )
    parser.add_argument("--batch", action="store_true", help="有界复用Word，默认每实例25份")
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args(argv)
    if args.batch and args.worker_script.name == "word_conversion_worker.ps1":
        args.worker_script = args.worker_script.with_name("word_batch_worker.ps1")
    try:
        paths = ConversionPaths(args.source_root, args.output_root)
        root, output = paths.source_root, paths.output_root
        if not root.is_dir() or root.is_relative_to(output) or output.is_relative_to(root):
            raise ValueError("source_output_overlap_or_missing")
        if args.limit is not None and args.limit < 1:
            raise ValueError("limit_must_be_positive")
        sources = sorted(
            (
                candidate
                for p in (args.source or root.rglob("*"))
                if p.is_file() and not p.name.startswith(("~", "."))
                for candidate in (paths.source(p),)
                if is_legacy_word_source(candidate)
            ),
            key=str,
        )
        if args.source and len(sources) != len(args.source):
            raise ValueError("source_must_be_doc")
        if any(not p.is_relative_to(root) for p in sources):
            raise ValueError("source_outside_root")
        if args.limit:
            sources = sources[: args.limit]
        with conversion_lock(output):
            converter_type = BatchPowerShellWordConverter if args.batch else PowerShellWordConverter
            options = {"max_files": args.batch_size} if args.batch else {}
            converter = converter_type(
                work_root=output / ".work",
                source_root=root,
                output_root=output,
                worker_script=args.worker_script,
                timeout_seconds=args.timeout_seconds,
                **options,
            )
            try:
                converter.recover_pending()
                failed = 0
                for source in sources:
                    record = convert_document(
                        source, source_root=root, output_root=output, converter=converter
                    )
                    records = tuple(
                        ConversionRecord.model_validate_json(p.read_text(encoding="utf-8"))
                        for p in sorted(output.glob("*/record.json"))
                    )
                    write_manifest(paths.output(output / "manifest.jsonl"), records)
                    print(f"{record.status}: {source.name}")
                    failed += record.status == "failed"
                    if record.error_code in {
                        "word_ownership_unverified",
                        "word_settings_restore_failed",
                        "word_busy",
                    }:
                        break
                if isinstance(converter, BatchPowerShellWordConverter):
                    converter.close()
                print(f"本次选择 {len(sources)}；失败 {failed}；清单 {output / 'manifest.jsonl'}")
                return 1 if failed else 0
            finally:
                if isinstance(converter, BatchPowerShellWordConverter):
                    converter.close()
    except (OSError, ValueError) as exc:
        # These errors carry stable control codes; never print COM payloads.
        print(f"转换未完成: {type(exc).__name__}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
