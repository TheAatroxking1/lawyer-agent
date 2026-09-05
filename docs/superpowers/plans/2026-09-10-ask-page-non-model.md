# 前端检索问答页（有据问答：结论 + 权威引用卡片 + 安全拒答）

- 日期：2026-09-10
- 类型：非模型切片（纯前端；后端 `/legal/questions` 已真实装配）
- 状态：已完成

## 动机

后端检索问答（HTTP + SSE）已就绪并真实装配，但无前端面。本切片在
frontend/ 新增「法规问答（有据）」页：输入问题 → 提交 `/legal/questions`
→ 渲染结论文本与**权威引用卡片**（法规/版本/条号/条文全文/来源/数据集）；
引用未通过核验时展示**安全拒答**（reason + 固定文案）；前置缺失时给出
503 族引导，绝不把不可用伪装成回答。

## 范围

- `types.ts`：`RetrievalCitation`（evidence_id/instrument_title/
  version_label/provision_no/provision_text/source_ref/dataset_version，镜像
  后端七字段白名单）与 `RetrievalQuestionReply`（refused/reason/text/usage?/
  citations[]）、`RetrievalQuestionInput`（question/alias?/target_date?）。
- `endpoints.ts`：`askQuestion(client, input)` → POST `/legal/questions`。
- `views/AskView.vue`（路由 `/ask`，name=ask，公开页）：输入（Enter 发送）→
  游客点击提交弹 AuthModal（复用登录三页签），登录成功自动补发该问题；
  结果区：busy 提示 → 拒答卡片（reason 可读 + 文案）/ 支持回答文本 +
  引用列表（标题行/全文/来源数据集）+ usage；错误横幅 code/title + 引导
  （retrieval_qa_unavailable / legal_dataset_not_published /
  model_provider_unavailable / timeout）；「重新提问」。
- `guard.ts`：ask 加入 AUTH_FREE（游客可浏览输入）；`AppShell` 导航加
  「法规问答」。

## 验证

- `npm run typecheck` 0 错误；vitest **31/31 绿**（新增 askQuestion 端点
  body/路径断言）；`npm run build` 成功（AskView gzip 2.4 kB）。
- 真实回答出结果需 key + OS 在线 + dataset_v1 已发布（后端 503/拒答语义已
  全栈覆盖，UI 路径与 /chat 同构）。

## 后续

AskView 接 SSE `/legal/questions/stream`（fetch 流解析 started/answer/error/
done）展示「检索中→出文」阶段；对话页与问答页按用途分工；上传下载、MCP
工具、Nginx 入 compose。
