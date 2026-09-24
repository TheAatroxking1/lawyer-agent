# 记住登录与会话历史实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development；按独立后端认证、历史持久化、前端集成三个边界实施并复核。

**Goal:** 修复反复登录，交付可恢复、可继续的账号会话历史。

**Architecture:** 设备账号会话＋派生租户短令牌；MySQL 保存普通会话，现有合同运行表提供历史；Vue侧栏统一入口。保持现有两个Qwen调用和法律门禁。

**Tech Stack:** FastAPI、SQLAlchemy async、Alembic、Vue3、TypeScript、现有SSE。

## Global Constraints

- UTF-8；不提交秘密或客户材料；保留既有脏工作树，不自动提交。
- 私有历史要求tenant_id、creator_user_id双范围；未授权统一404。
- 10分钟Access Token、HttpOnly刷新Cookie、CSRF和撤销继续生效。
- 普通会话历史以数据库为准，保存实际生成状态；不伪造旧消息。
- 现有合同读取通过既有权限，不重复付费推理。

## Task 1: 后端设备登录与租户令牌

- [x] 复用 sessions.py、auth.py、会话repository；先测试过期自动轮换所需协议、选择租户不撤销账号、退出后派生令牌失效、授权版本变化拒绝。
- [x] 增加 `POST /api/v1/auth/tenant-access`，输入 `{tenant_id,membership_id}`，账号鉴权，返回 `{access_token}`。不修改刷新Cookie、不替代账号Token。
- [x] 持久Cookie和长期滚动刷新期限；更新会话有效期与Refresh期限在同一事务；保留重放检测、锁、用户/成员有效性核对。
- [x] API、应用单元和相关隔离MySQL验证；不改变旧switch-tenant契约。

## Task 2: 历史和普通对话持久化

- [x] 新增租户conversation/message模型及向前迁移，按tenant和creator限定repository，list按更新时间倒序并分页。
- [x] `GET/POST /tenants/{tenant_id}/conversations`；GET列表返回 `{items:[{id,title,updated_at}],next_cursor}`；POST `{title?}` 返回该summary。
- [x] `GET /tenants/{tenant_id}/conversations/{id}` 返回summary加`messages:[{id,role,content,status}]`。
- [x] `POST /tenants/{tenant_id}/conversations/{id}/messages/stream` 输入 `{content,request_id}`，只接收用户正文；从数据库组装上下文，持久实际回答。SSE沿用delta/error/done，done状态completed/incomplete；幂等且拒绝同一会话并发。
- [x] `GET /tenants/{tenant_id}/contract-reviews`返回 `{items:[{id,file_name,status,created_at,updated_at}],next_cursor}`，仅当前创建者；既有GET/下载恢复详情。
- [x] 同租户同创建者允许、跨租户/其他创建者拒绝；中断和失败保存真实状态，未完成正文不进入下轮上下文。

## Task 3: 前端续期与可用侧栏

- [x] auth集中续期模块，HttpOnly cookie＋可读CSRF头；Web Locks串行轮换，401只在未开始流前重试一次；缓存失效不清除历史事实。
- [x] session保存当前租户身份；tenant-access用于所有选择租户入口；刷新账号后重新派生租户令牌，账号/租户分别保存。
- [x] 客户端JSON、上传、下载和SSE复用续期fetch；长流中途失败不自动重复模型请求。
- [x] 侧栏真实历史分普通聊天/合同，稳定 `/chat?conversation=<id>` 与 `/chat?review=<id>&tenant=<id>` 路由；新对话清当前视图、保留列表。
- [x] 普通发送改为服务端持久接口；打开会话加载消息、继续发送；打开合同仅load+download。
- [x] 前端续期、并发、logout、侧栏和恢复DOM回归；typecheck/build。

## Task 4: 联调与交付

- [x] DB驱动修复：真实23页已保存7条草稿，来源/引用/两次调用独立核验通过；记录本轮用量。
- [x] 迁移隔离库验证后应用本地服务；登录/refresh/tenant-access/普通历史/合同历史/不同身份拒绝实际HTTP验证。
- [x] 更新project-status，独立复核、diff检查；实际浏览器可用则验证，不把API验收当视觉验收。
