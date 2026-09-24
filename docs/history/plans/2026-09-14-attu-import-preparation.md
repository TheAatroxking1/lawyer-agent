# Attu 导入前转换实施计划

用户已在本任务中批准：为现有 16,006 份 API 向量文件编写带中文注释的校验及格式转换脚本。只交付脚本、说明和验证，不替用户调用模型、导入 Milvus 或修改原始文件。

## 设计与边界

- 新增独立标准库脚本 `scripts/prepare_attu_import.py`，不依赖后端服务、GPU、API Key 或第三方安装。
- 输入为 `artifacts/legal-corpus/api-embeddings/qwen3.7-1024/*.json`；以 `artifacts/legal-corpus/chunks/manifest.jsonl` 为完整性依据，回读原始 `document.json` / `chunks.jsonl` 核对身份、摘要、正文、行数与父子关系。
- 固定当前目标：qwen3.7-text-embedding、1024 维；8 个导入字段为 vector/model/chunk_id/text/document_id/chunk_type/article_no/parent_chunk_id。文本最大 65535 UTF-8 字节，其余字符串 256；非空正文、有效浮点数、全局 ID 去重。
- 保留页码块，不改正文、不截断、不重新生成 ID、不重新计算向量。原始元数据写入独立追溯文件；它们不上传 Attu。
- 每次建立新运行目录；流式写出约 50 MB 一批 JSONL，不累计全集向量。单份输入大小有界，全局 ID 使用 SQLite。仅在范围内全部核验通过且读回输出正确后，将暂存批次目录改名为可导入目录；失败或中断保留诊断，不宣称完成。
- `--check-only` 只检查并写报告；`--limit N` 明确标记为样本，不冒充全集；默认遍历完整清单，缺失、额外输入及重复均记录。
- 不替换现有 OpenSearch 发布架构。离线 ID 与数据库 ID 仍不同；原来的法律质量阻断不因格式通过而解除。

## 实施与验证

1. [x] 写合成数据测试：字段映射、分批边界、空值/中文、1024维、缺文件、超长、NaN/布尔、重复ID、来源漂移、父子关系、失败/中断不发布、输出隔离、样本范围及校验模式。已观察缺少脚本的预期失败。
2. [x] 实现独立 CLI，中文注释说明参数、校验、SQLite 去重、暂存发布与错误报告；测试转绿。
3. [x] 运行 Ruff、类型检查及脚本测试。真实输入只做小范围格式转换/预检，并记录 16,006 文件完整性盘点；不生成全部 20 GiB 转换产物。
4. [x] 独立代码复审通过，三项失败终态/追溯问题已补回归并修复；更新用户说明和 `docs/project-status.md`。

验证命令（仓库根目录）：

```powershell
python -X utf8 -m unittest discover -s scripts/tests -p test_prepare_attu_import.py -v
backend/.venv/Scripts/ruff.exe check scripts/prepare_attu_import.py scripts/tests/test_prepare_attu_import.py
backend/.venv/Scripts/python.exe -m mypy --strict scripts/prepare_attu_import.py
python -X utf8 scripts/prepare_attu_import.py --limit 3 --check-only
```

现有用户脏工作树全部保留，不创建提交；进度以本次运行证据更新。

最终证据：30项合成测试通过；Ruff按backend配置及严格mypy通过；真实3份/1129块sample_checked、sample_ready；1 MB测试分批26文件逐条回读与源向量独立比较通过，脚本SHA与报告一致。独立审查再次验证fsync/最终报告写入故障、报告中断、目录改名后中断及身份冲突，三项问题关闭。全集内容核验、全集转换、真实Attu导入未运行。
