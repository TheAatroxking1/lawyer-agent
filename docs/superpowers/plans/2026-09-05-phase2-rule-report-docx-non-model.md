# 阶段 2 增补：Rule Check DOCX 报告导出 Implementation Plan（非模型）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已交付的 Rule Pack/RuleEngine/RiskIssue 处置链路之上，把某受管 Document Version 的规则检查结果（含律师处置状态）导出为**受控 DOCX 工作产物**：用 stdlib `zipfile+xml` 只写不改，不解析不可信内容、不执行宏；产物可被既有只读 `ZipDocxLoader` 读回并自验证。这是「Drafting/导出」延后项中可独立验收的非模型先行切片。

**Scope / 边界（明确延后）：**
- 不调用生成模型/Embedding；不做 RAG/OpenSearch；不做 PDF、正式 `LegalReport` 发布/审批/版本化、模板变量引擎。
- 导出内容**只来自已入库数据**（Document 元数据、RiskIssue 及处置记录），绝不把不可信文档文本当指令执行。
- 产物为「工作产物/草稿」：不构成对外法律意见，不自动给出任何时限/效力结论；未处置或驳回的 Issue 不得被呈现为已确认结论。
- 产物只作为受控字节返回；派生 DocumentVersion 落库、对象存储持久化、下载 API、审计登记均为后续切片。

**Architecture:** MySQL 是 RiskIssue/处置的事实源。报告组装在应用层完成：读取租户 Document 元数据 + 该文档全部 RiskIssue（仓库已按 tenant 过滤），按受控模板生成「标题/免责声明/风险清单/处置记录」段；`ZipDocxReportWriter` 把这些段写成 `.docx` 字节（stdlib 写 ZIP 与 OOXML，文本做 XML 转义，无 DOCTYPE/ENTITY/外部关系）。每次导出即时用 `ZipDocxLoader` 读回校验内容一致，形成写→读闭环。

**Tech Stack:** Python 3.12、stdlib `zipfile`/`xml`、现有 FastAPI/SQLAlchemy 栈、pytest、Ruff、mypy。无新第三方依赖、无新外部服务。

## Global Constraints

- 全部代码/测试/文档 UTF-8；仅中国大陆法律/法律服务场景；本切片不产生对外法律结论。
- 租户隔离为硬边界：导出读取全部经显式 TenantContext/租户参数过滤；必须有跨租户反向测试。
- 报告免责声明固定包含「规则候选提示，非法律意见，需律师/法务人工核验」；高风险与时限一律不自动断言。
- 写 DOCX 只使用 stdlib，禁止解析不可信 XML 内容；文本一律转义；不写宏/外部链接。
- 使用 TDD；不加新 Migration（无 Schema 变更）；不改已发布 Migration。
- 不提交 Secret/.env/原件；日志脱敏。

## File Map

```text
backend/
  src/lawyer_agent/
    infrastructure/documents/docx_writer.py   # ZipDocxReportWriter（stdlib 写 docx）
    application/report_export.py              # RiskReportService：组装并导出报告字节
  tests/
    unit/test_docx_writer.py                  # 写→ZipDocxLoader 读回闭环、转义、防注入
    unit/test_report_export.py                # 免责声明/清单/处置状态/跨租户数据不泄漏（fake store）
    integration/mysql/test_rule_report_export.py  # seed→run checks→dispose→导出→读回+跨租户反向
```

## 里程碑 A：DOCX Writer（只写）

### Task A1: stdlib DOCX writer 写→读闭环
- `ZipDocxReportWriter.write(paragraphs) -> bytes`：段落（普通/标题/编号列表）→ docx 字节；文本 XML 转义；不含 DOCTYPE/ENTITY/外部关系。
- 单测：写出的字节用 `ZipDocxLoader` 读回，段落文本/顺序一致；含 `<`、`&`、`"`、换行等特殊字符文本往返无损；危险字符不破坏 XML。
- Commit: `feat: write read-only round-trip docx reports`

### Task A2: writer 输入受控与注入防御
- 输入为强类型段结构（标题/正文/风险行），不暴露自由 XML；非法控制字符/超长文本被拒绝或规范化。
- 单测：控制字符拒绝、超长截断、段类型白名单。
- Commit: `feat: constrain docx report inputs`

## 里程碑 B：报告组装服务

### Task B1: RiskReportService 组装
- `export(context|tenant, document_id)`：经 Document 仓储取元数据、经 Rule Pack 仓储取该文档全部 RiskIssue（含处置）；组装：标题（文档名/检查时间）、免责声明、风险清单行（条文号、命中文本、风险级别、证据级别 rule_based、状态、处置理由/时间）。
- 规则：仅显示 rule_based；`open` 标「待人工核验」；`accepted/rejected/modified` 附处置理由；`stale` 标「已随文本变更失效」。
- 单测（fake store）：免责声明必含且前置、清单排序稳定、处置字段齐全、无命中时输出「未发现规则命中（仍需人工核验）」而非「无风险」。
- Commit: `feat: assemble rule check export reports`

### Task B2: 跨租户导出隔离
- 导出路径全部走租户范围仓库读取；无裸 `get_by_id`；跨租户 document 读取返回空/拒导。
- 单测 + 后续 MySQL 集成反向测试兜底。
- Commit: `feat: scope report export per tenant`

## 里程碑 C：MySQL 集成与门禁

### Task C1: MySQL 集成流
- `test_rule_report_export.py`：seed 租户/matter/document + 激活 pack/规则 → docx 解析 → `RuleCheckService.run_checks` 命中入库 → 律师 dispose → `RiskReportService.export` 得到 docx 字节 → `ZipDocxLoader` 读回断言标题/免责/命中文本/处置状态存在；A 租户导出请求传 B 租户 document_id 不得带出 B 数据。
- Commit: `feat: export rule check docx over real mysql`

### Task C2: 全量门禁收尾
- 全量 pytest（真实 MySQL）、ruff、mypy、secret 扫描、AGENTS.md 阶段说明更新。
- Commit: `docs: complete rule report export slice`

## Plan Self-Review
- 规格 8.2-7/8.3「导出审查报告」后端先行、非模型部分；PDF/正式 LegalReport/Drafting 模板/派生版本落库/下载 API 均延后标注。
- Security：租户过滤、不可信文本不当指令、stdlib 只写无解析面、免责与人工核验门槛、跨租户反向测试。
- 闭环：writer 写出的 docx 由既有只读 loader 读回自验证，真实 MySQL 流覆盖 seed→检查→处置→导出。

## Exit Criteria
本切片通过里程碑 C 门禁后才完成：真实 MySQL 集成、DOCX 写→读往返、跨租户反向、免责声明与人工核验断言、ruff/mypy 零错误、无 Secret、工作树干净。生成模型/法规 RAG/PDF/正式发布为明确未验证项。
