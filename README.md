# bg-unified-publisher

独立双平台 stdio MCP：公众号使用原生 httpx 官方 API；小红书使用本项目管理的固定版本 Go 引擎，通过 REST 调用。不需要两个已有 MCP 连接器，不读取全局 mcp.json，不需要 --config。

这是“内置管理引擎”，不是纯 Python 重写，也没有融合上游 Go 源码。小红书引擎本身仍包含 /mcp 路由，但本项目从不调用它。引擎只绑定 127.0.0.1 随机端口并启用每实例随机 Bearer 鉴权。

## 能力

| 功能 | 微信公众号 | 小红书 |
| --- | --- | --- |
| 登录 | WECHAT_OA_APPID / WECHAT_OA_SECRET 获取 token | 内置登录状态、二维码 |
| 图文 | 永久封面上传、本地正文图上传及 HTML src 替换 | 本地/远程图片公开提交 |
| 草稿 | draft/add、get、update | 不支持 |
| 公开提交 | freepublish/submit，仅接受已有 draft_media_id | REST /api/v1/publish |
| 状态 | freepublish/get 或草稿存在性 | 仅本地提交记录，不支持发布后读回 |

公众号 token 仅在内存缓存，不返回、不写库、不输出原始异常。封面三选一：`image_path`（绝对 PNG/JPEG 文件）、`thumb_media_id`（已有永久素材）、`image_url`（兼容旧调用，下载要求 HTTPS 公网地址，建议使用本地图片）。正文 img src 支持带引号的本机绝对 PNG/JPEG 路径或已有 mmbiz.qpic.cn 地址；相同本地正文图单次操作只上传一次。更新草稿同样支持图片上传。图片限制 10MB，并检查 PNG/JPEG 文件头；实际平台限制仍可能更严格。

## 独立环境安装

Python 3.11+，在项目目录执行：

```bash
python -m venv .venv
# Windows 使用 .venv/Scripts/python.exe；macOS/Linux 使用 .venv/bin/python
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe engine.py install
```

`engine.py install` 是显式网络下载命令：仅安装 release.json 固定的 v2.5.5 官方引擎，校验发行资产 SHA256，使用临时目录、原子落盘，不执行引擎。支持 Windows amd64、Linux amd64、macOS arm64。不支持的平台不会猜测下载地址。也可以不安装，向 server.py 传 `--xhs-binary <现有官方binary绝对路径>`，同样必须通过固定版本哈希校验。安装目标默认 `.engines/v2.5.5/`，可用 `--directory` 指定，然后配合 `--xhs-binary` 使用。

### 浏览器必须单独准备

v2.5.5 上游只使用其固定 Chromium 派生浏览器 `148.0.7778.215`，不支持用普通系统 Chrome 替代。上游源码在缺少缓存时会自动下载；本管理器启动前强制检查缓存，缺失即拒绝，运行时不会替用户执行这一下载。

管理员需显式从 release.json 的 browser_source 对应版本目录取得本平台归档及 SHA256SUMS，先核对摘要、保留浏览器原始许可证，再解压到以下独立目录（`<runtime>` 对应 --runtime-dir）：

| 系统 | 上游归档名 | 解压根目录 | 目录内必须存在 |
| --- | --- | --- | --- |
| Windows amd64 | windows-x64.zip | `<runtime>/cache/xiaohongshu-mcp/browser/148.0.7778.215/` | chrome.exe |
| Linux amd64 | linux-x64.tar.xz | `<runtime>/cache/xiaohongshu-mcp/browser/148.0.7778.215/` | chrome |
| macOS arm64 | macos-arm64.dmg | `<runtime>/home/Library/Caches/xiaohongshu-mcp/browser/148.0.7778.215/` | Chromium |

Linux/macOS 应保留浏览器执行权限及依赖文件；Linux 还需要浏览器系统库。浏览器由管理员预先安装，本项目不代替上游维护其供应链或分发许可证。运行期间不要删除缓存。cookie 写入 `<runtime>/cookies/cookies.json`，HOME、缓存和临时目录也独立。运行目录属于本地账号，应限制其他账号访问；Windows ACL 由部署者配置。

## 接入客户端

参考 [config.example.json](config.example.json)，替换占位路径，并在客户端安全环境配置中设置 `WECHAT_OA_APPID`、`WECHAT_OA_SECRET`。示例不包含真实凭据。小红书不需公众号凭据；凭据不会传给子进程。

```text
command: <项目虚拟环境Python绝对路径>
args: [<项目/server.py绝对路径>, --db, <本地数据库绝对路径>, --runtime-dir, <独立运行目录绝对路径>]
可选追加: [--xhs-binary, <固定官方binary绝对路径>]
```

没有 --config 参数。数据库的父目录需预先存在。只用公众号时无需安装小红书引擎；引擎在首次小红书调用时惰性启动。客户端关闭 stdio、正常退出、SIGTERM 时清理本实例启动的引擎；不会连接、接管或终止用户已有的 18060 服务。运行目录有独占锁，不同实例不得共享 cookie 目录。异常断电/强制杀死父进程可能留下锁或引擎，需人工核实归属后处理，程序不会按 PID 文件杀未知进程。清理通过自有进程句柄完成，不按名字全局结束浏览器。

## 调用

七个工具及完整参数见 [MCP_SPEC.md](MCP_SPEC.md)，可机器读取 [mcp-tools.schema.json](mcp-tools.schema.json)。

先调用 `xiaohongshu_login_qrcode`（参数 `{}`），展示返回的 `img`，扫码后调用 `xiaohongshu_login_status`。二维码是临时登录材料，仅交给当前用户，不应公开分享。

混合公众号草稿与小红书公开提交：

```json
{
  "action": "draft",
  "platform_actions": {"xiaohongshu": "public"},
  "idempotency_key": "article-001-v1",
  "contents": {
    "wechat": {"title": "文章标题", "content": "<p>正文</p>", "thumb_media_id": "EXISTING_PERMANENT_MEDIA_ID"},
    "xiaohongshu": {"title": "笔记标题", "content": "正文", "images": ["C:/example/images/cover.jpg"]}
  }
}
```

`platform_actions` 可选，每个平台优先采用自己的 action，未指定则用原有 action。旧版单平台调用保持兼容。公众号 public 内容只接受 `{"draft_media_id":"EXISTING_DRAFT_ID"}`，应先审核草稿。更新使用 `update_content`，传完整文章、media_id、index、action=draft 和新 idempotency_key。

`get_publish_status` 默认只查本地；`verify_remote=true` 才查公众号草稿存在性或发布状态。小红书只报告本地回执：即便引擎返回“发布完成”，也只记 `submitted`，不声称公开可见或审核成功。

同一平台和 key 只执行一次；参数改变返回 conflict；超时、中断及无法识别的响应保存 unknown，不自动重试。双平台不是原子事务，可返回 partial_success；不自动回滚。不同 key 不保证去重，遇到 unknown 应先人工核对。数据库只保存指纹和精简结果，不保存正文、cookie、token。

## 测试和边界

```bash
.venv/Scripts/python.exe -m unittest discover -s . -p "test_*.py" -v
```

44 项离线测试通过。原有业务测试保留并适配；上游 MCP 测试替换为原生 HTTP/REST、图片替换、token 安全、固定安装哈希和临时目录清理、生命周期归属、混合 action、部分成功及防重复测试。网络使用 MockTransport，进程使用 mock，测试数据只在 TemporaryDirectory。未实际安装/执行上游引擎或浏览器，未登录真实账号、未真实发布、未完成真实端到端验收。SDK 可能输出 Pydantic 前向引用警告，不影响当前测试。

仅支持图文；不支持小红书更新、定时、商品、平台声明控件或发布后读回。微信权限/IP 白名单和平台审核仍由账号及平台决定。HTML 检查只是格式检查。服务面向可信本地客户端，图片路径及远程 URL 应来自可信用户。

## 上游核查与授权

固定来源：[xpzouying/xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp)，v2.5.5。核查 README、routes.go、handlers_api.go、service.go、main.go、app_server.go、middleware.go、cookies/cookies.go、browser/browser_download.go 和 browser_version.txt；来源与资产摘要存于 third_party/xiaohongshu-mcp/release.json。

本项目原创代码保持根目录 MIT。上游引擎为 Apache-2.0，原始 LICENSE 和本项目归属 NOTICE 位于 third_party/xiaohongshu-mcp。浏览器及其依赖适用各自授权，不冒用本项目 MIT，也不将可能存在的其他许可证改标为 MIT。
