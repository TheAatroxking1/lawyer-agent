# 受限旧 Word 静态文本恢复

## 问题与范围

全量受控 Word 转换处理 3,212 份候选，3,211 成功。厦门经济特区粮食安全保障规定原件在普通批处理、独立重试和原生 OpenAndRepair 中均于打开阶段遭 Office File Validation 拒绝（HRESULT 0x800A1897 / 6295）。宏、链接与文件校验设置不放宽，原件不改写。独立只读 CFB/FIB/CLX 诊断能解码该文件主正文 3,971 字符、74 非空段、连续 38 条；不能据此宣称完整格式有效。

用户已要求完成本范围全部文件切块；本增量仅补受控静态提取的明确入口，不执行文档代码、不访问外部链接、不进行数据库或索引发布。

## 实施与不变量

- 新增严格、有界的纯 Python 二进制 DOC 文本读取与派生 DOCX 写入适配器。仅支持已明确验证的 CFB/FIB/CLX UTF-16 结构；不识别的版本、压缩字符编码、加密、越界、链循环、交叉扇区、不支持的正文控制结构必须拒绝，不能猜测文本或静默降级。
- 主正文、已支持的辅助故事分开保留；字符控制、域代码/结果与段落边界显式处理。对未支持的非文本格式保留质量标记；不得把脚本、宏、OLE 对象、图片当作可执行内容或正文。实质正文/故事无法可靠恢复时拒绝。
- 通过现有转换边界保持原件路径、前后 SHA-256、派生哈希、适配器版本与代码/策略指纹，原子写入并校验派生 DOCX。静态结果使用独立版本标识，不能冒充 Microsoft Word 成功转换。
- 必须显式选择静态模式与来源文件；默认批量 Word 行为不变。不将 Word 失败自动静默转静态，不改变既有成功产物。
- 导出与核验识别该转换类型并保留 `legacy_binary_static_text_recovery`、`original_office_validation_failed` 及格式局限标记；缓存按实际转换记录/派生哈希核验。最终文件清单对该文件可见标记。
- 实际单文件采用前对照独立 CFB 诊断全文：逐段内容与顺序一致、条号序列一致、原件哈希不变。数量一致不能代替全文一致。辅助文本另行验证；主正文与辅助内容不得混合。

## 验证和交付

TDD 覆盖合法合成容器、中文全文、辅助文本、字段控制、非法链/长度/版本/编码/越界、输入输出隔离、静态模式显式选择、版本与质量标记传播。独立代码审查与真实单文件全文对照通过后，重新完整导出、独立核验、CSV 与候选元数据清单；仍有未处理来源则不声明全量完成。

依据：Microsoft [MS-DOC FIB](https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-doc/26fb6c06-4e5c-4778-ab4e-edbf26a545bb)、[Documents.Open Interop](https://learn.microsoft.com/en-us/dotnet/api/microsoft.office.interop.word.documents.open?view=word-pia)；异常报告位于忽略的 `.superpowers/sdd/`。Office 校验拒绝不等同恶意文档判定，也不代表静态恢复可保留原始版式。

## 已执行证据

实现为 `infrastructure/documents/binary_word_text.py`、显式单文件 `cli/corpus_static_extract.py`、转换记录 `recovery_reason` 和保留的 `recovery_origin.json`、导出/核验共享的类型化来源字段。Word 默认入口与安全配置不变。静态适配器独立 40 项测试通过；接线、指纹必填、格式/可见性局限、CSV 展示均复审批准。全后端检查 1,773 passed / 1 skipped / 1 warning，Ruff 全库与 mypy 188 个源文件通过。

真实恢复入口退出 0。独立比较脚本没有调用新二进制读取器，使用此前 CFB/FIB/CLX 审计脚本的原始文本，再单独处理已确认的三个非嵌套域：仅保留缓存结果、CR 分段、分页/换行符对应 LF、段落边缘空白去除。原件主故事 3,971 字符，移除空结果超链接域指令后 3,935 字符；74 个非空正文段、2 个非空页眉段逐段内容/顺序完全相同，38 条条号序列相同。隐藏/修订显示属性未解释，页码只保留已有缓存，不能认定恢复了原始显示版式。

原件 SHA-256 为 `9f1766018174b3be660b5c0af5ee3fa539e3b691769a8173c1bf4b6e0056f39a`，前后未变；派生 SHA-256 为 `ea57a0a4ac8808e47872a294d1e9ba01bfc14fe6d832c6cbdf89faa900d2ac09`。对照报告保存于忽略的 `artifacts/legal-corpus/static-recovery-verification.json`。累计 3,212 份转换候选已有成功记录，其中 3,211 为 Word 转换、1 为明确标记的静态恢复。随后最终全集导出及独立核验通过：16,006份/921,117块/0问题，complete=true；CSV已包含静态恢复与格式局限标记。
