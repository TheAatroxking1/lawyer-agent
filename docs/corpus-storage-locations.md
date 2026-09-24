# 第一版语料产物的磁盘位置

用户于 2026-09-15 明确授权将项目的大体积切块、向量和导入前清理产物迁移到 F 盘，释放 C 盘空间。以下是派生产物目录；`F:\ai律师数据库` 中其他法律原件继续只读。本次不迁移模型权重、虚拟环境、Docker 数据卷或 Milvus 集合，不重新计算向量、不重新导入。

## 目录对应关系

| 内容 | F 盘实际位置 | 项目中的兼容入口 |
| --- | --- | --- |
| 切块、文档元数据、清单和核验记录 | `F:\ai律师数据库\切块第一版\chunks` | `artifacts/legal-corpus/chunks` |
| 百炼 API 向量，含 `qwen3.7-1024` 子目录 | `F:\ai律师数据库\向量化第一版\api-embeddings` | `artifacts/legal-corpus/api-embeddings` |
| 旧本地 Embedding 缓存，保留原模型身份目录 | `F:\ai律师数据库\向量化第一版\vectors` | `artifacts/legal-corpus/vectors` |
| Attu 导入文件、追溯数据、导入及名称回填记录 | `F:\ai律师数据库\导入前清理第一版\attu-import` | `artifacts/legal-corpus/attu-import` |

迁移成功后，兼容入口是 Windows NTFS 目录联接（Junction），不是另一份实体数据。读取旧 C 盘路径会访问 F 盘内容；从旧路径修改或删除里面的文件，也会修改或删除 F 盘实体。F 盘必须保持可用且盘符不变。不能因为资源管理器通过两个路径都能看到文件，就把其中一边的内容当成重复副本清理。

Attu 全集导入文件位于：

```text
F:\ai律师数据库\导入前清理第一版\attu-import\20260914T142608Z-7c17c736\import
```

本次仅改变存储位置。已有 Milvus 数据不需要重导。RAG HTTP 服务通过 Milvus 读取文本、名称和向量，其 Compose 没有挂载上述四个离线目录。

## 校验与迁移记录

记录目录：`artifacts/legal-corpus/storage-migration/`。

- `report.json`：整个迁移的最终状态、目录计数、字节数与 C 盘前后可用空间。必须检查实际文件和 `status`，不能仅凭本文判断完成。
- `*-state.json`：各目录复制、校验、删除原副本和完成的阶段状态。
- `*-sha256.jsonl`：逐文件相对路径、字节数和 SHA-256；生成前已核对源文件与目标文件摘要相同。
- `*-robocopy.log`：复制日志。
- `migrate-first-edition.ps1`：本次固定路径迁移脚本；默认只预检，加 `-Execute` 才执行。目标已存在会拒绝，因此不要将它当作日常同步或盲目恢复脚本重复运行。
- `vectors-interrupted-state.json` 与 `vectors-sha256-interrupted.jsonl`：旧缓存的初次校验因逐文件路径处理过慢而主动暂停，保留当时记录。随后 `verify-vectors-fast.py` 使用最多 4 个工作线程、256 个待处理任务重新核对全部缓存文件；通过后才由 `finish-vectors.ps1` 清理 C 盘副本。不是从部分成功记录跳过余下核验。
- `final-verification.json`：迁移后的独立复查，包括全部目标文件计数/大小、摘要清单身份、旧路径读回样本、备份不存在以及 HTTP 健康检查。

操作顺序是复制、逐文件双端 SHA-256 校验、复查目录集合与源文件元数据、将 C 盘原目录暂存为已验证备份、建立并核验 Junction，最后仅删除白名单内的 C 盘备份。任何失败均不覆盖 F 盘既有目标，也不把未校验副本当作完成。

## 后续脚本的路径边界

历史报告中的路径、哈希、ID 和内容不改写，继续利用 Junction 读取。该操作不代表历史批次重新验收。

`prepare_attu_import.py` 和 `build_document_name_map.py` 目前仍有“禁止向整个 `F:\ai律师数据库` 输出”的保护；它们调用 `resolve()` 后能识别 Junction 的真实目标。因此旧产物的读取正常，但重新生成清洗批次或重新构建名称映射时，仍可能返回 `output_is_readonly_source`。本次不扩大这些脚本的写入权限；后续需要重跑时，为新批次设置位于该原件根目录之外的独立输出目录，或通过单独变更仅允许本次指定的派生目录。不得关闭整个原件目录的保护。

Docker/WSL 若以后需要挂载这些离线文件，应显式使用容器可访问的 F 盘实际路径并验证挂载；宿主机 Junction 可读不等于容器跨盘挂载已验收。
