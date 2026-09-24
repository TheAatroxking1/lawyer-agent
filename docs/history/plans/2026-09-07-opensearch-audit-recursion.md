# OpenSearch 自审计循环修复实施计划

> 执行使用 subagent-driven-development：独立配置实现与任务复审；真实服务验证由根代理负责。用户已授权持续推进及资源优化，在现有审计/隐私边界内完成修复。

**目标：** 阻止本机 OpenSearch 审计写入递归，同时保留业务 Bulk 审计并停止记录请求正文。

**架构：** 保持镜像、卷、索引、别名和内部审计存储。使用当前固定镜像 3.8.0 所带 Security 3.8.0.0 的逐项 Bulk 解析路径，其中 logBulkItem 已实现审计索引前缀排除；设置同时写入 Compose 和现有集群的 persistent 设置，核对 transient 无反向覆盖。

**技术：** Docker Compose、OpenSearch cluster settings API、PowerShell 白名单采样、pytest 配置契约。

## 约束

- GPU 计算继续暂停；真实 OpenSearch 验证与真实模型验证串行，避免同时增加内存压力。
- 不关闭全部审计、REQUEST_AUDIT 或业务 Bulk 审计，不删除历史审计索引，不更改正式别名，不重建数据卷。
- 不存储或输出审计正文、请求头或身份；验证正文缺失只使用 exists 计数，命中的 _source 仅取类型、动作、索引等白名单字段。
- 当前服务仅绑定 loopback，现有开发 security.disabled 状态不扩散到生产配置。
- 所有现有脏工作树保留，不提交。配置回滚恢复本次两个准确键的原值，不清空其它集群设置。

## Task 1：部署配置与契约

文件：`deploy/compose.yaml`、`backend/tests/unit/test_compose_contract.py`。实现前保存当前两文件快照，生成任务独立 diff；不使用 HEAD 差异代表本任务。

- [x] 先新增契约测试，要求 OpenSearch environment 含 `plugins.security.audit.config.resolve_bulk_requests: "true"` 和 `plugins.security.audit.config.log_request_body: "false"`；不允许该服务配置整体 audit.enabled=false、REQUEST_AUDIT 禁用或 Bulk ignore。先观察失败。
- [x] 仅加入两个准确环境键及必要说明，保持其它服务配置。
- [x] 聚焦 compose 契约与 Ruff 通过，`docker compose --env-file deploy/.env -f deploy/compose.yaml config --quiet` 通过；不输出插值秘密。
- [x] 独立复审达到 spec/quality 双通过。

## Task 2：原实例动态设置与有限验收

输入证据 `.superpowers/sdd/opensearch-self-audit-evidence-20260907.md` 和 `artifacts/legal-corpus/full-release/opensearch-audit-metadata-sample-20260907.json`。准确实现来源链接保存在证据中。

- [x] 核对原容器 ID、项目、卷以及无模型进程；启动原容器，最多 20 次有界 HTTP 就绪检查，再有界等待分片恢复至 yellow/green。保存相关 persistent/transient/default 两键及全部别名，不读取其它敏感配置。
- [x] persistent 设置逐项解析 true、正文 false。3.8 将这两个键标为 Sensitive，连 transient=null 也拒绝；先核对当前 transient 两键均无值，仅更新 persistent。若出现旧 transient 覆盖先停止并调查，不提交该版本明确拒绝的 transient 更新。回读验证。
- [x] 记录稳定间隔的审计索引 indexing 计数与自身 Bulk 审计计数；后台刷新延迟及在途审计不能误判修复失败或成功。只统计新时间窗，保留旧记录。
- [x] 创建唯一命名、单主分片/零副本的合成诊断索引并写两条 Bulk。验证业务文档计数 2、对应逐项 REQUEST_AUDIT 存在、正文 exists 计数 0。只删除本次明确创建的诊断索引；保留审计证据。
- [ ] 比较正式别名保持一致；记录资源采样与容器状态。验证失败时恢复准确旧设置并正常停止原容器；成功后也先正常停止，供后续真实向量验证独占资源。
- [x] 更新项目状态和运行说明，明确有限验证范围，不将其等同于全集发布或 GPU 硬件修复。

Task1 报告与快照 `.superpowers/sdd/opensearch-audit-config-*`：RED缺键失败，最终13 passed、Ruff、Compose quiet、独立 spec/quality PASS。Task2 最终报告 `artifacts/legal-corpus/full-release/opensearch-audit-repair-20260906t173706z.json`：persistent回读true/false、transient无值；修复前30秒自身Bulk=86、修复后窗口=0；2文档/4业务审计/0正文；探针删除、原别名一致、容器停止。初次173316与173554设置失败（Sensitive键放transient=null而HTTP400）；173443探针在分片恢复前无法取得字段能力，均已记录并正常停止，无设置生效。

剩余资源观察：本次审计 indexing_total 1068→3108，可能仍有在途写入，但本次没有确认其具体构成，不能将自己Bulk=0当作全部写入已静止。容器峰值内存/CPU也未重测。先保持OpenSearch停止供CPU向量接续，下次独占资源启动时补做持续时间窗和资源采样；上述未勾选项中的别名与关闭部分已完成。
