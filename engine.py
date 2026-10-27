"""固定发行版安装及仅归属于本实例的引擎生命周期。导入不启动进程。"""
import argparse
import asyncio
import hashlib
import json
import os
import platform
import secrets
import socket
import subprocess
import tempfile
from pathlib import Path

import httpx

from core import Rejected, SafeError

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / 'third_party' / 'xiaohongshu-mcp' / 'release.json'


def metadata():
    return json.loads(MANIFEST.read_text(encoding='utf-8'))


def platform_key():
    machine = platform.machine().lower()
    arch = {'amd64': 'amd64', 'x86_64': 'amd64', 'arm64': 'arm64', 'aarch64': 'arm64'}.get(machine, machine)
    return platform.system().lower() + '-' + arch


def asset_info():
    try:
        return metadata()['assets'][platform_key()]
    except KeyError:
        raise Rejected('engine_platform_unsupported') from None


def verify_binary(path, asset):
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != asset['sha256']:
        raise Rejected('engine_missing_or_hash_mismatch_run_explicit_install')


def install(directory, client_factory=httpx.Client):
    """只下载固定清单中的单文件，不解压、不执行、不接受任意 URL。"""
    asset = asset_info()
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / asset['name']
    if target.exists():
        verify_binary(target, asset)
        return target
    with tempfile.TemporaryDirectory(prefix='.install-', dir=directory) as staging:
        temporary = Path(staging) / 'engine'
        digest = hashlib.sha256()
        count = 0
        try:
            def require_https(request):
                if request.url.scheme != 'https':
                    raise Rejected('insecure_download_redirect')
            with client_factory(timeout=120, follow_redirects=True, trust_env=False, event_hooks={'request': [require_https]}) as client:
                with client.stream('GET', asset['url']) as response:
                    response.raise_for_status()
                    if response.url.scheme != 'https' or any(r.url.scheme != 'https' for r in response.history):
                        raise Rejected('insecure_download_redirect')
                    with temporary.open('wb') as output:
                        for chunk in response.iter_bytes():
                            count += len(chunk)
                            if count > 200 * 1024 * 1024:
                                raise Rejected('engine_download_too_large')
                            digest.update(chunk)
                            output.write(chunk)
            if digest.hexdigest() != asset['sha256']:
                raise Rejected('engine_download_hash_mismatch')
            temporary.chmod(0o700)
            # 硬链接不覆盖其他安装者已写入的文件。
            try:
                os.link(temporary, target)
            except FileExistsError:
                verify_binary(target, asset)
            return target
        except Rejected:
            raise
        except Exception:
            raise SafeError('engine_install_failed') from None


class EngineManager:
    def __init__(self, runtime_dir, binary=None, http_factory=httpx.AsyncClient):
        self.runtime = Path(runtime_dir).resolve()
        self.binary = Path(binary).resolve() if binary else ROOT / '.engines' / metadata()['version'] / asset_info()['name']
        self.http_factory = http_factory
        self.process = None
        self.base_url = None
        self.auth = secrets.token_urlsafe(32)
        self._lock = asyncio.Lock()
        self._owner_file = None

    def child_env(self):
        # 不向引擎传入公众号凭据、代理或宿主鉴权信息。
        env = {k: v for k, v in os.environ.items() if k.upper() in {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'DISPLAY', 'WAYLAND_DISPLAY'}}
        for name in ('home', 'cache', 'tmp', 'cookies'):
            (self.runtime / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        env.update({'HOME': str(self.runtime / 'home'), 'USERPROFILE': str(self.runtime / 'home'),
                    'LOCALAPPDATA': str(self.runtime / 'cache'), 'XDG_CACHE_HOME': str(self.runtime / 'cache'),
                    'TMP': str(self.runtime / 'tmp'), 'TEMP': str(self.runtime / 'tmp'), 'TMPDIR': str(self.runtime / 'tmp'),
                    'COOKIES_PATH': str(self.runtime / 'cookies' / 'cookies.json'), 'AUTH_TOKEN': self.auth})
        return env

    def check_browser(self):
        cache = self.runtime / ('home/Library/Caches' if platform.system() == 'Darwin' else 'cache')
        folder = cache / 'xiaohongshu-mcp' / 'browser' / metadata()['browser_version']
        name = {'Windows': 'chrome.exe', 'Darwin': 'Chromium', 'Linux': 'chrome'}.get(platform.system())
        if not name or not any(p.is_file() for p in folder.rglob(name)):
            raise Rejected('browser_missing_install_fixed_browser_into_runtime_cache')

    def acquire_owner(self):
        self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        owner = self.runtime / 'manager.lock'
        try:
            self._owner_file = owner.open('x')
        except FileExistsError:
            raise Rejected('runtime_already_owned_or_stale_lock') from None
        self._owner_file.write(str(os.getpid()))
        self._owner_file.flush()

    async def ensure_started(self):
        async with self._lock:
            if self.process and self.process.poll() is None:
                return
            if self.process:
                self.close()
            verify_binary(self.binary, asset_info())
            self.check_browser()
            self.acquire_owner()
            try:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    port = probe.getsockname()[1]
                self.base_url = f'http://127.0.0.1:{port}'
                self.process = subprocess.Popen([str(self.binary), '-port', f'127.0.0.1:{port}', '-headless=true'],
                    cwd=self.runtime, env=self.child_env(), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                async with self.http_factory(timeout=2, trust_env=False, follow_redirects=False) as client:
                    for _ in range(60):
                        if self.process.poll() is not None:
                            raise Rejected('engine_exited_during_start')
                        try:
                            # 带随机鉴权验证归属；不调用会打开浏览器的 login/status。
                            response = await client.get(self.base_url + '/api/v1/login/status', headers={'Authorization': 'Bearer invalid-startup-probe'})
                            if response.status_code == 401:
                                health = await client.get(self.base_url + '/health')
                                if health.status_code == 200 and self.process.poll() is None:
                                    owned = await client.post(self.base_url + '/api/v1/publish', headers={'Authorization': 'Bearer ' + self.auth}, json={})
                                    if owned.status_code == 400:
                                        return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.2)
                raise Rejected('engine_start_timeout')
            except BaseException:
                self.close()
                raise

    async def request(self, method, route, payload=None):
        if (method, route) not in {('GET', '/api/v1/login/status'), ('GET', '/api/v1/login/qrcode'), ('POST', '/api/v1/publish')}:
            raise Rejected('unsupported_engine_route')
        await self.ensure_started()
        try:
            async with self.http_factory(timeout=180, trust_env=False, follow_redirects=False) as client:
                response = await client.request(method, self.base_url + route, headers={'Authorization': 'Bearer ' + self.auth}, json=payload)
                response.raise_for_status()
                data = response.json()
            # 发布错误也可能发生在点击提交之后，保守保持 unknown。
            if not isinstance(data, dict) or data.get('success') is not True or not isinstance(data.get('data'), dict):
                raise SafeError('engine_response_not_success')
            return data['data']
        except Exception:
            raise SafeError('engine_request_outcome_unknown') from None

    def close(self):
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._owner_file:
            self._owner_file.close()
            self._owner_file = None
            (self.runtime / 'manager.lock').unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='显式安装固定小红书引擎；不启动引擎，不下载浏览器')
    parser.add_argument('command', choices=['install'])
    parser.add_argument('--directory', default=str(ROOT / '.engines' / metadata()['version']))
    args = parser.parse_args()
    try:
        install(args.directory)
    except Exception:
        raise SystemExit('引擎安装失败；检查网络、平台及固定 SHA256，不输出原始异常。') from None
    print('固定版本引擎已安装并校验；使用前需按 README 准备独立浏览器缓存。')


if __name__ == '__main__':
    main()
