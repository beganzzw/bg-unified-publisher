"""离线测试：禁止真实网络和子进程；数据库仅位于 TemporaryDirectory。"""
import asyncio
import json
import socket
import subprocess
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

import httpx
from mcp.types import CallToolResult, TextContent

from adapters import MCPBridge, Secrets, Wechat, Xiaohongshu, silence_logs
from core import Publisher, Rejected, SafeError, Store, check_content
from server import build_server

WX = {'title': '标题', 'content': '<p>正文</p>', 'image_url': 'https://example.com/cover.jpg'}
XHS = {'title': '标题', 'content': '正文', 'images': ['https://example.com/image.jpg']}
SECRET = 'FAKE_PRIVATE_TOKEN_987654'


class FakeAdapter:
    def __init__(self, result=None, error=None):
        self.calls = 0
        self.result = result or {'state': 'submitted', 'publish_id': '123'}
        self.error = error

    async def publish(self, content, action):
        self.calls += 1
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return self.result

    async def update(self, content, media_id, index):
        return await self.publish(content, 'draft')

    async def status(self, record):
        return {'state': 'publishing', 'publish_status': 1}


class FakeBridge:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return self.responses.pop(0)


class OfflineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        self.a = FakeAdapter()
        self.b = FakeAdapter()
        self.p = Publisher({'wechat': self.a, 'xiaohongshu': self.b}, Store(self.db))
        for target in ('socket.socket.connect', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('禁止真实网络或子进程'))
            guard.start()
            self.addCleanup(guard.stop)

    async def test_duplicate_and_restart(self):
        first = await self.p.publish_content({'wechat': WX}, 'draft', 'same')
        p2 = Publisher(self.p.adapters, Store(self.db))
        second = await p2.publish_content({'wechat': WX}, 'draft', 'same')
        self.assertEqual(self.a.calls, 1)
        self.assertTrue(second['platforms']['wechat']['replayed'])
        self.assertEqual(first['state'], 'success')

    async def test_concurrent_duplicate(self):
        await asyncio.gather(*(self.p.publish_content({'wechat': WX}, 'draft', 'same') for _ in range(8)))
        self.assertEqual(self.a.calls, 1)

    async def test_conflict(self):
        await self.p.publish_content({'wechat': WX}, 'draft', 'same')
        result = await self.p.publish_content({'wechat': {**WX, 'title': '新标题'}}, 'draft', 'same')
        self.assertEqual(result['platforms']['wechat']['state'], 'conflict')
        self.assertEqual(self.a.calls, 1)

    async def test_unknown_no_retry_and_no_secrets(self):
        self.a.error = TimeoutError('access_token=' + SECRET)
        result = await self.p.publish_content({'wechat': WX}, 'draft', 'k')
        await self.p.publish_content({'wechat': WX}, 'draft', 'k')
        self.assertEqual(result['platforms']['wechat']['state'], 'unknown')
        self.assertEqual(self.a.calls, 1)
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn(SECRET.encode(), self.db.read_bytes())

    async def test_failed_no_retry(self):
        self.a.error = Rejected(SECRET)
        await self.p.publish_content({'wechat': WX}, 'draft', 'k')
        result = await self.p.publish_content({'wechat': WX}, 'draft', 'k')
        self.assertEqual(result['platforms']['wechat']['state'], 'failed')
        self.assertEqual(self.a.calls, 1)

    async def test_interrupted_reservation(self):
        from core import digest
        self.p.store.reserve(['k', 'wechat'], digest(['wechat', 'publish', 'draft', WX, None, 0]))
        result = await self.p.publish_content({'wechat': WX}, 'draft', 'k')
        self.assertEqual(result['platforms']['wechat']['state'], 'unknown')
        self.assertEqual(self.a.calls, 0)

    async def test_partial_success(self):
        self.b.error = TimeoutError()
        result = await self.p.publish_content({'wechat': {'draft_media_id': '123'}, 'xiaohongshu': XHS}, 'public', 'k')
        self.assertEqual(result['state'], 'partial_success')
        self.assertEqual(self.a.calls, 1)
        self.assertEqual(self.b.calls, 1)

    async def test_validation_never_sends(self):
        bad = [dict(WX, content='<script>x</script>'), dict(WX, content='<p>'), dict(WX, title='x' * 65), dict(WX, content='<img src="https://example.com/a">'), dict(WX, content='<p onclick="x">x</p>')]
        for item in bad:
            result = await self.p.publish_content({'wechat': item}, 'draft', 'k')
            self.assertEqual(result['platforms']['wechat']['state'], 'validation_failed')
        self.assertEqual(self.a.calls, 0)
        self.assertFalse(self.db.exists())

    async def test_xhs_validation(self):
        for item in (dict(XHS, images=[]), dict(XHS, images=['C:/a.jpg']), dict(XHS, content='x' * 1001)):
            self.assertFalse(check_content('xiaohongshu', item, 'public')['valid'])
        self.assertFalse(check_content('xiaohongshu', XHS, 'draft')['valid'])

    async def test_public_needs_existing_draft(self):
        result = await self.p.publish_content({'wechat': WX}, 'public', 'k')
        self.assertEqual(result['platforms']['wechat']['state'], 'validation_failed')
        self.assertEqual(self.a.calls, 0)

    async def test_update_validation(self):
        result = await self.p.execute('xiaohongshu', XHS, 'draft', 'k', 'update', '123')
        self.assertEqual(result['state'], 'validation_failed')
        item = {'title': '标题', 'content': '<p>正文</p>', 'thumb_media_id': 'thumb'}
        result = await self.p.execute('wechat', item, 'draft', 'k', 'update', '123', -1)
        self.assertEqual(result['state'], 'validation_failed')
        self.assertEqual(self.a.calls, 0)

    async def test_status_source(self):
        await self.p.publish_content({'wechat': {'draft_media_id': '123'}}, 'public', 'k')
        local = await self.p.get_publish_status('wechat', 'k')
        remote = await self.p.get_publish_status('wechat', 'k', True)
        self.assertFalse(local['remote_verified'])
        self.assertTrue(remote['remote_verified'])
        self.assertEqual(remote['remote']['state'], 'publishing')
        self.assertIsNone((await self.p.get_publish_status('wechat', 'missing'))['record'])

    async def test_remote_failure_is_not_verified(self):
        await self.p.publish_content({'xiaohongshu': XHS}, 'public', 'k')
        result = await self.p.get_publish_status('xiaohongshu', 'k', True)
        self.assertFalse(result['remote_verified'])

    async def test_fastmcp_registration_without_server(self):
        app = build_server(self.p)
        tools = await app.list_tools()
        self.assertEqual({x.name for x in tools}, {'prepare_content', 'check_content', 'publish_content', 'update_content', 'get_publish_status'})
        self.assertIn('action', next(x for x in tools if x.name == 'publish_content').inputSchema['required'])
        self.assertFalse(self.db.exists())

    async def test_discovery_before_call_and_pagination(self):
        events = []
        class Session:
            async def list_tools(self, cursor=None):
                events.append(('list', cursor))
                if cursor is None:
                    return NS(tools=[], nextCursor='page2')
                return NS(tools=[NS(name='publish_content', inputSchema={'type': 'object', 'required': ['title']})], nextCursor=None)
            async def call_tool(self, name, arguments):
                events.append(('call', name))
                return CallToolResult(content=[TextContent(type='text', text='{"ok":true}')])
        bridge = MCPBridge({}, Secrets())
        @asynccontextmanager
        async def session():
            yield Session()
        bridge.session = session
        await bridge.call('publish_content', {'title': '标题'})
        self.assertEqual(events, [('list', None), ('list', 'page2'), ('call', 'publish_content')])
        events.clear()
        with self.assertRaises(Rejected):
            await bridge.call('nonexistent', {})
        self.assertFalse(any(x[0] == 'call' for x in events))
        with self.assertRaises(Rejected):
            await bridge.call('publish_content', {})

    async def test_wechat_contract_and_token_not_returned(self):
        secrets = Secrets()
        bridge = FakeBridge([{'success': True, 'access_token': SECRET}, {'success': True, 'draft_media_id': 'draft123'}])
        adapter = Wechat(bridge, secrets)
        result = await adapter.publish(WX, 'draft')
        self.assertEqual(result, {'state': 'draft_created', 'media_id': 'draft123'})
        self.assertEqual(json.loads(bridge.calls[1][1]['args'])['access_token'], SECRET)
        self.assertNotIn(SECRET, json.dumps(result))
        with self.assertRaises(SafeError):
            secrets.safe_id(SECRET)

    async def test_wechat_unknown_wrapper_failure(self):
        bridge = FakeBridge([{'success': True, 'access_token': SECRET}, {'success': False, 'errcode': None, 'error_msg': SECRET}])
        with self.assertRaises(SafeError):
            await Wechat(bridge, Secrets()).publish({'draft_media_id': 'd'}, 'public')

    async def test_official_update_and_status_mock_http(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.url.path.endswith('/update'):
                return httpx.Response(200, json={'errcode': 0})
            if request.url.path.endswith('/freepublish/get'):
                return httpx.Response(200, json={'publish_status': 0})
            return httpx.Response(200, json={'news_item': [{'title': '标题'}]})
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
        bridge = FakeBridge([{'success': True, 'access_token': SECRET}] * 3)
        adapter = Wechat(bridge, Secrets(), factory)
        result = await adapter.update({'title': '标题', 'content': '<p>正文</p>', 'thumb_media_id': 'thumb'}, 'media', 0)
        self.assertEqual(result['state'], 'updated')
        payload = json.loads(requests[0].content)
        self.assertEqual(payload['media_id'], 'media')
        self.assertIsInstance(payload['articles'], dict)
        self.assertEqual((await adapter.status({'publish_id': 'pub'}))['state'], 'published')
        self.assertEqual((await adapter.status({'media_id': 'media'}))['state'], 'draft_exists')

    async def test_xhs_does_not_claim_publication(self):
        adapter = Xiaohongshu(FakeBridge([{'message': SECRET, 'success': True}]))
        result = await adapter.publish(XHS, 'public')
        self.assertEqual(result['state'], 'unknown')
        self.assertNotIn(SECRET, json.dumps(result))

    async def test_cancelled_write_stays_reserved(self):
        self.a.error = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.p.publish_content({'wechat': WX}, 'draft', 'cancel')
        result = await self.p.publish_content({'wechat': WX}, 'draft', 'cancel')
        self.assertEqual(result['platforms']['wechat']['state'], 'unknown')
        self.assertEqual(self.a.calls, 1)

    async def test_remote_query_error_preserves_local_record(self):
        await self.p.publish_content({'wechat': WX}, 'draft', 'k')
        async def broken(record):
            raise TimeoutError(SECRET)
        self.a.status = broken
        result = await self.p.get_publish_status('wechat', 'k', True)
        self.assertFalse(result['remote_verified'])
        self.assertIsNotNone(result['record'])
        self.assertNotIn(SECRET, json.dumps(result))

    async def test_bad_url_is_validation_failure(self):
        result = await self.p.publish_content({'wechat': dict(WX, image_url='https://[invalid')}, 'draft', 'k')
        self.assertEqual(result['platforms']['wechat']['state'], 'validation_failed')
        self.assertEqual(self.a.calls, 0)

    async def test_logging_disabled(self):
        import logging
        silence_logs()
        self.assertFalse(logging.getLogger('httpx').isEnabledFor(logging.ERROR))


if __name__ == '__main__':
    unittest.main(verbosity=2)
