from __future__ import annotations

import json
import zipfile
from hashlib import sha256
from pathlib import Path

from lawyer_agent.application.legal_corpus_export import ExportConfig, export_corpus
from lawyer_agent.application.legal_export_verification import verify_export


def test_docm_static_xml_is_exported_without_executing_embedded_content(tmp_path: Path) -> None:
    source_root, output = tmp_path / "source", tmp_path / "output"
    source_root.mkdir()
    source = source_root / "合成法规.docm"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r>'
            '<w:t>第一条 合成宏容器中的静态文字。</w:t>'
            '</w:r></w:p></w:body></w:document>',
        )
        archive.writestr("word/vbaProject.bin", b"synthetic inert VBA placeholder")
    original_hash = sha256(source.read_bytes()).hexdigest()
    summary = export_corpus(ExportConfig(source_root, output))
    assert summary.total == summary.completed == 1
    assert summary.pending_conversion == 0
    metadata_path = next(output.glob("*/document.json"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "macro_enabled_container_no_execution" in metadata["quality_flags"]
    assert sha256(source.read_bytes()).hexdigest() == original_hash
    assert verify_export(source_root=source_root, output_root=output).complete
    docx_only = export_corpus(ExportConfig(source_root, output, docx_only=True))
    assert docx_only.total == 0
