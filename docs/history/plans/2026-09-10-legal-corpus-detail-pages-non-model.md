# 法规语料详情页（版本清单 + 条文全文）

- 日期：2026-09-10
- 类型：非模型切片
- 状态：已完成

## 动机

Vue 骨架切片只交付了语料**目录**（instruments 分页列表），目录行不可点，
法规版本元数据与条文正文无浏览入口。后端只读端点早已就绪
（`GET /api/v1/legal/instruments/{id}`、
`GET /api/v1/legal/instruments/{id}/versions`、
`GET /api/v1/legal/versions/{id}`、
`GET /api/v1/legal/versions/{id}/provisions`），本切片在 frontend/ 中补上
详情页把「目录 → 版本 → 条文全文」读链路走通。

## 范围

- `src/api/types.ts`：镜像 `LegalVersionSummary`（id/instrument_id/
  version_label/status/published_on/effective_on/repealed_on/law_number/
  source_ref/dataset_version/parser_version，日期与来源可空）与
  `ProvisionSummary`（id/version_id/provision_no/level/structure_path[]/
  title?/full_text）；并修正 `InstrumentSummary.region_code` 为可空
  （后端 `str | None`）。
- `src/api/endpoints.ts`：`getInstrument` / `listVersionsForInstrument` /
  `getVersion` / `listProvisionsForVersion`（id 一律 encodeURIComponent）。
- `src/lib/format.ts`：纯展示函数 `versionStatusLabel`（current→现行、
  repealed→已废止、historical→历史版本、status_unknown→状态未知、未知原样
  回退）与 `isoDate`（ISO 日期截取/空值安全）。
- `src/views/CorpusInstrumentView.vue`：详情页——法规身份头；版本胶囊
  （服务端 newest-first，默认选中首个）；选中版本元数据 dl 白名单展示；
  条文正文有序列表（title/条号/全文 pre-wrap）。并发拉取
  instrument+versions 后并行拉 version+provisions；错误统一 ErrorNote。
- 路由：`/corpus/:instrumentId`（corpus-instrument）；目录行改为指向详情的
  RouterLink。

## 验证

- `npm run typecheck` 0 错误；
- vitest 新增 7 项（endpoints 路径组 3 + format 4），合计 **25/25 绿**；
- `npm run build` 成功（详情页独立分包 4.0 kB gzip 1.8 kB）；
- 后端零改动。

## 后续

法规对话界面与 SSE、上传下载页面、Nginx 托管入 compose。
