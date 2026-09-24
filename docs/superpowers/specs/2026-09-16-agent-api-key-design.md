# Agent API Key 集中加载

状态：加载器和 Provider 已实现，合同服务使用 qwen3.6-flash；现有 DeepSeek 功能保留。参考教程的集中填写方式，不在 Python 源码中写密钥，也不在导入模块时发请求。

## 唯一填写位置

真实文件为 `deploy/secrets/dashscope.json`，模板为 [dashscope.config.example.json](../../../deploy/dashscope.config.example.json)：

```json
{
  "DASHSCOPE_API_KEY": "在这里填写阿里云 Key",
  "LAWYER_DASHSCOPE_BASE_URL": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
  "LAWYER_DASHSCOPE_MODEL": "qwen3.6-flash"
}
```

本机已按用户提供的信息配置，并完成最小连通调用；无需重复填写。真实文件在 Git 忽略目录中，不能复制到模板、测试、日志或前端。

新环境在仓库根目录执行（不覆盖已有配置）：

```powershell
New-Item -ItemType Directory -Force -Path './deploy/secrets' | Out-Null
$keyConfigPath = Join-Path (Get-Location) 'deploy/secrets/dashscope.json'
if (-not (Test-Path -LiteralPath $keyConfigPath)) {
    Copy-Item -LiteralPath './deploy/dashscope.config.example.json' -Destination $keyConfigPath
}
notepad $keyConfigPath
```

填写后保存为 UTF-8。模型名和地址集中放在同一 JSON 中，便于维护；空 Key 会导致加载失败。

## Python 如何读取

[实际实现](../../../backend/src/lawyer_agent/infrastructure/providers/dashscope.py)合并配置加载与供应商适配，避免增加仅有一层转发的小文件：

```python
from pathlib import Path
from lawyer_agent.infrastructure.providers.dashscope import DashScopeSettings

settings = DashScopeSettings.load(
    Path("C:/absolute/project/deploy/secrets/dashscope.json")
)
# settings.api_key 是 SecretStr；不要输出它的真实值。
```

独立脚本也可设置 `LAWYER_DASHSCOPE_API_KEY_FILE` 绝对路径后调用 `DashScopeSettings.load()`。部署环境可用 `DASHSCOPE_API_KEY` 替代密钥文件，但不能同时配置两者；`LAWYER_DASHSCOPE_BASE_URL`、`LAWYER_DASHSCOPE_MODEL` 环境变量覆盖文件中的非秘密参数。

加载器拒绝相对路径、非法文件、超过 64 KiB、重复/额外字段、无效 JSON 和空 Key；凭据使用 SecretStr，错误不包含文件内容。只允许支持的阿里云 HTTPS 域名，HTTP 禁止重定向。运行时不弹出交互输入。

## 合同服务接入

网页服务还需要 [合同配置模板](../../../deploy/contract-review.config.example.json)：`dashscope_config_file` 指向上面的绝对路径。整个合同服务配置通过 `LAWYER_CONTRACT_REVIEW_CONFIG_FILE` 加载，详见 [运行说明](../../contract-review-core.md)。只填写模型 Key 不代表数据库、对象存储和 RAG 已就绪。

```text
合同服务配置 → DashScopeSettings → DashScopeChatProvider
→ 项目 ModelGateway → GatewayReviewModel → 合同 Workflow
```

Key 只存在服务端 Provider，不进入 MCP 参数、Prompt 或网页。当前阿里云适配器提供非流式 chat；用户可见的流式体验由后端 SSE 阶段通知实现，最后发送已通过门禁并保存的完整批注，不泄露内部推理。

## 已验证与边界

配置、超时、异常、响应大小、截断结果和调用计量均有合成测试。本机真实连通请求仅要求回答 OK，未使用客户合同；首次请求成功后本地统计代码出错，修正后第二次确认成功（第二次 23 输入 / 1 输出 token）。不将连通测试当作法律审阅质量或全部供应商功能验收。交互初始化与专用配置检查 CLI 尚未提供，使用模板与上述 Python 加载入口即可。
