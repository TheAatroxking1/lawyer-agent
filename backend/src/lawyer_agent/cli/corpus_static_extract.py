"""Explicit, single-source passive recovery after an observed Office rejection."""

from __future__ import annotations

import argparse
from hashlib import sha256
from pathlib import Path

from lawyer_agent.cli.corpus_convert import conversion_lock
from lawyer_agent.infrastructure.documents.conversion_paths import ConversionPaths
from lawyer_agent.infrastructure.documents.word_conversion import (
    ConversionRecord,
    WordConverter,
    convert_document,
    file_sha256,
    write_manifest,
)


def _converter() -> WordConverter:
    from lawyer_agent.infrastructure.documents.binary_word_text import BinaryWordTextConverter

    return BinaryWordTextConverter()


def _origin(
    record: ConversionRecord,
    source: Path,
    digest: str,
) -> bool:
    return (
        record.source_path == str(source)
        and record.source_sha256 == digest
        and record.status == "failed"
        and record.error_code == "word_conversion_failed"
        and record.recovery_reason is None
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="显式恢复单份旧Word的静态文本，不启动Office")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument(
        "--reason",
        choices=["office_validation_failed"],
        required=True,
        help="操作者已从诊断确认Office文件校验拒绝；保留原始失败记录",
    )
    args = parser.parse_args(argv)
    try:
        paths = ConversionPaths(args.source_root, args.output_root)
        source = paths.source(args.source)
        if not paths.source_root.is_dir() or not source.is_file():
            raise ValueError("source_missing")
        if paths.source_root.is_relative_to(paths.output_root) or paths.output_root.is_relative_to(
            paths.source_root
        ):
            raise ValueError("source_output_overlap")
        with conversion_lock(paths.output_root):
            digest = file_sha256(source)
            identity = source.relative_to(paths.source_root).as_posix() + "\0" + digest
            folder = paths.output(paths.output_root / sha256(identity.encode()).hexdigest())
            prior_path = paths.output(folder / "record.json")
            prior = ConversionRecord.model_validate_json(prior_path.read_text(encoding="utf-8"))
            if prior.source_path != str(source) or prior.source_sha256 != digest:
                raise ValueError("prior_source_mismatch")
            origin_path = paths.output(folder / "recovery_origin.json")
            if origin_path.exists():
                origin = ConversionRecord.model_validate_json(
                    origin_path.read_text(encoding="utf-8")
                )
                if not _origin(origin, source, digest):
                    raise ValueError("recovery_origin_invalid")
                if not _origin(prior, source, digest) and not (
                    prior.recovery_reason == "office_validation_failed"
                    and (
                        prior.status == "failed"
                        or prior.converter_version == "binary-word-static-text-v1"
                    )
                ):
                    raise ValueError("prior_conversion_not_recoverable")
            else:
                if not _origin(prior, source, digest):
                    raise ValueError("observed_word_failure_required")
                write_manifest(origin_path, (prior,))
            record = convert_document(
                source,
                source_root=paths.source_root,
                output_root=paths.output_root,
                converter=_converter(),
                recovery_reason="office_validation_failed",
            )
            records = tuple(
                ConversionRecord.model_validate_json(paths.output(p).read_text(encoding="utf-8"))
                for p in sorted(paths.output_root.glob("*/record.json"))
            )
            write_manifest(paths.output(paths.output_root / "manifest.jsonl"), records)
    except (OSError, ValueError):
        print("静态恢复未完成：来源、原失败记录或输出边界无效。")
        return 2
    print(f"{record.status}: {source.name}；静态恢复原因：{record.recovery_reason}")
    return 0 if record.status == "converted" else 1


if __name__ == "__main__":
    raise SystemExit(main())
