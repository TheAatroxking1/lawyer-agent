#!/usr/bin/env python3
"""Generate a small sample legal DOCX for the corpus publish CLI (stdlib only).

Produces a minimal, read-only-safe .docx (no external packages) whose paragraphs
mimic a law instrument: title line, optional 编/章 headings and numbered
articles ("第一条 ..."). Feed the output to:

    uv run --no-sync python -m lawyer_agent.cli.corpus_publish --docx <out.docx> ...

Usage:
    python scripts/make_sample_law_docx.py <out.docx>
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

_XML_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_LINES = [
    "房屋租赁合同司法解释（样例）",
    "第一章 一般规定",
    "第一条 为正确审理城镇房屋租赁合同纠纷案件，保护当事人合法权益，根据《中华人民共和国民法典》等法律规定，制定本解释。",
    "第二条 出租人应当按照约定将租赁物交付承租人，并在租赁期限内保持租赁物符合约定的用途。",
    "第三条 承租人应当按照约定的方法使用租赁物。因使用不当造成租赁物毁损、灭失的，应当承担赔偿责任。",
    "第二章 合同的履行与解除",
    "第四条 承租人应当按照约定的期限支付租金。对支付租金的期限没有约定或者约定不明确的，应当在租赁期届满时支付。",
    "第五条 承租人无正当理由未支付或者迟延支付租金的，出租人可以请求承租人支付欠付租金，并可以依照约定主张违约金。",
    "第六条 约定的违约金过分高于造成的损失的，人民法院可以根据当事人的请求予以适当减少。",
    "第七条 出租人知道或者应当知道承租人转租，但在合理期限内未提出异议的，视为同意转租。",
    "第八条 因不可归责于承租人的事由，致使租赁物部分或者全部毁损、灭失的，承租人可以请求减少租金或者不支付租金。",
    "第九条 租赁期限届满，承租人继续使用租赁物，出租人没有提出异议的，原租赁合同继续有效，但租赁期限为不定期。",
    "第三章 附则",
    "第十条 本解释自公布之日起施行。",
]


def build(lines: list[str]) -> bytes:
    runs = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines)
    xml = (
        "<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
        f'<w:document xmlns:w="{_XML_NS}"><w:body>{runs}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python scripts/make_sample_law_docx.py <out.docx>", file=sys.stderr)
        return 2
    out = Path(argv[0])
    if not out.suffix.lower() == ".docx":
        print("output path must end with .docx", file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(build(_LINES))
    print(f"wrote {len(_LINES)} paragraphs to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
