# 文书名称映射 Implementation Plan

**Goal:** 为现有Milvus语料建立可查询的document_id到文书显示名称映射。

**Architecture:** 标准库独立工具从已验收的转换追溯文件构建JSON映射；按来源清单和导入报告绑定验证，原子发布。查询类为后续本地RAG提供lookup/find/enrich入口。

**Tech Stack:** Python 3.12、UTF-8 JSON/JSONL、unittest。

用户已确认设计方向，按当前任务直接执行。保留脏工作树，不提交Git，不改原件、向量或Milvus Schema。

- [x] 创建 `scripts/tests/test_document_name_map.py`：临时合成完整报告/manifest/provenance，验证中文日期保留、重复ID、同名、来源冲突、缺失覆盖、坏报告、文件篡改、未知ID、回填不修改输入、重跑不覆盖。运行 `python -X utf8 -m unittest discover -s scripts/tests -p test_document_name_map.py -q`，确认功能缺失导致失败。
- [x] 创建 `scripts/build_document_name_map.py`：`build(report_path: Path) -> Path`、`DocumentNameMap(directory: Path)`，实现 `lookup(document_id: str) -> str`、`find(name: str) -> list[dict[str, str]]`、`enrich(hits: list[dict[str, Any]]) -> list[dict[str, Any]]`。命令行提供build/lookup/find；输出固定在来源报告同级document-names目录。
- [x] 合成测试通过后运行backend配置Ruff和严格mypy；修复本次问题，不改正在使用的导入器和转换器。
- [x] 执行真实build；逐项对照16,006条映射与追溯文件，核对块数921,117、同名统计、来源SHA以及重跑不改变文件摘要；实际查询Milvus后enrich验证正确名称。
- [x] 更新 `docs/document-name-map.md`、本计划与 `docs/project-status.md`，明确实际文件位置、Python用法和未接入Attu字段/应用问答的边界。

实际完成：15项合成测试、Ruff、严格mypy通过；16,006条映射逐项核验，2组同名保留ID，前/中/后3个真实Milvus命中回填通过。输出与证据路径见docs/document-name-map.md。未修改数据库或向量。
