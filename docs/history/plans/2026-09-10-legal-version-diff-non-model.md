# 2026-09-10 语料版本条文级差异（同一法规两版本 diff，非模型）

## 目标

2026-09-04 计划 Task A2 要求仓储「按版本号/时间点过滤；版本间差异查询
（候选关系不自动合并）」；spec 6.6 半自动更新第一步即「计算文件与条文级
差异」。当前只有导入与单版本读取，**没有同一法规两个版本之间的条文级 diff
代码**。本切片实现非模型 `LegalVersionDiffService`：给定同一 instrument 的
旧/新两个 version，按 `provision_no` 键与条文全文内容计算
added / removed / modified / unchanged 分组，绝不把跨版本条文自动合并成
新的 LegalInstrument（候选关系由上层决定）。

## 范围（只做这些）

- `application/legal_corpus_diff.py`：
  - `LegalVersionDiffError`（两版本非同一 instrument / 输入非法，稳定 message）；
  - `LegalVersionDiffQueryPort`（只读：`version_with_instrument(version_id)`、
    `provisions_for_version(version_id)`）；
  - `LegalVersionDiffService.diff(*, from_version_id, to_version_id)`：
    1. 校验两版本存在且 `instrument_id` 相同（否则抛错，绝不跨法规 diff）；
    2. 读两版本全部条文（均按 char_start 稳定有序）；
    3. 纯函数按 `provision_no` 分组：新增（仅新版）、删除（仅旧版）、
       修改（两版均含但 full_text 不同）、未变（两版一致）；
    4. 返回 `LegalVersionDiff`（instrument_id/两版本 id + 四组有序值，
       modified 携带 previous/current 两版 `Provision`；未变以新版为准）。
  - diff 只用内容与键比较：不写库、不改版本、不生成候选合并。
- 纯函数 `version_diff(old_provisions, new_provisions)` 独立可测；
  相同条文号不同版本（同 provision_no 不同全文）→ modified，绝不自动合并。
- 不接 HTTP/审计；不改 Schema；不加依赖。

## 明确不做

- 不做文件级（docx/二进制）diff；不做候选 LegalInstrument 关系或人工合并；
  不做 dataset_v2 生成/发布；不做跨租户/检索相关。
- diff 不比较结构路径/条号之外字段的展示差异（只分 added/removed/modified/
  unchanged，modified 提供前后全文供上层展示）。

## 验证

- 聚焦单测（纯离线 fake 端口）：新增/删除/修改/未变分组正确、顺序稳定、
  跨 instrument 抛错、版本缺失抛错、空条文安全、modified 带前后原文、
  同号不同文绝不合并。
- 真实 MySQL 集成：seed 同 instrument 两版本（含：仅旧版条文、仅新版条文、
  同号同文、同号异文）→ diff 四组断言；异 instrument 两版本 → 抛错；清理。
- 全量 pytest（Redis 抖动按已知模式隔离重跑）；ruff/mypy 零错误；无 Secret；
  树干净。
