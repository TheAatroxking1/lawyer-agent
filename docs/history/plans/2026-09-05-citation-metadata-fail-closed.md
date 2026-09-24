# CitationGate 不完整日期与草案拒绝

依据总体规格 6.5、Phase 1 Task C1 的日期/状态完整性及 fail-closed 要求，修复已核实的元数据放行缺口；不增加法律语义推断。

- [ ] 先补参数化 RED：DRAFT；缺公布/生效日期；REPEALED/HISTORICAL 缺废止日期；生效晚于废止；任意状态携带已过废止日期。保留 Bundle/授权/unknown 状态的优先拒绝。
- [ ] 新稳定 reason：draft_not_effective、published_date_unknown、effective_date_unknown、end_date_unknown、effective_date_conflict。合法历史区间仍允许，禁止用 date.min 补未知生效日期。
- [ ] 已有 target_date > repealed_on 边界在本切片保持兼容并测试锁定；不把公布日晚于施行日一概当冲突。失效日是否包含属于现有数据字段的待生产复核语义，记录到 pending-production-reviews 对应法律质量项，不能宣称本轮已解决。
- [ ] Claim/QA代表性回归验证新 reason 形成稳定拒答、无引用或答案正文流出；前端如有 reason 字典需更新。保持正式审查与证据授权规则。
- [ ] 聚焦 pytest、API/contract 回归、Ruff/mypy、独立审查；文档记录既有CURRENT缺日期/草案等将从通过改为拒答，完整日期历史版本保持可用。

范围：application/evidence.py、tests/unit/test_evidence.py、必要 Claim/QA 测试、必要前端reason字典与测试；production review仅补字段语义复核，不写生产已验收。无需真实模型或外部法律检索，所有测试为合成数据。
