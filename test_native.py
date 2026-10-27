"""所有网络用 MockTransport，进程用 mock，文件仅写临时目录。"""
import asyncio
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from adapters import Wechat, Xiaohongshu, Secrets
from core import Rejected, SafeError, check_content
from engine import EngineManager, install, asset_info, metadata

TOKEN = 'FAKE_TOKEN_FOR_OFFLINE_TEST_ONLY'


class NativeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for target in ('socket.socket.connect', 'subprocess.Popen'):
            guard = patch(target, side_effect=AssertionError('禁止真实网络或进程'))
            guard.start()
            self.addCleanup(guard.stop)
        self.calls = []

    def wechat(self, responder=None):
        def handler(request):
            self.calls.append(request)
            path = request.url.path
            if path.endswith('/token'):
                return httpx.Response(200, json={'access_token': TOKEN, 'expires_in': 7200})
            if responder:
                return responder(request)
            results = {'material/add_material': {'media_id': 'cover'}, 'media/uploadimg': {'url': 'https://mmbiz.qpic.cn/image'},
                       'draft/add': {'media_id': 'draft'}, 'draft/update': {'errcode': 0}, 'draft/get': {'news_item': [{}]},
                       'freepublish/submit': {'publish_id': 'pub'}, 'freepublish/get': {'publish_status': 0}}
            return httpx.Response(200, json=results[path.removeprefix('/cgi-bin/')])
        factory = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw)
        return Wechat(http_factory=factory, appid='fake-app', secret='fake-secret')

    async def test_local_images_cover_and_html_replacement(self):
        image = self.root / '图片.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nFAKE')
        content = {'title': '标题', 'content': f'<p>正文</p><img src="{image}"><img src=\'{image}\'/>', 'image_path': str(image)}
        self.assertTrue(check_content('wechat', content)['valid'])
        adapter = self.wechat()
        result = await adapter.publish(content, 'draft')
        self.assertEqual(result['media_id'], 'draft')
        article = json.loads(self.calls[-1].content)['articles'][0]
        self.assertEqual(article['thumb_media_id'], 'cover')
        self.assertNotIn(str(image), article['content'])
        self.assertEqual(article['content'].count('https://mmbiz.qpic.cn/image'), 2)
        self.assertEqual(sum(r.url.path.endswith('/uploadimg') for r in self.calls), 1)
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(sum(r.url.path.endswith('/token') for r in self.calls), 1)

    async def test_token_cache_concurrent_and_safe_id(self):
        adapter = self.wechat()
        await asyncio.gather(*(adapter.token() for _ in range(5)))
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(SafeError):
            adapter.secrets.safe_id(TOKEN)

    async def test_missing_credentials(self):
        with self.assertRaises(Rejected):
            await Wechat(appid='', secret='').token()

    async def test_update_submit_and_status_contract(self):
        adapter = self.wechat()
        await adapter.update({'title': '标题', 'content': '<p>正文</p>', 'thumb_media_id': 'cover', 'source_url': 'https://example.com'}, 'draft', 0)
        payload = json.loads(self.calls[-1].content)
        self.assertEqual(payload['articles']['content_source_url'], 'https://example.com')
        self.assertEqual((await adapter.publish({'draft_media_id': 'draft'}, 'public'))['publish_id'], 'pub')
        self.assertEqual((await adapter.status({'publish_id': 'pub'}))['state'], 'published')
        self.assertEqual((await adapter.status({'media_id': 'draft'}))['state'], 'draft_exists')

    async def test_wechat_explicit_rejection(self):
        adapter = self.wechat(lambda r: httpx.Response(200, json={'errcode': 40001, 'errmsg': TOKEN}))
        with self.assertRaises(Rejected) as caught:
            await adapter.publish({'draft_media_id': 'draft'}, 'public')
        self.assertNotIn(TOKEN, str(caught.exception))

    async def test_wechat_transport_does_not_leak(self):
        def broken(r):
            raise httpx.ConnectError(TOKEN)
        with self.assertRaises(SafeError) as caught:
            await self.wechat(broken).publish({'draft_media_id': 'draft'}, 'public')
        self.assertNotIn(TOKEN, str(caught.exception))

    async def test_invalid_image_not_uploaded(self):
        image = self.root / 'bad.png'
        image.write_bytes(b'not an image')
        with self.assertRaises(Rejected):
            await self.wechat().upload(str(image))
        self.assertEqual(self.calls, [])

    async def test_xhs_rest_acknowledgement_only(self):
        engine = Mock()
        engine.request = AsyncMock(return_value={'status': '发布完成', 'content': TOKEN})
        adapter = Xiaohongshu(engine)
        result = await adapter.publish({'title': '标题'}, 'public')
        self.assertEqual(result['state'], 'submitted')
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertEqual(engine.request.call_args.args[1], '/api/v1/publish')
        self.assertEqual(engine.request.call_args.args[2]['visibility'], '公开可见')
        engine.request.return_value = {'success': True}
        self.assertEqual((await adapter.publish({}, 'public'))['state'], 'unknown')

    async def test_login_and_qr_allowlist(self):
        engine = Mock()
        engine.request = AsyncMock(return_value={'is_logged_in': False, 'cookie': TOKEN})
        adapter = Xiaohongshu(engine)
        self.assertEqual(await adapter.login_status(), {'is_logged_in': False})
        engine.request.return_value = {'is_logged_in': False, 'img': 'data:image/png;base64,YQ==', 'timeout': '4m0s', 'cookie': TOKEN}
        self.assertNotIn(TOKEN, json.dumps(await adapter.login_qrcode()))
        engine.request.return_value['img'] = 'invalid'
        with self.assertRaises(SafeError):
            await adapter.login_qrcode()

    def manager(self, handler):
        manager = EngineManager(self.root, binary=self.root / 'engine', http_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
        manager.ensure_started = AsyncMock()
        manager.base_url = 'http://127.0.0.1:12345'
        return manager

    async def test_rest_wrapper_and_error_are_not_success(self):
        for payload in ({'success': False, 'data': {}}, {'success': True}, {'success': 'true', 'data': {}}, {'error': TOKEN}):
            manager = self.manager(lambda r: httpx.Response(200, json=payload))
            with self.assertRaises(SafeError) as caught:
                await manager.request('POST', '/api/v1/publish', {})
            self.assertNotIn(TOKEN, str(caught.exception))
        manager = self.manager(lambda r: httpx.Response(200, json={'success': True, 'data': {'status': '发布完成'}}))
        self.assertEqual((await manager.request('POST', '/api/v1/publish', {}))['status'], '发布完成')

    async def test_unknown_route_never_starts(self):
        manager = self.manager(lambda r: httpx.Response(200))
        with self.assertRaises(Rejected):
            await manager.request('GET', '/mcp')
        manager.ensure_started.assert_not_called()

    async def test_missing_binary_does_not_download_or_start(self):
        manager = EngineManager(self.root, self.root / 'absent')
        with self.assertRaises(Rejected):
            await manager.ensure_started()
        self.assertIsNone(manager.process)

    async def test_browser_missing_refuses_start(self):
        manager = EngineManager(self.root, self.root / 'engine')
        with patch('engine.verify_binary'):
            with self.assertRaises(Rejected):
                await manager.ensure_started()
        self.assertIsNone(manager.process)

    async def test_lifecycle_loopback_and_owned_process_only(self):
        process = Mock()
        process.poll.return_value = None
        def handler(r):
            if r.url.path == '/health':
                return httpx.Response(200)
            return httpx.Response(400 if r.headers.get('Authorization') == 'Bearer ' + manager.auth else 401)
        manager = EngineManager(self.root, self.root / 'engine', http_factory=lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler), **kw))
        with patch('engine.verify_binary'), patch.object(manager, 'check_browser'), patch('engine.subprocess.Popen', return_value=process) as popen:
            await asyncio.gather(manager.ensure_started(), manager.ensure_started())
            popen.assert_called_once()
            args, kwargs = popen.call_args
            self.assertTrue(args[0][2].startswith('127.0.0.1:'))
            self.assertEqual(kwargs['env']['COOKIES_PATH'], str(self.root / 'cookies' / 'cookies.json'))
            self.assertNotIn('WECHAT_OA_SECRET', kwargs['env'])
        manager.close()
        manager.close()
        process.terminate.assert_called_once()
        self.assertFalse((self.root / 'manager.lock').exists())

    async def test_failed_start_cleans_owned_process(self):
        manager = EngineManager(self.root, self.root / 'engine')
        process = Mock()
        process.poll.return_value = 1
        with patch('engine.verify_binary'), patch.object(manager, 'check_browser'), patch('engine.subprocess.Popen', return_value=process):
            with self.assertRaises(Rejected):
                await manager.ensure_started()
        process.terminate.assert_not_called()
        self.assertIsNone(manager.process)
        self.assertFalse((self.root / 'manager.lock').exists())

    async def test_existing_runtime_owner_is_untouched(self):
        lock = self.root / 'manager.lock'
        lock.write_text('existing-owner')
        manager = EngineManager(self.root, self.root / 'engine')
        with patch('engine.verify_binary'), patch.object(manager, 'check_browser'):
            with self.assertRaises(Rejected):
                await manager.ensure_started()
        manager.close()
        self.assertEqual(lock.read_text(), 'existing-owner')

    async def test_terminate_timeout_kills_only_owned_handle(self):
        manager = EngineManager(self.root, self.root / 'engine')
        process = Mock()
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('fake', 5), 0]
        manager.process = process
        manager.close()
        process.kill.assert_called_once()

    async def test_install_hash_and_idempotence_without_execution(self):
        payload = b'fake-engine'
        asset = {'name': 'engine', 'url': 'https://example.com/fixed', 'sha256': hashlib.sha256(payload).hexdigest()}
        factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=payload)), **kw)
        with patch('engine.asset_info', return_value=asset):
            target = install(self.root, factory)
            self.assertEqual(target.read_bytes(), payload)
            self.assertEqual(install(self.root, factory), target)
        self.assertEqual([p.name for p in self.root.iterdir()], ['engine'])

    async def test_install_corrupt_download_cleans_staging(self):
        factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b'corrupt')), **kw)
        with self.assertRaises(Rejected):
            install(self.root, factory)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_existing_binary_never_overwritten(self):
        target = self.root / asset_info()['name']
        target.write_bytes(b'user-file')
        with self.assertRaises(Rejected):
            install(self.root)
        self.assertEqual(target.read_bytes(), b'user-file')

    async def test_install_rejects_http_redirect(self):
        def handler(request):
            if request.url.scheme == 'https':
                return httpx.Response(302, headers={'Location': 'http://example.com/unsafe'})
            return httpx.Response(200, content=b'unsafe')
        factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
        with self.assertRaises(Rejected):
            install(self.root, factory)
        self.assertEqual(list(self.root.iterdir()), [])

    async def test_exported_schema_matches_registration(self):
        from server import build_server
        tools = await build_server(None).list_tools()
        exported = json.loads(Path(__file__).with_name('mcp-tools.schema.json').read_text(encoding='utf-8'))
        self.assertEqual(exported['tools'], [t.model_dump(mode='json', exclude_none=True) for t in tools])

    async def test_manifest_fixed_release_and_digests(self):
        info = metadata()
        self.assertEqual(info['version'], 'v2.5.5')
        for asset in info['assets'].values():
            self.assertIn('/releases/download/v2.5.5/', asset['url'])
            self.assertRegex(asset['sha256'], r'^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
