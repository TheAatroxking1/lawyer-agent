# 2026-09-10 DeepSeek API key JSON 配置底座（全栈首切片）

## 目标

用户转向「全栈最终实现」并明确：DeepSeek API key 由用户自己在 JSON
文件里配置（模板先留空），其余功能（对话/检索/前端等）由开发完成。
对话功能的第一步是**安全读取该 key**：提供一个用户可填写 JSON 模板
（真实文件不入库/Git），后端 `Settings` 从该 JSON 读取 DeepSeek key，
字段 `repr=False/exclude=True`（错误信息/日志/序列化永不泄露），为后续
DeepSeek chat provider（经 ModelGateway）提供配置底座。

## 范围（只做这些）

- 模板文件：仓库根新建 `deploy/deepseek.config.example.json`
  `{"api_key": ""}`（提交模板、空 key）；`.gitignore` 增加
  `deploy/deepseek.config.json`（用户复制模板填 key 的真实路径，绝不提交）。
- `backend/src/lawyer_agent/config.py`：
  - 新字段 `deepseek_api_key_file: Path | None`（`repr=False/exclude=True`，
    必须绝对路径、常规文件、有界读取、UTF-8、≤64KiB，复用既有安全文件
    读取纪律）；
  - 新字段 `deepseek_api_key: str | None`（`repr=False/exclude=True`，
    允许从环境变量 `LAWYER_DEEPSEEK_API_KEY` 或 JSON 文件提供，二选一，
    同时给出拒绝；空/空白视为未配置）；
  - JSON 解析严格：仅接受对象含 `api_key` 字符串，拒绝重复键/非对象/多余
    根键/非文本，错误信息不回显内容。
- 文档：`docs/history/plans/2026-09-10-deepseek-key-config-non-model.md`
  即本文件；README 或部署说明在切片内附简短用法。

## 明确不做

- 不实现 DeepSeek chat provider/网络调用（下一切片经 ModelGateway 接入）；
  不改 AI Job 运行时等用户延后项。

## 验证

- 单测（新 `tests/unit/test_deepseek_settings.py`）：文件缺失拒绝、
  非绝对路径拒绝、目录拒绝、超限拒绝、坏 JSON/缺 api_key/非字符串拒绝、
  file 与 env 双给拒绝、正常读取后字段存在但 repr/model_dump/error 不
  含 key、模板文件已提交且 api_key 为空。
- ruff/mypy 全量；全量 pytest（Redis/RabbitMQ 抖动按已知模式隔离重跑）；
  无 Secret；树干净（真实 key 文件被忽略）。
