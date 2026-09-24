# 内网RAG HTTP服务实施计划

用户已确认通过HTTP对接同内网同事的Agent，现要求执行服务封装。保留现有CLI与公网业务后端；本服务只读共享法律集合，不处理租户私有资料，不冒充公网多租户服务或法律答案发布。

**架构：** 同一独立查询镜像加入FastAPI/Uvicorn，由新Compose启动常驻服务。`GET /healthz`只测进程，认证后的`GET /readyz`检查Milvus；`POST /api/v1/rag/search`复用现有检索；`POST /api/v1/rag/read`按document_id配chunk_id或article_no读取已有原文。`/docs`和OpenAPI供同事联调。

**契约与限制：** Bearer独立调用密钥仅由秘密文件注入；不复用Embedding密钥。检索响应包含request_id、集合身份、evidence_id、document/chunk/name/article/text及warnings；不能核验全文时明确unknown，不把标题当条文、截断当全文，不编造来源URL/法律日期或语料发布版本。读取只支持明确ID/条号，不能无界下载整份语料。稳定脱敏错误、JSON Schema严格校验、64KiB请求体上限、单进程并发2个外部工作、每分钟30个认证请求；同步SDK在工作线程运行，实际结束前不释放并发容量。无额外GPU，单次证据预算以字符数明确表述，不能冒充token计数。

**部署：** 新`deploy/rag-http.compose.yaml`，服务监听0.0.0.0:8080，宿主默认8088；宿主发布IP用Git忽略env文件固定为当前LAN地址，不绑定所有网卡。凭据生成脚本不覆盖已有秘密。Windows防火墙限定同事IP或当前网段，不改变网络Profile、不放行Milvus。若权限不足保留可执行脚本并明确报告，不能宣称已可从同事电脑访问。

- [x] TDD：鉴权、验证脱敏、成功检索/原文、空态、异常、请求体/证据限制、并发占用与限流；补响应验证错误和未加载ready的RED→GREEN。
- [x] FastAPI契约及有界执行器，复用查询适配器，锁定新增依赖；37项容器测试、Ruff、8源码mypy及Compose通过。
- [x] Compose、秘密/内网配置准备、Windows窄范围防火墙脚本；UTF-8 BOM解决Windows PowerShell5中文解析，管理员规则已真实回读。原Public/Private防火墙关闭，未修改其启用状态，未宣称远端网段隔离生效。
- [x] 真实HTTP无鉴权/错Key/正确Key、BM25、hybrid、read与ready验证；最终同事客户端追加一次hybrid，总计2次Embedding，验证前后计数921117不变。部署时未从同事电脑实测；后续用户已反馈同事电脑连通，具体带认证业务调用结果尚未单独报告。
- [x] 导出OpenAPI、同事可运行的客户端示例和无秘密ZIP、使用/错误/限制说明；更新项目状态，保留证据，不提交Git。
