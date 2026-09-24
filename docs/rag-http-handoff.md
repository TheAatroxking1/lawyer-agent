# RAG内网接口联调说明

适用范围：两位开发者共享法律法规语料的内网联调。本服务独立于公网多租户业务API，只读`blog.lawyer_db`。不接收租户私有材料，不生成法律答案，不执行Agent任务。API版本1.0.0。

## 同事接入

当前地址：`http://192.168.31.14:8088`。先打开`/healthz`看到`{"status":"alive"}`，再访问`/docs`查看Swagger。`alive`只表示进程工作，认证后的`/readyz`核验Milvus字段/索引/加载状态；不调用百炼，因此不能用ready证明余额或模型权限正常。

只需交付本说明、`scripts/rag_client_example.py`和单独安全传递的RAG调用密钥。密钥存放在服务端`deploy/secrets/rag-api-key.txt`，不要将`milvus-query.json`里的百炼密钥、整个secrets目录或数据库凭据交给同事。Swagger中点Authorize后填RAG密钥，不加Bearer前缀。程序请求头是`Authorization: Bearer <RAG密钥>`。

同事将收到的RAG密钥保存为自己电脑上的`rag-api-key.txt`，与示例脚本同目录，执行：

```powershell
python -X utf8 rag_client_example.py "北京租房的时候，有什么需要注意的" --mode bm25
# 默认hybrid会在服务端调用一次百炼Embedding，不需要同事安装PyMilvus。
python -X utf8 rag_client_example.py "用人单位违法解除劳动合同，应如何支付赔偿金？"
```

后续地址变化使用`--base-url`或`RAG_BASE_URL`；密钥路径用`--key-file`或`RAG_API_KEY_FILE`。不要把密钥写进Git或命令行参数。现阶段为普通内网HTTP，不提供传输加密；超出可信内网使用范围时需要HTTPS/VPN。

## 接口契约

全部检索/读取接口需要Bearer认证。`/docs`、`/openapi.json`和`/healthz`不需要密钥；它们不返回语料或秘密。在线`/openapi.json`是完整机器契约。

### POST /api/v1/rag/search

```json
{
  "query": "北京租房的时候，有什么需要注意的",
  "mode": "hybrid",
  "top_k": 5,
  "document_ids": [],
  "raw_bm25": false,
  "max_chars": 20000
}
```

- query：1～8000字符，不能全空白。向量路保留问题内容，BM25默认保守清理套话。
- mode：hybrid/dense/bm25。hybrid固定官方RRF60，双路各100/候选50；bm25不调用Embedding。
- top_k：1～20；document_ids最多20个64位小写十六进制文书ID，过滤同时作用于两路。
- raw_bm25：true还原原问题BM25，用于对照。
- max_chars：1000～40000，默认20000，是所有返回证据正文的字符总预算，**不是token数**。不包含元数据大小。

返回`status`、`request_id`、`collection`、`corpus_version`、`mode`、`ranker`、`candidate_count`、`bm25_query`、`evidence`、`warnings`、`truncated`。candidate_count指融合/检索候选数，可能大于展示条数。

evidence每项含文书ID、分块ID、document_name、article_no、text、chunk_type、parent_chunk_id、model、rank及对应的fusion_score/retrieval_score；`evidence_id`与本次request_id绑定。两种分数不能直接比较，也不是正确率。

当前集合没有可核验的发布快照版本，corpus_version返回null；缺少权威来源URL、完整性及地域证明，不编造这些字段。`is_complete=null`表示原分块完整性未核验；超出字符预算时`text_truncated=true`且`is_complete=false`，响应truncated=true。不得把null当作完整。

### POST /api/v1/rag/read

```json
{
  "document_id": "实际64位文书ID",
  "chunk_id": "实际64位分块ID",
  "max_chars": 20000
}
```

document_id必填；chunk_id与article_no二选一。按条号读取时最多20个块，超过时标记truncated并给出article_chunk_limit_reached。结果是当前Milvus中的已存原文，**不意味着重新读取Word/PDF原件或自动恢复完整法律全文**。分块返回按ID稳定排序，不能当作原文顺序拼接。没有条号时用chunk_id；指定错误document_id不会返回其他文书的同一chunk。

如果首先命中标题，Agent可再调用search，限定document_ids并输入更具体的问题，然后read对应条文。后续的标题自动导航、地域核验和完整父条文恢复仍是独立工作。

### 空结果与错误

正常无结果返回200、status=empty、evidence=[]。不会用空结果掩盖上游故障。错误使用application/problem+json，包含status、code和request_id，不回显问题、凭据或供应商响应。

| HTTP | code | 同事处理 |
|---|---|---|
| 401 | unauthorized | 检查独立RAG密钥 |
| 408 | request_body_timeout | 请求体传输超过10秒 |
| 413 | request_too_large | 请求体超过64KiB |
| 422 | invalid_request | 对照OpenAPI检查参数；不接受collection/tenant_id等额外字段 |
| 429 | rate_limited/service_busy | 按Retry-After稍后重试，不立即循环重试 |
| 502 | embedding_access_denied/embedding_rate_limited/embedding_unavailable | 服务端核对百炼配置和可用性 |
| 503 | dependency_unavailable/dependency_response_invalid | 服务端检查Milvus/Schema及响应结构，不当作无相关法律 |

每分钟最多30次认证请求（包含ready/read/search），最多2个实际外部工作；限额按当前单进程共享密钥计算，不支持直接增加workers/副本来宣称全局限流。同步工作在线程中执行，客户端取消等待不会提前归还正在运行的名额。Milvus每次RPC15秒、Embedding网络等待30秒不等于整条请求硬截止；客户端示例等待75秒。LLM补检索次数和整个任务预算由Agent另行限制。

## 服务端运行与停止

从仓库根目录运行：

```powershell
# 仅首次；保留已有配置和密钥。换网络后手动更新现有env中的RAG_BIND_IP。
python -X utf8 scripts/prepare_rag_http.py --bind-ip 192.168.31.14
docker compose --env-file deploy/secrets/rag-http.env -f deploy/rag-http.compose.yaml up -d --build
docker compose --env-file deploy/secrets/rag-http.env -f deploy/rag-http.compose.yaml ps
docker compose --env-file deploy/secrets/rag-http.env -f deploy/rag-http.compose.yaml logs --tail 30
# 仅停止RAG，不停止Milvus，不删除卷。
docker compose --env-file deploy/secrets/rag-http.env -f deploy/rag-http.compose.yaml down
```

宿主绑定指定LAN IP:8088；容器监听0.0.0.0:8080，通过现有milvus网络连接standalone，不需要新增Milvus端口。镜像非root、只读、2 CPU/1GiB、无GPU。restart=unless-stopped只在Docker引擎运行时有效；电脑关机/休眠或Docker停止会中断同事调用。

Windows防火墙规则准备在`scripts/allow_rag_http.ps1`（UTF-8 BOM，兼容Windows PowerShell5）：

```powershell
# 管理员PowerShell运行；知道同事IP后将RemoteAddress缩小为该IP。
.\scripts\allow_rag_http.ps1 -LocalAddress 192.168.31.14 -RemoteAddress 192.168.31.0/24 -Port 8088
```

该脚本不修改系统防火墙Profile的启用状态。当前机器Public/Private防火墙原本关闭，因此不能把Allow规则当作已经生效的网段隔离；本服务仍要求独立密钥，且仅绑定指定内网网卡地址。用户现已确认同事电脑连通；该结论来自用户反馈，尚未单独报告同事端带认证的search/read调用结果。若同事无法连接，检查地址、路由/AP隔离、防火墙/安全软件及Docker端口发布。

## 验证与后续边界

```powershell
docker build --target test -t lawyer-milvus-query:test tools/milvus_query
docker run --rm --entrypoint python lawyer-milvus-query:test -m unittest discover -s tests -q
```

测试镜像安装httpx，正式runtime镜像不安装开发依赖。完整服务验证证据位于`artifacts/legal-corpus/rag-http/`。现阶段不是公网多租户生产服务；没有Agent回答、引用支持校验、最终Citation Gate、权限委托或法律人工复核。Agent应保存本次证据包并只引用其中允许的evidence_id；生成evidence_id本身不等于已经实现后端引用门禁。
