# MCP 接口规范

服务 `bg-unified-publisher` 使用 mcp==1.26.0 FastMCP，客户端传输仅 stdio。初始化、JSON-RPC 封装和工具分发由 SDK 实现；协议版本由 initialize 协商，不声称支持 SDK 之外的版本。不注册业务 Resources、Prompts、Sampling、Elicitation 或 Tasks。

## 工具

| 工具 | 必填 | 可选 | 副作用 |
| --- | --- | --- | --- |
| prepare_content | contents, action | platform_actions | 本地检查，不改写 |
| check_content | platform, content, action | operation=publish | 本地检查 |
| publish_content | contents, action, idempotency_key | platform_actions | 上传素材、创建草稿或公开提交 |
| update_content | platform, content, media_id, idempotency_key, action | index=0 | 更新公众号草稿 |
| get_publish_status | platform, idempotency_key | verify_remote=false | 默认只读本地，可查微信远端 |
| xiaohongshu_login_status | 无 | 无 | 惰性启动引擎并打开浏览器检查登录 |
| xiaohongshu_login_qrcode | 无 | 无 | 获取二维码，扫码成功后引擎保存独立 cookie |

机器契约由实际 list_tools 导出至 mcp-tools.schema.json。嵌套 content 为 dict，业务验证由 core.check_content 实施，不能仅依赖顶层 JSON Schema。

## 输入约束

平台是 wechat 或 xiaohongshu。action 是 draft 或 public；platform_actions 是可选平台到 action 的映射，覆盖共同 action。publish_content 拒绝映射中的未知平台或非法 action；action 纳入每平台幂等指纹，旧调用不必加映射。

微信 draft/update：title 非空且最多 64 字符；content 为 HTML，少于 20000 字符且少于 1MB；不接受活动标签、事件属性或未配对标签。封面 image_path、image_url、thumb_media_id 三选一。image_path 需本地绝对 PNG/JPEG 路径；image_url 兼容旧字段，但执行时仅下载 HTTPS 公网资源且不跟随重定向；thumb_media_id 需非空。正文 img src 仅接受现有微信图片 URL 或加引号的本地绝对 PNG/JPEG 路径。上传后替换 src。可选 author、digest、source_url、need_open_comment（0/1）。更新需 action=draft、非空 media_id、非负整数 index，传完整文章。

微信 public 仅接受 content={draft_media_id: 已有草稿ID}。不自动创建后立即发布。

小红书仅 public 图文：title 最多 20 字符；content 与 tags 合计最多 1000 字符；images 1–9 张 HTTP(S) 地址或存在的绝对 PNG/JPEG/WebP 路径。不支持 update、draft、视频或发布后读回。

## 状态与幂等

发布返回 `{state, platforms}`，整体 state 为 success、partial_success、not_completed；输入整体无效可为 validation_failed。平台结果可为 draft_created、submitted、updated、validation_failed、failed、unknown、conflict。

SQLite 以 `[idempotency_key, platform]` 的散列唯一预占，指纹包含 operation/action/content/目标。相同请求重放加 replayed=true；参数不同返回 conflict。预占后崩溃保留 unknown。失败与未知均不自动重发，也不自动换 key。不同 key 不提供全局防重复，跨平台无事务回滚。

写结果 remote_verified=false；submitted 只代表上游接受或浏览器回执。小红书要求 HTTP 成功、success 严格为 true、data 是对象，且 data.status 精确为“发布完成”；其他结果保持 unknown。错误正文和异常文本不返回、不落库。

get_publish_status 返回 source=local_submission_record、record、remote_verified=false。微信 verify_remote=true 可返回 source=remote_query_and_local_record 及 remote_verified=true，remote 是草稿存在性或 freepublish 状态；草稿存在不保证内容逐字一致。查询失败保留原本地记录。小红书远端验证返回 unsupported 原因，不假装已公开。

登录状态只返回 is_logged_in。二维码只返回 is_logged_in、img、timeout；不返回用户名、cookie 或引擎 token。登录失败返回 unavailable 和固定诊断提示，不输出原始响应。

## 原生接口与进程

微信直接调用 https://api.weixin.qq.com/cgi-bin/ 下 token、material/add_material（永久封面）、media/uploadimg（正文图片）、draft/add/get/update、freepublish/submit/get。环境变量 WECHAT_OA_APPID / WECHAT_OA_SECRET，token 仅内存缓存。

小红书固定 v2.5.5 REST 路由：GET /api/v1/login/status、GET /api/v1/login/qrcode、POST /api/v1/publish。启动健康探测使用 /health、错误鉴权请求及空发布参数验证；空参数在 handler 参数校验阶段返回 400，不执行发布。不会请求 /mcp，不连接既有 18060 服务。

显式 engine.py install 才能下载固定 SHA256 引擎；server.py 不下载。固定浏览器需提前安装，缺缓存拒绝启动。独立 cookie/cache/temp、随机本地端口及鉴权、运行目录独占锁；正常退出只结束本实例持有句柄的进程，不查杀外部进程。强制杀死/断电后的残留需人工核实，详见 README。

## 验证范围

当前仅完成 44 项离线 mock 测试，不构成真实登录、浏览器兼容性或真实发布验收。未启动生产 MCP、未修改全局配置、未向 GitHub 推送。源码实际行为优先于文档说明。
