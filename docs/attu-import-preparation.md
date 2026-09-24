# Attu 导入文件准备

脚本：`scripts/prepare_attu_import.py`。使用 Python 3.12 或更高版本及标准库，无需安装 OpenAI、PyMilvus，不调用百炼、不使用 GPU、不连接数据库。

## 先准备什么

- 已有向量：`artifacts/legal-corpus/api-embeddings/qwen3.7-1024/*.json`。
- 原切块：`artifacts/legal-corpus/chunks/manifest.jsonl` 和各子目录的 `document.json`、`chunks.jsonl`。
- 运行转换期间保留输入，不移动、不更改，也不要继续改写向量结果。2026-09-15 用户授权将第一版产物迁移到 F 盘，目录对应关系和后续输出限制见[存储位置说明](corpus-storage-locations.md)；旧路径通过 Junction 读取。
- 当前目标 Collection 输入字段为 `chunk_id`、`vector`、`text`、`document_id`、`model`、`chunk_type`、`article_no`、`parent_chunk_id`。主键为 `chunk_id`，AutoID 关闭；`vector` 是 FloatVector/1024；`text` 是 VarChar/65535；其他字符串字段 VarChar/256；`article_no`、`parent_chunk_id` 允许为空。
- `sparse_vector` 是你已配置的 BM25 Function 输出，不写入导入文件。这里不会自动配置分词、索引或搜索。

## 启动命令

在项目根目录 `C:\Users\11639\Documents\ChatGPT\律师Agent` 的 PowerShell 运行。

先检查三份文档，不生成导入文件：

```powershell
python -X utf8 scripts/prepare_attu_import.py --limit 3 --check-only
```

生成三份文档的样本批次：

```powershell
python -X utf8 scripts/prepare_attu_import.py --limit 3
```

生成全集（默认每个 JSONL 最大 50,000,000 字节，约 50 MB）：

```powershell
python -X utf8 scripts/prepare_attu_import.py
```

只想完整检查一遍、不生成向量导入产物：

```powershell
python -X utf8 scripts/prepare_attu_import.py --check-only
```

默认输出根目录：

```text
C:\Users\11639\Documents\ChatGPT\律师Agent\artifacts\legal-corpus\attu-import
```

每次会建立独立的时间戳运行目录，终端打印实际绝对路径。已有运行不会被覆盖；不要反复执行并将各次输出都导入，以免重复插入同一批 ID。样本应导入单独的测试 Collection，或在目标集合导入全集前明确处理样本记录，不能依赖普通 insert 自动去重。

可以修改批次大小和输出位置：

```powershell
python -X utf8 scripts/prepare_attu_import.py --batch-mb 25 --output-root "E:\attu-import"
```

不要将输出指定到任何原件/输入目录或它们的父目录。单份输入默认最多 64 MiB；如报告 `input_file_too_large`，先核查是否确实为正常的大文档，再通过 `--max-file-mib 128` 调整。该上限限制单文件，不代表进程内存精确上限；JSON 解码还会产生 Python 对象开销。

## 输出怎么用

```text
某次运行目录/
├── report.json                 最终状态、范围、计数、批次大小与 SHA-256
├── errors.jsonl                错误清单；成功时为空
├── sources.jsonl               每份来源是否通过、块数及向量文件摘要
├── validation.sqlite3          本地全局 ID 去重表，不是 Milvus 数据库
├── provenance/                 不上传 Attu；保留追溯信息
│   ├── documents.jsonl         来源路径、来源摘要、版本等文档信息
│   └── chunks.jsonl            chunk_id、章节、段落和字符位置等
└── import/                     仅通过后出现；Attu 只选这里面的 JSONL
    ├── import_00001.jsonl
    ├── import_00002.jsonl
    └── ...
```

`report.json` 状态：

上传前要求最终报告为 `ready` 或 `sample_ready` **并且 `import` 目录实际存在**，两者缺一不可。最终报告先落盘、目录再发布；若在这两个步骤之间强制结束进程，可能留下 ready 报告但没有 import，仍不能视为可导入。磁盘故障导致最终报告无法写入时，终端明确提示失败，不能沿用旧报告判定成功。

| status | 含义 | 是否可以上传 |
| --- | --- | --- |
| `ready` | 全清单格式与来源校验通过，批次已生成并读回 | 可以；仍需真实 Attu 小样本验收 |
| `sample_ready` | 仅 `--limit` 指定样本通过 | 只用于样本测试，不是全集 |
| `checked` / `sample_checked` | 校验通过，没有生成导入文件 | 无文件可上传 |
| `failed` | 存在输入、输出或验证错误 | 不上传 |
| `interrupted` | 用户 Ctrl+C 正常中断 | 不上传 |
| `running` | 未完成；进程被强制结束时也可能保留此状态 | 不上传 |

失败/中断时，已写的临时数据保留在 `_staging_do_not_import`，不能上传；查看 `errors.jsonl` 排查。脚本不会跳过坏文档再把剩余数据冒充全集。为收集诊断，它通常会继续校验其他文档，最后返回失败。

本工具不做断点追加：修复问题后重新运行，会重新读取和转换，但不会重新算 embedding，也不会产生 API 费用。磁盘需容纳新输出及仍保留的旧运行；本工具不自动删除它们。

## 数据转换规则

- 保留所有原块，包括目前讨论的页码 `source_paragraph`；不改正文，不截断，不改 ID，不按标题合并法规，不重新归一化向量。
- `rows` 一项对应一条 JSONL。`model` 从外层复制，`document_id` 从 `document.document_id` 复制；`article_no` 和 `parent_chunk_id` 的空值写作 JSON `null`。
- 全局重复 `chunk_id` 拒绝，不静默去重。父块必须存在于同一文档，不接受悬空引用或父链循环。
- 对照源清单、源文档与切块原始字节摘要；逐条核对原切块字段和正文哈希、文档声明块数，验证 1024 个数值有限且适合 float32。检查每个字符串的 UTF-8 字节长度。
- 格式检查只能确认保存的向量数值和来源绑定，不能证明向量确实由声明模型/同一输入模板产生，也不能检验语义检索质量。若中途改过 embedding 输入拼接规则，需要另行核对运行记录。
- 仅数字数组转换成 JSONL；Python 解析/序列化保持数值，Milvus 的 FloatVector 在导入时按 float32 存储。
- 未上传的源元数据仍存在原始 JSON 和 `provenance/` 中；检索程序要另行实现通过 ID 查回，Milvus 不会自动读取本地文件。
- 未知法律日期/原始效力元数据保留原值，不用文件日期或模型输出补齐。本次格式通过不等于法律质量门禁通过，也不代表原项目全集数据库发布完成。

## 常见错误

| code（可能附行号） | 处理方法 |
| --- | --- |
| `missing_input_file` | 补齐对应来源的向量或原切块文件 |
| `unexpected_vector_file` | 核对是否把模板/其它模型结果误放在输入目录；保留原文件再使用正确目录 |
| `model_mismatch` / `dimension_mismatch` | 不要混入其它模型或维度的结果 |
| `chunks_hash_mismatch` / `source_row_mismatch` | 原切块与向量所对应的内容不一致，查清来源，不绕过检查 |
| `string_too_long:text` | 正文超过当前 Collection 65535 字节上限，不能直接截断 |
| `duplicate_chunk_id...` | 排查重复结果或错误 ID；程序不会自动保留某一条 |
| `input_changed_during_run` | 先停止改写输入，再重新执行转换 |

终端退出码：成功 `0`，校验/运行失败 `1`，参数或启动失败 `2`，正常 Ctrl+C `130`。每次输出目录中的报告是最终依据。
