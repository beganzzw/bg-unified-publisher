"""纯业务层：不联网；只有执行写操作时才建立本地幂等记录。"""
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


class SafeError(Exception):
    pass


class Rejected(SafeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def web_url(value):
    if not isinstance(value, str):
        return False
    try:
        u = urlsplit(value)
        return u.scheme in ('https', 'http') and bool(u.hostname) and not u.username and not u.password
    except ValueError:
        return False


def local_image(value):
    try:
        return isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file() and Path(value).suffix.lower() in {'.png', '.jpg', '.jpeg'}
    except (OSError, ValueError):
        return False


class HTMLCheck(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.errors = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'iframe', 'object', 'embed', 'form', 'base', 'link', 'meta', 'svg'}:
            self.errors.append('HTML 含不支持的活动标签')
        for key, value in attrs:
            if key.startswith('on') or (value and re.search(r'javascript\s*:|expression\s*\(|@import', value, re.I)):
                self.errors.append('HTML 含活动属性')
        if tag == 'img':
            src = dict(attrs).get('src', '')
            if not local_image(src) and (not web_url(src) or urlsplit(src).hostname != 'mmbiz.qpic.cn'):
                self.errors.append('正文图片需本机绝对 PNG/JPEG 路径或微信 mmbiz.qpic.cn 地址')
            if local_image(src) and not re.search(r'\bsrc\s*=\s*[\"\']', self.get_starttag_text(), re.I):
                self.errors.append('本地图片 src 必须加引号')
        if tag not in {'img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'wbr', 'area', 'base', 'col', 'embed', 'param', 'track'}:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append('HTML 标签未正确配对')


def check_content(platform, content, action='draft', operation='publish'):
    errors = []
    warnings = ['仅作技术检查，不保证平台审核、权限、素材有效性或发布成功']
    if platform not in {'wechat', 'xiaohongshu'}:
        errors.append('平台不支持')
    if action not in {'draft', 'public'}:
        errors.append('action 必须显式为 draft 或 public')
    if not isinstance(content, dict):
        return {'valid': False, 'errors': ['content 必须是对象'], 'warnings': warnings}
    if platform == 'xiaohongshu' and (action != 'public' or operation == 'update'):
        errors.append('小红书仅支持图文公开发布，不支持草稿或更新')
    if platform == 'wechat' and action == 'public' and operation == 'publish':
        if set(content) != {'draft_media_id'} or not isinstance(content.get('draft_media_id'), str) or not content['draft_media_id'].strip():
            errors.append('公众号 public 只接受已有 draft_media_id；先单独创建并审核草稿')
        return {'valid': not errors, 'errors': errors, 'warnings': warnings}
    allowed = {'title', 'content', 'images', 'tags'} if platform == 'xiaohongshu' else {'title', 'content', 'image_url', 'image_path', 'author', 'digest', 'source_url', 'need_open_comment', 'thumb_media_id'}
    if set(content) - allowed:
        errors.append('包含不支持字段')
    title, body = content.get('title'), content.get('content')
    if not isinstance(title, str) or not title.strip() or len(title) > (20 if platform == 'xiaohongshu' else 64):
        errors.append('标题为空或超过本 MVP 长度限制')
    if not isinstance(body, str) or not body.strip():
        errors.append('正文不能为空')
    elif platform == 'xiaohongshu':
        if len(body) > 1000:
            errors.append('正文超过本 MVP 1000 字符限制')
    else:
        if len(body) >= 20000 or len(body.encode()) >= 1000000:
            errors.append('HTML 正文须少于 20000 字符及 1MB')
        parser = HTMLCheck()
        parser.feed(body)
        errors.extend(parser.errors)
        if parser.stack:
            errors.append('HTML 标签未闭合')
    if platform == 'xiaohongshu':
        images = content.get('images')
        if not isinstance(images, list) or not 1 <= len(images) <= 9 or not all(web_url(x) or (isinstance(x, str) and Path(x).is_absolute() and Path(x).is_file() and Path(x).suffix.lower() in {'.png', '.jpg', '.jpeg', '.webp'}) for x in images):
            errors.append('需要 1–9 张 HTTP(S) 图片地址或当前机器存在的绝对图片路径')
        tags = content.get('tags', [])
        if not isinstance(tags, list) or not all(isinstance(t, str) and t.strip() for t in tags):
            errors.append('tags 必须是非空字符串数组')
        elif isinstance(body, str) and len(body) + sum(len(t) + 2 for t in tags) > 1000:
            errors.append('正文加标签超过本 MVP 1000 字符限制')
        warnings.append('图片只检查地址和数量，不下载、不验证 MIME 或尺寸；标题保守按字符计数')
    elif platform == 'wechat':
        covers = [k for k in ('image_path', 'image_url', 'thumb_media_id') if k in content]
        if len(covers) != 1:
            errors.append('需且仅需一种封面：image_path、image_url 或 thumb_media_id')
        if 'image_path' in content and not local_image(content['image_path']):
            errors.append('image_path 需存在的绝对 PNG/JPEG 路径')
        if 'image_url' in content and not web_url(content['image_url']):
            errors.append('image_url 格式无效')
        if 'thumb_media_id' in content and (not isinstance(content['thumb_media_id'], str) or not content['thumb_media_id'].strip()):
            errors.append('thumb_media_id 不可为空')
        for field in ('author', 'digest', 'source_url', 'thumb_media_id'):
            if field in content and not isinstance(content[field], str):
                errors.append('可选文本字段类型错误')
        if content.get('source_url') and not web_url(content['source_url']):
            errors.append('source_url 格式无效')
        if 'need_open_comment' in content and (type(content['need_open_comment']) is not int or content['need_open_comment'] not in (0, 1)):
            errors.append('need_open_comment 只能为 0 或 1')
    return {'valid': not errors, 'errors': sorted(set(errors)), 'warnings': warnings}


class Store:
    def __init__(self, path):
        self.path = str(path)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=15)
        try:
            with con:
                con.execute('CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL)')
                yield con
        finally:
            con.close()

    def reserve(self, key, fingerprint):
        with self.connect() as con:
            cur = con.execute('INSERT OR IGNORE INTO operations VALUES (?,?,?)', (digest(key), fingerprint, json.dumps({'state': 'unknown', 'reason': 'reserved_or_interrupted', 'remote_verified': False})))
            row = con.execute('SELECT fingerprint,result FROM operations WHERE key=?', (digest(key),)).fetchone()
            if row[0] != fingerprint:
                raise Rejected('idempotency_conflict')
            return cur.rowcount == 1, json.loads(row[1])

    def save(self, key, result):
        with self.connect() as con:
            con.execute('UPDATE operations SET result=? WHERE key=?', (json.dumps(result), digest(key)))

    def read(self, key):
        if not Path(self.path).exists():
            return None
        with self.connect() as con:
            row = con.execute('SELECT result FROM operations WHERE key=?', (digest(key),)).fetchone()
            return json.loads(row[0]) if row else None


class Publisher:
    def __init__(self, adapters, store):
        self.adapters, self.store = adapters, store

    async def execute(self, platform, content, action, key, operation='publish', media_id=None, index=0):
        check = check_content(platform, content, action, operation)
        if not check['valid']:
            return {'state': 'validation_failed', 'check': check, 'remote_verified': False}
        if not isinstance(key, str) or not 1 <= len(key) <= 200:
            return {'state': 'validation_failed', 'reason': 'invalid_idempotency_key'}
        if operation == 'update' and (not isinstance(media_id, str) or not media_id.strip() or type(index) is not int or index < 0 or action != 'draft'):
            return {'state': 'validation_failed', 'reason': 'invalid_update_target'}
        scoped = [key, platform]
        fingerprint = digest([platform, operation, action, content, media_id, index])
        try:
            fresh, previous = self.store.reserve(scoped, fingerprint)
        except Rejected:
            return {'state': 'conflict', 'reason': 'idempotency_key_reused_with_different_request'}
        if not fresh:
            return {**previous, 'replayed': True, 'retry_allowed': False}
        try:
            adapter = self.adapters[platform]
            if operation == 'update':
                result = await adapter.update(content, media_id, index)
            else:
                result = await adapter.publish(content, action)
        except Rejected:
            result = {'state': 'failed', 'reason': 'rejected_before_send_or_explicit_remote_rejection'}
        except Exception:
            # 异常文本可能含 URL、令牌、请求体，绝不返回或记录。
            result = {'state': 'unknown', 'reason': 'outcome_unknown_do_not_retry'}
        result = {**result, 'remote_verified': False, 'retry_allowed': False}
        self.store.save(scoped, result)
        return result

    async def publish_content(self, contents, action, idempotency_key, platform_actions=None):
        if not isinstance(contents, dict) or not contents:
            return {'state': 'validation_failed'}
        if platform_actions is not None and (not isinstance(platform_actions, dict) or set(platform_actions) - set(contents) or any(a not in ('draft', 'public') for a in platform_actions.values())):
            return {'state': 'validation_failed', 'reason': 'invalid_platform_actions'}
        results = {}
        for platform, content in contents.items():
            selected_action = (platform_actions or {}).get(platform, action)
            results[platform] = await self.execute(platform, content, selected_action, idempotency_key)
        states = [r['state'] for r in results.values()]
        good = sum(s in {'submitted', 'draft_created', 'updated'} for s in states)
        return {'state': 'success' if good == len(states) else 'partial_success' if good else 'not_completed', 'platforms': results}

    async def get_publish_status(self, platform, idempotency_key, verify_remote=False):
        record = self.store.read([idempotency_key, platform])
        result = {'source': 'local_submission_record', 'remote_verified': False, 'record': record}
        if not verify_remote or not record:
            return result
        if platform != 'wechat':
            return {**result, 'reason': 'remote_verification_unsupported_in_mvp'}
        try:
            remote = await self.adapters[platform].status(record)
            return {**result, 'source': 'remote_query_and_local_record', 'remote_verified': True, 'remote': remote}
        except Exception:
            return {**result, 'reason': 'remote_verification_unavailable'}
