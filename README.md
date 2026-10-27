# bg-unified-publisher

统一微信公众号与小红书图文发布的 MCP 服务。使用 Python MCP SDK，通过 stdio 向 AI 客户端提供内容技术检查、发布、公众号草稿更新和状态查询。

> 当前为 MVP。复用已有公众号和小红书 MCP 服务，不内置平台登录。已通过 23 项离线测试，尚未完成本服务真实端到端发布验收。技术校验不等于平台合规审核。

## 能做什么

| 功能 | 微信公众号 | 小红书 |
| --- | --- | --- |
| 内容技术检查 | 标题、HTML、图片地址 | 标题、正文、标签、图片 |
| 创建草稿 | 支持 | 不支持 |
| 公开发布 | 提交已有草稿，受账号权限限制 | 提交图文笔记 |
| 更新内容 | 仅更新已有草稿 | 不支持 |
| 查询远端状态 | 支持草稿存在性及发布状态 | 暂不支持 |

提供五个 MCP 工具：

- `prepare_content`：组装已写好的平台文案并校验，不自动改写。
- `check_content`：检查单个平台的内容格式。
- `publish_content`：执行草稿创建或公开发布。
- `update_content`：更新公众号草稿。
- `get_publish_status`：查看本地操作记录，可选查询公众号远端状态。

## 安装

建议 Python 3.11 或更新版本。

```bash
git clone https://github.com/beganzzw/bg-unified-publisher.git
cd bg-unified-publisher
python -m venv .venv
```

Windows：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

## 上游连接要求

运行时通过 `--config` 读取本机 JSON 配置中的 `mcpServers`，需要存在以下两个键：

- `wechat_oa_api_mcp`
- `xiaohongshu-mcp`

请复用已经配置并验证可用的服务条目。支持 `stdio`、`http`、`streamable-http` 和 `sse`；HTTP 连接必须显式设置 `type`，未指定时按 stdio 处理。

公众号上游需提供 `get_access_token`、`create_wechat_draft`、`publish_wechat_draft`，接收 `{"args":"JSON字符串"}`；获取令牌及业务结果须使用当前适配器支持的 JSON 字段。小红书上游需提供 `publish_content`，接收标题、正文、图片及标签。每次调用前动态发现能力并校验上游参数。

这不是任意同名服务的通用兼容层。接入其他实现时，应核对 `adapters.py` 的参数和响应约定。小红书登录由上游服务维护；微信公众号的 AppID、AppSecret 由上游配置和包装器管理。

## 接入 MCP 客户端

将以下条目合并到客户端配置的 `mcpServers` 中，保留两个原有上游条目。将示例路径替换为本机绝对路径：

```json
{
  "mcpServers": {
    "bg-unified-publisher": {
      "command": "C:/path/to/bg-unified-publisher/.venv/Scripts/python.exe",
      "args": [
        "C:/path/to/bg-unified-publisher/server.py",
        "--config",
        "C:/path/to/mcp.json",
        "--db",
        "C:/path/to/bg-unified-publisher/operations.sqlite3"
      ]
    }
  }
}
```

macOS / Linux 将 `command` 改为项目 `.venv/bin/python` 的绝对路径。

在 WorkBuddy 中添加配置后，到连接器管理页的自定义连接器入口信任并启用服务。配置文件应保留在本机，禁止提交含凭据的配置到仓库。

## 调用示例

以下均为 MCP 工具的 JSON 参数。示例 ID、URL 和文件路径需要替换为真实值。

### 检查小红书文案

调用 `check_content`：

```json
{
  "platform": "xiaohongshu",
  "action": "public",
  "content": {
    "title": "这次模型更新值得关注吗",
    "content": "这里填写已经核对的正文。",
    "images": ["C:/images/cover.jpg"],
    "tags": ["人工智能", "科技观察"]
  }
}
```

本版本保守限制：标题最多 20 个字符；正文与标签合计最多 1000 个字符；图片 1–9 张。图片可使用 HTTP(S) 地址，或服务器所在机器上存在的绝对路径（PNG、JPG、JPEG、WebP）。远程部署时，客户端本地路径不能直接作为服务器路径使用。

### 发布小红书图文

调用 `publish_content`：

```json
{
  "action": "public",
  "idempotency_key": "article-001-xhs-v1",
  "contents": {
    "xiaohongshu": {
      "title": "这次模型更新值得关注吗",
      "content": "这里填写已确认发布的正文。",
      "images": ["C:/images/cover.jpg"],
      "tags": ["人工智能"]
    }
  }
}
```

此调用会公开提交。当前不支持定时发布、带货商品、原创声明开关或平台 AI 声明控件；使用前应在内容审核和平台操作中处理需要的声明。上游成功回执只记为 `submitted`，不视为已审核公开。

### 创建公众号草稿

调用 `publish_content`：

```json
{
  "action": "draft",
  "idempotency_key": "article-001-wechat-draft-v1",
  "contents": {
    "wechat": {
      "title": "这次模型更新值得关注吗",
      "content": "<section><p><span leaf=\"\">这里是正文。</span></p></section>",
      "image_url": "https://example.com/cover.jpg",
      "author": "作者名",
      "digest": "文章摘要"
    }
  }
}
```

封面 `image_url` 必须为可供上游读取的真实 HTTP(S) 地址。正文中的图片须先通过微信上传接口获得 `mmbiz.qpic.cn` 地址。本版没有整合公众号本地图片自动上传。

### 更新公众号草稿

调用 `update_content`，传入完整的目标文章字段，不要把它当作只提交差异的补丁：

```json
{
  "platform": "wechat",
  "action": "draft",
  "media_id": "EXISTING_DRAFT_MEDIA_ID",
  "index": 0,
  "idempotency_key": "article-001-wechat-update-v2",
  "content": {
    "title": "更新后的标题",
    "content": "<section><p><span leaf=\"\">更新后的正文。</span></p></section>",
    "thumb_media_id": "EXISTING_PERMANENT_THUMB_MEDIA_ID"
  }
}
```

封面需要已有永久素材 ID。仅支持草稿，不支持修改已发表文章。

### 提交公众号草稿公开发布

调用 `publish_content`：

```json
{
  "action": "public",
  "idempotency_key": "article-001-wechat-public-v1",
  "contents": {
    "wechat": {
      "draft_media_id": "EXISTING_DRAFT_MEDIA_ID"
    }
  }
}
```

需要公众号拥有相应发布权限。统一服务不能绕过微信 `48001` 等账号权限限制。

### 查询状态

调用 `get_publish_status`：

```json
{
  "platform": "wechat",
  "idempotency_key": "article-001-wechat-public-v1",
  "verify_remote": true
}
```

默认只查询本地记录。公众号远端查询可确认草稿存在或获取发布状态，草稿存在不代表正文逐字核验。小红书暂时只支持本地提交记录，需另外查看平台最终展示状态。

## 双平台操作与防重复

`contents` 可同时包含 `wechat` 和 `xiaohongshu`，但 `action` 为整次调用共用。因此“公众号存草稿 + 小红书公开发布”需要两次调用；结果按平台分别返回，不提供跨平台原子事务或自动回滚。

每次新的写操作使用唯一 `idempotency_key`。同一平台和 key 的请求只执行一次：重复相同参数返回保存的结果，参数不同则返回冲突。记录通过 SQLite 持久化。

失败、超时或响应无法识别时，不自动重试。遇到 `unknown`，先到平台核对，不能简单换 key 重发。不同 key 不会自动识别同一篇内容，也不承诺账号全局去重。

本地数据库仅记录请求指纹和精简结果，不存文章正文或访问令牌。它不是文章素材库；请自行备份原稿及平台 ID。

## 测试

```bash
python -m unittest discover -s . -p "test_*.py"
```

23 项离线测试覆盖参数检查、重复调用、未知结果、部分成功、适配器与敏感异常处理。测试使用替身，不触发真实发布。当前环境在导入 SDK 时可能出现 Pydantic 前向引用警告，测试仍通过；生产使用前请完成连接握手和受控验收。

## 安全与限制

- 不提交 `mcp.json`、`.env`、令牌、发布数据库或虚拟环境。
- 运行时读取上游凭据，原始响应只在内存处理，错误返回不暴露原始异常。
- 仅做基础格式检查，不检查事实真实性、素材授权，也不保证平台审核通过。
- 本地图片路径和远端图片 URL 应来自可信输入；不要将服务直接暴露给不可信客户端。
- 发布权限、账号登录和审核结果由各平台决定。

## 许可证

[MIT](LICENSE) · Copyright (c) 2026 beganzzw
