# bg-unified-publisher：MCP 接口规范

本文描述当前代码的实际接口，不代表独立协议认证。与本文冲突时，以运行中的 `tools/list`、`server.py`、`core.py` 和 `adapters.py` 为准。

## 1. 协议与传输

- 服务名称：`bg-unified-publisher`。
- SDK：Python `mcp==1.26.0`，使用 FastMCP。
- 对客户端仅提供 **stdio**，消息使用 MCP 的 JSON-RPC 2.0，由 SDK 实现消息封装、生命周期、初始化及工具分发。
- 当前安装的 SDK 声明支持：`2024-11-05`、`2025-03-26`、`2025-06-18`、`2025-11-25`。实际会话版本以初始化协商为准。
- 官方规范页面在本次查询时提示最新版本为 `2026-07-28`；本项目没有验证该版本兼容性，不声明支持最新版全部能力。
- 初始化流程：客户端 `initialize` → 服务端返回协商结果和能力 → 客户端 `notifications/initialized` → `tools/list` / `tools/call`。
- 本项目注册五个 Tools，没有注册业务 Resources 或 Prompts，没有实现 Sampling、Elicitation 或 MCP Tasks 工作流。具体协商能力由 SDK 返回。
- 下游适配器支持 stdio、Streamable HTTP、SSE，不代表本服务开放 HTTP 服务端口。

参考：[MCP 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)、[官方当前规范](https://modelcontextprotocol.io/specification/latest)。

## 2. 机器可读契约

[mcp-tools.schema.json](mcp-tools.schema.json) 从实际 `build_server(...).list_tools()` 导出，包含五个工具的 `inputSchema` / `outputSchema` 和 SDK 支持的协议版本。

目前平台内容参数采用开放 `dict`，嵌套业务字段不是完整静态 JSON Schema；细节由 `core.check_content` 校验。不能仅根据顶层 Schema 认为任意 `content` 都合法。

当前未显式声明工具 annotations；宿主应按工具实际行为进行授权，尤其不能将发布或草稿更新当作只读操作。工具名称虽叫 `prepare_content`，实际不调用大模型进行改写。

## 3. 工具参数

| 工具 | 必填参数 | 可选参数 | 副作用 |
| --- | --- | --- | --- |
| `prepare_content` | `contents: object`, `action: string` | 无 | 仅本地组装与技术检查；可能检查本地图片是否存在 |
| `check_content` | `platform: string`, `content: object`, `action: string` | `operation="publish"` | 本地技术检查；可能检查本地文件是否存在 |
| `publish_content` | `contents: object`, `action: string`, `idempotency_key: string` | 无 | 写操作记录，访问上游，创建草稿或提交公开发布 |
| `update_content` | `platform`, `content`, `media_id`, `idempotency_key`, `action` | `index=0` | 更新公众号草稿并写记录 |
| `get_publish_status` | `platform`, `idempotency_key` | `verify_remote=false` | 读取本地记录；远端验证时获取令牌并查询微信 |

`platform` 的业务值为 `wechat` / `xiaohongshu`；`action` 为 `draft` / `public`。它们在顶层 Schema 中为字符串，非法值在业务校验阶段处理。

`contents` 是平台名到平台内容对象的映射。同次调用共享一个 action，不支持为每个平台分别设置 action。双平台依序执行，独立返回结果，不具备原子性或回滚。

## 4. 平台内容对象

### 小红书

仅支持 `action=public`、发布操作。

必填：`title: string`、`content: string`、`images: string[]`。
可选：`tags: string[]`。

- 标题按 Python 字符数最多 20；正文加标签按 `len(content) + sum(len(tag)+2)` 最多 1000。
- 图片数量 1–9；允许 HTTP(S) URL 或服务器本机存在的绝对文件路径（.png/.jpg/.jpeg/.webp）。
- 图片 URL 不在校验阶段下载，不验证远端尺寸或 MIME。
- 发布时由适配器加上 `visibility="公开可见"`。
- 不接受其他字段；没有定时、商品、原创或 AI 标识控件参数。

### 微信公众号创建草稿

`action=draft`。必填：`title`、`content`（HTML）、`image_url`（HTTP(S) 封面地址）。
可选：`author`、`digest`、`source_url`、`need_open_comment`、`thumb_media_id`。文本字段须为字符串，`need_open_comment` 只能为整数 0 或 1。

标题最多 64 字符；HTML 少于 20000 字符且 UTF-8 小于 1MB。HTML 检查活动标签、事件属性、部分危险 CSS 及标签配对，不是完整公众号排版校验器。正文图片必须为 `mmbiz.qpic.cn` URL。

### 微信公众号更新草稿

`action=draft`；`media_id` 为现有草稿 ID，`index` 为非负整数。内容需包含标题、HTML 和已有永久 `thumb_media_id`，不接受 `image_url` 自动上传。更新时将 `source_url` 转为官方 `content_source_url`。

应传完整目标字段，按微信官方更新语义处理未提供字段，本服务不先读取并合并旧文章。

### 微信公众号公开发布

`action=public` 时内容对象只接受 `draft_media_id: string`，不能同时携带标题或正文。创建草稿和公开发布是独立动作。

## 5. 调用与返回

JSON-RPC 示例（省略初始化过程）：

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "check_content",
    "arguments": {
      "platform": "wechat",
      "action": "public",
      "content": {"draft_media_id": "EXISTING_DRAFT_ID"}
    }
  }
}
```

工具返回 Python 字典，由 SDK 封装为 MCP `CallToolResult`。业务对象与 JSON-RPC 外层不能混用；客户端应读取 SDK 返回的结构化内容或对应文本内容。

检查结果业务对象：

```json
{"valid": true, "errors": [], "warnings": ["仅作技术检查，不保证平台审核、权限、素材有效性或发布成功"]}
```

发布聚合业务对象示意：

```json
{
  "state": "success",
  "platforms": {
    "wechat": {
      "state": "draft_created",
      "media_id": "DRAFT_ID",
      "remote_verified": false,
      "retry_allowed": false
    }
  }
}
```

`prepare_content` 返回 `contents`、`action`、按平台组织的 `checks` 和 `rewritten:false`。它不会删改敏感信息，也不应传入与文案无关的凭据。

## 6. 状态与错误

### 单平台写操作

| state | 含义 |
| --- | --- |
| `validation_failed` | 业务参数未通过检查，本平台不发送写请求 |
| `conflict` | 同一平台及幂等 key 已绑定不同参数 |
| `draft_created` | 上游确认草稿创建并返回 ID |
| `submitted` | 已获得提交回执，不代表审核通过或公开展示 |
| `updated` | 微信确认草稿更新，不代表逐字回读核验 |
| `failed` | 调用前拒绝或上游明确拒绝 |
| `unknown` | 请求结果不确定，或无法识别上游回执 |

重复 key 返回保存结果，并增加 `replayed:true`、`retry_allowed:false`。

聚合状态为 `success` / `partial_success` / `not_completed`；这里的 success 只代表所有平台返回 draft_created、submitted 或 updated 之一，不表示所有内容已公开。

业务错误通常作为普通工具结果返回，不设置 MCP `isError`。JSON-RPC、未知工具、顶层参数解析以及未捕获异常由 SDK 处理；不要把业务 `failed` 当作协议错误，也不要假定所有异常都有同一返回结构。

原始上游异常在发布器执行路径中不会返回或落盘，可能含令牌的日志被禁用。具体原因有意概括，因此排障能力有限。

### 状态查询

默认返回 `source="local_submission_record"`、`remote_verified:false`、`record`（不存在为 null）。`verify_remote=true` 仅支持微信公众号：

- 有 publish_id：查询 `freepublish/get`；映射 published、publishing、originality_failed、failed、review_rejected、deleted、blocked。
- 有 media_id：查询 `draft/get`；返回 draft_exists 和 article_count，`content_match_verified:false`。
- 远端查询成功时 `remote_verified:true` 仅表示这次状态查询有远端依据。
- 小红书返回 `remote_verification_unsupported_in_mvp`，不伪造笔记链接或审核状态。

## 7. 幂等与中断

SQLite 主键为 `[idempotency_key, platform]` 的哈希，指纹包含平台、操作、action、内容、media_id 和 index。key 长度 1–200。

写请求发送前预留记录，预留状态为 unknown。进程中断后重复相同请求会返回原记录而不重新发送。异常、失败均不自动重试；同一内容换一个 key 仍可能再次发布，所以不能用换 key 代替失败恢复。

只有共享同一 SQLite 数据库的实例共享去重记录。数据库丢失或另起数据库无法保证去重；它不提供平台全局 exactly-once 保证。

## 8. 凭据、权限与测试边界

服务读取 `--config` 中既有上游配置，不在接口参数中要求用户填写 AppSecret。数据库只保留请求指纹和精简结果。上游账号权限仍有效，例如微信发布权限不足无法由本服务解决。

客户端负责获得用户授权，尤其是 `public` 操作。当前没有服务端二次审批流程、OAuth、多租户权限隔离、工具 annotations、任务队列或 HTTP 服务端鉴权；仅面向受信任的本机客户端。

已完成：23 项离线测试；从实际 SDK 导出五个工具 Schema。尚未完成：本服务真实 stdio 初始化握手、跨客户端兼容性认证、两平台端到端发布验收和最新协议版本迁移。因此不能声称“完整符合所有最新版 MCP 要求”。
