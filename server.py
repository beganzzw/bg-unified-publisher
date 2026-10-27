"""统一发布 MCP。导入本模块不会读取配置、联网或创建数据库。"""
import argparse
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from adapters import MCPBridge, Secrets, Wechat, Xiaohongshu, load_config, silence_logs
from core import Publisher, Store, check_content as technical_check


def build_server(publisher):
    app = FastMCP('bg-unified-publisher', log_level='CRITICAL')

    @app.tool()
    def prepare_content(contents: dict, action: str) -> dict:
        """组装用户已写好的平台文案并校验；不进行 AI 自动改写。平台为 wechat/xiaohongshu。"""
        return {'contents': contents, 'action': action, 'checks': {p: technical_check(p, c, action) for p, c in contents.items()}, 'rewritten': False}

    @app.tool()
    def check_content(platform: str, content: dict, action: str, operation: str = 'publish') -> dict:
        """长度、图片地址和 HTML 技术检查，不构成内容合规保证。"""
        return technical_check(platform, content, action, operation)

    @app.tool()
    async def publish_content(contents: dict, action: str, idempotency_key: str) -> dict:
        """显式 draft/public；公众号 public 只收已有 draft_media_id。失败或未知不得自动换 key 重试。"""
        return await publisher.publish_content(contents, action, idempotency_key)

    @app.tool()
    async def update_content(platform: str, content: dict, media_id: str, idempotency_key: str, action: str, index: int = 0) -> dict:
        """仅更新公众号已有草稿，action 必须为 draft；不支持修改已发布文章或小红书笔记。"""
        return await publisher.execute(platform, content, action, idempotency_key, 'update', media_id, index)

    @app.tool()
    async def get_publish_status(platform: str, idempotency_key: str, verify_remote: bool = False) -> dict:
        """默认只查本地提交记录；显式 verify_remote 才查询公众号远端。小红书远端验证不支持。"""
        return await publisher.get_publish_status(platform, idempotency_key, verify_remote)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--db', default=str(Path(__file__).with_name('operations.sqlite3')))
    args = parser.parse_args()
    silence_logs()
    try:
        secrets = Secrets()
        config = load_config(args.config, secrets)
        wechat = Wechat(MCPBridge(config['wechat_oa_api_mcp'], secrets), secrets)
        xhs = Xiaohongshu(MCPBridge(config['xiaohongshu-mcp'], secrets))
        publisher = Publisher({'wechat': wechat, 'xiaohongshu': xhs}, Store(args.db))
        build_server(publisher).run(transport='stdio')
    except Exception:
        # 启动错误也不允许 traceback 暴露环境或请求信息。
        raise SystemExit('统一发布服务启动或运行失败；检查配置结构及依赖，不输出原始异常。') from None


if __name__ == '__main__':
    main()
