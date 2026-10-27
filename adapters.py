"""原生微信 HTTP API 与内置小红书引擎 REST 适配器。"""
import asyncio
import html
import logging
import os
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from core import Rejected, SafeError, local_image


def silence_logs():
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


class ImageSources(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == 'img':
            self.sources.extend(v for k, v in attrs if k == 'src' and v)


class Wechat:
    BASE = 'https://api.weixin.qq.com/cgi-bin/'

    def __init__(self, secrets=None, http_factory=httpx.AsyncClient, appid=None, secret=None):
        self.secrets = secrets or Secrets()
        self.http_factory = http_factory
        self.appid = appid if appid is not None else os.environ.get('WECHAT_OA_APPID', '')
        self.secret = secret if secret is not None else os.environ.get('WECHAT_OA_SECRET', '')
        self.secrets.add(self.secret)
        self._token, self._expires = None, 0
        self._lock = asyncio.Lock()

    async def request(self, method, endpoint, **kwargs):
        try:
            async with self.http_factory(timeout=60, follow_redirects=False, trust_env=False) as client:
                response = await client.request(method, self.BASE + endpoint, **kwargs)
                response.raise_for_status()
                data = response.json()
            if not isinstance(data, dict):
                raise SafeError('invalid_official_response')
            if data.get('errcode', 0) != 0:
                raise Rejected('official_api_rejected')
            return data
        except (Rejected, SafeError):
            raise
        except Exception:
            raise SafeError('official_transport_or_response_error') from None

    async def token(self):
        async with self._lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            if not self.appid or not self.secret:
                raise Rejected('wechat_credentials_missing')
            data = await self.request('GET', 'token', params={'grant_type': 'client_credential', 'appid': self.appid, 'secret': self.secret})
            token, ttl = data.get('access_token'), data.get('expires_in')
            if not isinstance(token, str) or not token or type(ttl) is not int or ttl <= 0:
                raise Rejected('credential_unavailable')
            self.secrets.add(token)
            self._token, self._expires = token, time.monotonic() + max(0, ttl - 120)
            return token

    async def official(self, endpoint, payload):
        return await self.request('POST', endpoint, params={'access_token': await self.token()}, json=payload)

    async def image_bytes(self, source):
        if local_image(source):
            data = Path(source).read_bytes()
        else:
            # 仅兼容旧封面 URL；不带 token 下载，不跟随重定向。
            import ipaddress
            import socket
            try:
                u = urlsplit(source)
                if u.scheme != 'https' or not u.hostname or u.username or u.password or u.port not in (None, 443):
                    raise ValueError()
                addresses = await asyncio.get_running_loop().run_in_executor(None, socket.getaddrinfo, u.hostname, 443)
                if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                    raise ValueError()
                async with self.http_factory(timeout=30, follow_redirects=False, trust_env=False) as client:
                    async with client.stream('GET', source) as response:
                        response.raise_for_status()
                        data = b''
                        async for chunk in response.aiter_bytes():
                            data += chunk
                            if len(data) > 10 * 1024 * 1024:
                                raise ValueError()
            except Exception:
                raise Rejected('cover_download_failed_use_local_image') from None
        if not data or len(data) > 10 * 1024 * 1024:
            raise Rejected('image_size_invalid')
        if data.startswith(b'\x89PNG\r\n\x1a\n'):
            return data, 'image.png', 'image/png'
        if data.startswith(b'\xff\xd8\xff'):
            return data, 'image.jpg', 'image/jpeg'
        raise Rejected('only_png_jpeg_supported')

    async def upload(self, source, cover=False):
        data, name, mime = await self.image_bytes(source)
        params = {'access_token': await self.token()}
        if cover:
            params['type'] = 'image'
        result = await self.request('POST', 'material/add_material' if cover else 'media/uploadimg', params=params, files={'media': (name, data, mime)})
        if cover:
            return self.secrets.safe_id(result.get('media_id'))
        url = result.get('url')
        if not isinstance(url, str) or urlsplit(url).hostname != 'mmbiz.qpic.cn' or urlsplit(url).scheme not in ('http', 'https'):
            raise SafeError('invalid_image_url')
        if any(s in url for s in self.secrets.values):
            raise SafeError('secret_in_image_url')
        return url

    async def article(self, content):
        article = dict(content)
        cover = article.pop('image_path', None) or article.pop('image_url', None)
        if cover:
            article['thumb_media_id'] = await self.upload(cover, cover=True)
        if 'source_url' in article:
            article['content_source_url'] = article.pop('source_url')
        parser = ImageSources()
        parser.feed(article['content'])
        replacements = {}
        for src in parser.sources:
            if local_image(src) and src not in replacements:
                replacements[src] = await self.upload(src)
        # 只替换 img 的 src 属性，保留原 HTML 其他内容及格式。
        def replace_tag(match):
            def replace_src(attr):
                value = html.unescape(attr.group(2))
                return attr.group(1) + html.escape(replacements.get(value, value), quote=True) + attr.group(3)
            return re.sub(r'((?<![\w:-])src\s*=\s*[\"\'])(.*?)([\"\'])', replace_src, match.group(0), flags=re.I | re.S)
        article['content'] = re.sub(r'<img\b[^>]*>', replace_tag, article['content'], flags=re.I | re.S)
        return article

    async def publish(self, content, action):
        if action == 'draft':
            data = await self.official('draft/add', {'articles': [await self.article(content)]})
            return {'state': 'draft_created', 'media_id': self.secrets.safe_id(data.get('media_id'))}
        data = await self.official('freepublish/submit', {'media_id': self.secrets.safe_id(content['draft_media_id'])})
        return {'state': 'submitted', 'publish_id': self.secrets.safe_id(data.get('publish_id'))}

    async def update(self, content, media_id, index):
        media_id = self.secrets.safe_id(media_id)
        data = await self.official('draft/update', {'media_id': media_id, 'index': index, 'articles': await self.article(content)})
        if data.get('errcode') != 0:
            raise SafeError('unrecognized_update_response')
        return {'state': 'updated', 'media_id': media_id}

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
    def __init__(self, engine):
        self.engine = engine

    async def publish(self, content, action):
        if action != 'public':
            raise Rejected('draft_unsupported')
        data = await self.engine.request('POST', '/api/v1/publish', {**content, 'visibility': '公开可见'})
        if data.get('status') == '发布完成':
            return {'state': 'submitted', 'reason': 'submission_acknowledged_public_visibility_not_verified'}
        return {'state': 'unknown', 'reason': 'unrecognized_publish_response'}

    async def login_status(self):
        data = await self.engine.request('GET', '/api/v1/login/status')
        if type(data.get('is_logged_in')) is not bool:
            raise SafeError('invalid_login_response')
        return {'is_logged_in': data['is_logged_in']}

    async def login_qrcode(self):
        data = await self.engine.request('GET', '/api/v1/login/qrcode')
        if type(data.get('is_logged_in')) is not bool:
            raise SafeError('invalid_qrcode_response')
        if not data['is_logged_in'] and not re.fullmatch(r'data:image/(?:png|jpeg);base64,[A-Za-z0-9+/=\r\n]+', data.get('img', '')):
            raise SafeError('invalid_qrcode_image')
        return {k: data[k] for k in ('is_logged_in', 'img', 'timeout') if k in data}

    async def update(self, content, media_id, index):
        raise Rejected('update_unsupported')
