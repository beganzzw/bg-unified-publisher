"""运行时连接既有 MCP；所有上游原始响应仅在内存处理。"""
import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import httpx
from jsonschema import validate
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.client.sse import sse_client

from core import Rejected, SafeError


def silence_logs():
    # HTTP 调试日志可能携带 query token，禁用整个服务的 Python 日志。
    logging.disable(logging.CRITICAL)


class Secrets:
    def __init__(self):
        self.values = set()

    def add(self, value):
        if isinstance(value, str) and value:
            self.values.add(value)

    def safe_id(self, value):
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', value):
            raise SafeError('unsafe_or_missing_identifier')
        if any(secret in value for secret in self.values):
            raise SafeError('secret_in_identifier')
        return value


def load_config(path, secrets):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))['mcpServers']
    selected = {}
    for name in ('wechat_oa_api_mcp', 'xiaohongshu-mcp'):
        item = config[name]
        for value in (item.get('env') or {}).values():
            secrets.add(value)
        for value in (item.get('headers') or {}).values():
            secrets.add(value)
        selected[name] = item
    return selected


class MCPBridge:
    def __init__(self, config, secrets):
        self.config, self.secrets = config, secrets

    @asynccontextmanager
    async def session(self):
        c = self.config
        kind = c.get('type', 'stdio')
        if kind == 'stdio':
            args = c.get('args', [])
            if '--debug' in args:
                raise Rejected('upstream_debug_not_allowed')
            env = {**os.environ, **(c.get('env') or {})}
            params = StdioServerParameters(command=c['command'], args=args, env=env)
            with open(os.devnull, 'w') as sink:
                async with stdio_client(params, errlog=sink) as (read, write):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                        await session.initialize()
                        yield session
        elif kind in ('http', 'streamable-http'):
            async with httpx.AsyncClient(headers=c.get('headers'), timeout=120, follow_redirects=False) as client:
                async with streamable_http_client(c['url'], http_client=client) as (read, write, _):
                    async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                        await session.initialize()
                        yield session
        elif kind == 'sse':
            async with sse_client(c['url'], headers=c.get('headers'), timeout=120) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=120)) as session:
                    await session.initialize()
                    yield session
        else:
            raise Rejected('unsupported_transport')

    async def discover(self, session):
        tools, cursor, seen = {}, None, set()
        while True:
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                tools[tool.name] = tool.inputSchema
            cursor = page.nextCursor
            if cursor is None:
                return tools
            if cursor in seen:
                raise Rejected('invalid_pagination')
            seen.add(cursor)

    async def call(self, name, arguments):
        async with asyncio.timeout(150):
            async with self.session() as session:
                tools = await self.discover(session)
                if name not in tools:
                    raise Rejected('capability_missing')
                try:
                    validate(arguments, tools[name])
                except Exception:
                    raise Rejected('upstream_schema_mismatch') from None
                result = await session.call_tool(name, arguments=arguments)
                if result.isError:
                    # 工具可能在已经发送请求后才失败，保守记作未知。
                    raise SafeError('upstream_tool_error')
                data = getattr(result, 'structuredContent', None)
                if data is None:
                    texts = [x.text for x in result.content if x.type == 'text']
                    if len(texts) != 1:
                        raise SafeError('unrecognized_response')
                    try:
                        data = json.loads(texts[0])
                    except ValueError:
                        if name == 'publish_content' and texts[0].startswith('内容发布成功:') and 'Status:发布完成' in texts[0]:
                            return {'submission_acknowledged': True}
                        raise SafeError('unrecognized_response') from None
                if isinstance(data, dict) and set(data) == {'result'}:
                    data = data['result']
                if not isinstance(data, dict):
                    raise SafeError('unrecognized_response')
                return data


class Wechat:
    def __init__(self, bridge, secrets, http_factory=httpx.AsyncClient):
        self.bridge, self.secrets, self.http_factory = bridge, secrets, http_factory

    async def token(self):
        data = await self.bridge.call('get_access_token', {'args': '{}'})
        token = data.get('access_token')
        if data.get('success') is not True or not isinstance(token, str) or not token:
            raise Rejected('credential_unavailable')
        self.secrets.add(token)
        return token

    def check_result(self, data):
        if data.get('success') is not True:
            if type(data.get('errcode')) is int and data['errcode'] != 0:
                raise Rejected('remote_rejected')
            raise SafeError('unknown_upstream_result')

    async def publish(self, content, action):
        token = await self.token()
        if action == 'draft':
            data = await self.bridge.call('create_wechat_draft', {'args': json.dumps({**content, 'access_token': token}, ensure_ascii=False)})
            self.check_result(data)
            return {'state': 'draft_created', 'media_id': self.secrets.safe_id(data.get('draft_media_id'))}
        data = await self.bridge.call('publish_wechat_draft', {'args': json.dumps({'draft_media_id': content['draft_media_id'], 'access_token': token})})
        self.check_result(data)
        return {'state': 'submitted', 'publish_id': self.secrets.safe_id(data.get('publish_id'))}

    async def official(self, endpoint, payload):
        token = await self.token()
        async with self.http_factory(timeout=30, follow_redirects=False) as client:
            response = await client.post('https://api.weixin.qq.com/cgi-bin/' + endpoint, params={'access_token': token}, json=payload)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise SafeError('invalid_official_response')
        if data.get('errcode', 0) != 0:
            raise Rejected('official_api_rejected')
        return data

    async def update(self, content, media_id, index):
        safe_media = self.secrets.safe_id(media_id)
        article = dict(content)
        if 'source_url' in article:
            article['content_source_url'] = article.pop('source_url')
        data = await self.official('draft/update', {'media_id': safe_media, 'index': index, 'articles': article})
        if data.get('errcode') != 0:
            raise SafeError('unrecognized_update_response')
        return {'state': 'updated', 'media_id': safe_media}

    async def status(self, record):
        if record.get('publish_id'):
            data = await self.official('freepublish/get', {'publish_id': record['publish_id']})
            code = data.get('publish_status')
            states = {0: 'published', 1: 'publishing', 2: 'originality_failed', 3: 'failed', 4: 'review_rejected', 5: 'deleted', 6: 'blocked'}
            if type(code) is not int or code not in states:
                raise SafeError('invalid_status_response')
            return {'state': states[code], 'publish_status': code}
        if record.get('media_id'):
            data = await self.official('draft/get', {'media_id': record['media_id']})
            if not isinstance(data.get('news_item'), list) or not data['news_item']:
                raise SafeError('invalid_draft_response')
            return {'state': 'draft_exists', 'article_count': len(data['news_item']), 'content_match_verified': False}
        raise SafeError('no_remote_identifier')


class Xiaohongshu:
    def __init__(self, bridge):
        self.bridge = bridge

    async def publish(self, content, action):
        if action != 'public':
            raise Rejected('draft_unsupported')
        data = await self.bridge.call('publish_content', {**content, 'visibility': '公开可见'})
        if data.get('submission_acknowledged') is True:
            return {'state': 'submitted', 'reason': 'submission_acknowledged_public_visibility_not_verified'}
        return {'state': 'unknown', 'reason': 'upstream_returned_but_publication_not_verified'}

    async def update(self, content, media_id, index):
        raise Rejected('update_unsupported')
