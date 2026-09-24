# DOCX Loader 不可信 XML 与 ZIP 边界对齐

依据：已批准总体架构与 AGENTS 的不可信用户文件、只读来源、禁止执行宏及安全门禁要求。当前单文件导入使用的 `ZipDocxLoader` 只以原始字节子串拒绝 DTD/entity，且在读取正文 XML 前未校验解压大小；这与已完成离线导出的安全边界不一致。

本切片仅修改 `docx_loader.py` 及其测试，不改变正文、段落、style、换行或结构解析语义。不修改正在运行的转换、Worker、导出、核验、Reader 或 CLI；不运行 Word、真实语料、模型或服务。完整 Reader 语义统一留给 S6a。

1. 合成测试复现 UTF-16（含无 BOM）DTD/entity 绕过，以及压缩 XML 的读取前限额缺失；同时覆盖 ZIP 成员数与总解压量。
2. 使用 Expat 在解析结构前拒绝 DTD/entity，兼容原有合法 UTF-8/UTF-16 XML；保持稳定 InvalidDocx 异常边界。
3. 读取任何成员前检查最多 2048 成员、总展开最多 64 MiB；实际读取的正文 XML 最多 16 MiB。未读取图片不解压，仍计入总量；保留原有压缩输入 32 MiB 上限。
4. 运行 Loader 与相关导入准备/解析测试、Ruff、mypy，记录 RED/GREEN；独立复审后交接。新增限制会拒绝此前无边界的大型输入，这是安全修复的预期行为。

验收：恶意声明在所有测试编码中都抛 InvalidDocx，超限 ZIP 在 ZipFile.read 之前拒绝，合法中文与既有段落语义不变，图片仅受总量/成员数影响。
