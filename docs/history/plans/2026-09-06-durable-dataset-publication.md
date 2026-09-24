# S6b1 持久发布记录与保守恢复

依据总体规格 6.6 与 S6，补齐已确认的跨系统发布缺口。此切片不表示真实语料专业质量审核、模型验收或全产品完成。

## 协议与约束

- 构建候选索引和导航后，先提交 MySQL 发布记录，再请求 OpenSearch 切换别名。候选记录保存完整 manifest、质量指标、旧目标和新目标，保留历史记录。
- 状态为 `ready → switching → acknowledged → completed`。每次转换独立提交，CAS 防止两个恢复进程重复发送。每个别名只允许一个未完成记录，每个物理索引只允许一个记录。
- `switching` 在发送前持久化。超时、取消、确认丢失后保持该状态，禁止自动重发或仅凭一次 GET 就宣布完成。未决请求可能仍执行，自动释放占用不安全。
- `acknowledged` 表示已收到严格验证的成功响应；恢复只核对实际别名再把快照与 completed 在一个 MySQL 事务提交，不再次切换。目标冲突保持占用并返回稳定错误。
- 写别名前核对旧目标；协议要求所有应用发布经此入口，运维直接改别名属于冲突。不能把读取后写入描述为 OpenSearch CAS，也不使用会过期的租约。
- 保留已有用户改动、不提交、不触碰真实 alias/外部来源/共享 Docker。测试使用合成数据与专属隔离 MySQL。
- 单文件 CLI 接入持久发布；新增按发布 ID 查询/恢复 CLI。集合构建复用现有多版本服务，集合清单质量审核和全量发布另行验收。

## 实施与验证

- [x] 领域校验、发布状态机和故障测试：CAS 竞争、取消、未知结果、确认后快照失败、目标漂移。
- [x] migration14、持久仓储：独立事务、唯一活动别名、历史记录、快照原子完成；实际 MySQL 集成与迁移验证。
- [x] 分离集合 build 与切换，接入单文件发布和恢复 CLI；严格 OpenSearch alias 响应确认。
- [x] 独立复审，运行范围匹配的测试/Ruff/mypy，更新现状与恢复说明。

## 验证记录

2026-09-06 最终后端 `.venv/Scripts/python.exe -X utf8 -m pytest tests/unit tests/api tests/contract -q --tb=short`：**2070 passed / 1 skipped / 1 warning（22.41s）**。跳过为 Windows 符号链接，警告为 Starlette/httpx；全库 Ruff 通过，mypy 200 个源文件通过，`git diff --check` 通过。

专属临时 MySQL 8.4（127.0.0.1:13307）与 OpenSearch 3（127.0.0.1:19201）运行以下组合：**20 passed（31.94s）**：

```text
tests/integration/mysql/test_legal_dataset_publication_persistence.py
tests/integration/mysql/test_dataset_publication_recovery_e2e.py
tests/integration/mysql/test_corpus_batch_import.py
tests/integration/mysql/test_legal_source_proof_persistence.py
tests/integration/mysql/test_corpus_cli_hierarchical_onboarding.py
```

通过受控本地脚本注入临时管理员连接；每项使用随机隔离数据库，OpenSearch 使用随机候选和派生导航索引。两个真实跨系统试验以合成 DOCX 导入、合成向量构建集合，在切换确认/快照提交边界注入故障，不调用真实模型。恢复不再嵌入或切别名；未知结果持续阻止后续发布。migration14 升降级、历史降级拒绝与 `alembic command.check` 无漂移均验证。

开发中先观察到严格 alias 确认缺失的 16 项预期失败、GET 错误正文泄漏失败、UUID/版本范围拒绝失败，再实现修复。服务复验发现旧 source-proof 测试把受阻降级后的版本错误改为14；恢复为13并补注释：空的14可先降级，13的来源证明门禁随后阻止继续删除。最终组合全通过。

实现与独立复审均完成；额外补充 `status --alias` 覆盖数据库提交后、打印 ID 前的中断。临时 MySQL/OpenSearch 容器已按记录 ID、归属标签和 tmpfs 校验后停止移除，临时 MySQL 密码文件已删除。未操作共享 Compose 栈、真实别名、源文档或付费模型，未提交 Git。

后续仍需集合清单/质量门禁与真实模型验收，未决请求运维处置及完整历史回滚；S6b1 完成不等于这些项目完成。
