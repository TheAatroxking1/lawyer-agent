# 有证据的延续条号发布检查

状态：在已批准全集发布与来源复核范围内实施；2026-09-07。

## 问题与证据

现有 `LegalCorpusQualityGate._sequence_break` 要求首条为一。三份原件实际为1993年第三至十一条、1999年第十二至十七条、2004年第十八至三十一条；只读原始 XML 与 Parser 输出一致。北京市人大转载的2018年全国人大修宪草案说明明确说明此前各次修正案的独立延续编号。

官方 HTML 已经 HTTPS 校验下载，位置 `artifacts/legal-corpus/full-release/constitutional-numbering-official-20260907-beijing/explanation.html`，SHA-256 `872c3393a3efa9ceee4d97478d79832595d66f37a84b8b8a052ba43807832254`。对应页面：<https://www.bjrd.gov.cn/zyfb/zt/13jqgrd1chybjthdztbd/wjbg1301dbt/202101/t20210129_2243318.html>。它证明三份旧修正案的编号范围，不代表逐字核对三份法规全文，亦不用于证明2018年草案就是最终文本。

## 设计选择

采用显式发布审核叠加在不可变完整导入报告上。单独记录经证据核对的编号范围，避免重写旧导入报告或将发布审核混入 Parser 身份。自动接受任意起始编号会掩盖正文缺失；按标题或来源哈希硬编码例外无法适应可审计版本管理，均不采用。

新增冻结领域值 `ArticleNumberingReview(first_article, last_article, review_ref, evidence_ref, evidence_sha256)`。范围严格为整数 `2 <= first_article <= last_article <= 9999`，不得接受 bool；审核引用非空且最多512字符；证据引用为无凭据、无 fragment 的 HTTPS URL，最多4096字符；摘要为64位小写十六进制。只支持连续普通条号，不支持补充条、引用型条号集或多段区间。

`ReleaseSelection.numbering_review` 为可选该值；仅 articles 模式允许，声明条文数必须等于区间长度。`ReleaseSourceSummary` 携带同样信息，审核记录进入质量摘要和持久发布候选的完整质量报告。没有审核记录时，历史质量 digest 保持一致，不生成新的空字段来改变既有审核身份。

发布质量检查对有审核的选择严格核对首尾、区间内每个条号、顺序、数量，不允许补充条。缺号、重复、倒序、区间外及非规范数字均拒绝。没有审核时原逻辑完整保留。来源/结构证明、条文全文哈希、分块图、元数据、日期政策、发布前审核摘要绑定等所有其它门禁不变。

## 清单与读取边界

现有 `legal-corpus-release-set-v1` 格式和原始报告保持原样。新增 v2，在原有 `schema_version/total/reports` 上必须提供非空 `numbering_reviews` 列表（最多256项）：

```json
{
  "version_id": "已入库UUIDv7",
  "source_sha256": "原件摘要",
  "input_sha256": "载体摘要",
  "structure_sha256": "结构摘要",
  "first_article": 3,
  "last_article": 11,
  "review_ref": "review:constitutional-numbering-1993",
  "evidence_ref": "https://official.example/evidence",
  "evidence_path": "C:\\absolute\\evidence.html",
  "evidence_sha256": "本地证据原始字节摘要"
}
```

读取时先执行既有完整报告、摘要、计数、重复来源与解析身份检查，再按版本ID精确查找选择，核对三个来源摘要、模式及计数，然后读取证据并验证摘要。证据路径复用既有绝对路径/设备/重解析点拒绝策略；必须为普通文件，读取最多4 MiB + 1，同一证据路径只读一次，累计证据最多16 MiB。无网络抓取、无外部来源文件读写、无目录递归。未知字段、重复JSON键、v1带审核字段、v2缺少/空审核、重复版本审核、未知版本或任一绑定错配都拒绝，错误不回显正文。读取 v2 原始字节摘要进入既有 selection_sha256，报告成员摘要继续独立持久保存。

单文件使用同一条集合发布入口，可将一个完整报告作为集合唯一成员。原始单文件发布入口保持严格默认，不为它增加第二套审核机制。这里的预检指 `corpus_publish_set --release-set ... --check`，不是原件导入预检。

## 验收与边界

合成回归先失败后实现：范围与类型验证、缺失/重号/补充号拒绝、默认缺首条仍拒绝、证据错配/更改/超限/不安全路径、清单错误、旧格式与旧digest不变、审核变化使digest变化且旧批准摘要无效；经过实际CLI装配的质量报告含审核记录，持久候选仍含记录。

只读实源验证三份旧修正案：原件/载体不变，默认检查仍拒绝，绑定证据后编号通过，全部其它质量结论如实保留；不更改法律效力/日期或正文。完整后端检查与独立审查通过后记录结果。此切片不切别名、不重导、不运行模型，不表示全集发布完成。

## 资源与工作树约束

保留当前脏工作树和所有旧报告，不提交。单一CPU向量任务 PID58004 继续运行，禁止修改其五项冻结 runtime 文件、加载第二模型或启动 OpenSearch；GPU计算暂停。此切片只变更发布检查代码、合成测试与对应文档。
