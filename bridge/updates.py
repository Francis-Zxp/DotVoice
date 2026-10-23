"""Read-only release checks for DotVoice. Never downloads or runs an installer."""
import json
from http.client import HTTPException
import math
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPOSITORY = 'https://github.com/Francis-Zxp/DotVoice'
RELEASE_API = 'https://api.github.com/repos/Francis-Zxp/DotVoice/releases/latest'
CHECK_INTERVAL = 6 * 60 * 60
RETRY_AFTER_ERROR = 20 * 60  # offline (at logon, before Wi-Fi): look again soon, not in six hours
MAX_RESPONSE = 512 * 1024
VERSION = re.compile(r'(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})', re.ASCII)


def _version(value):
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError('发行版版本号无效')
    return tuple(int(part) for part in value.split('.'))


def _fetch_release():
    request = Request(RELEASE_API, headers={
        'Accept': 'application/vnd.github+json', 'User-Agent': 'DotVoice-Update-Check',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    with urlopen(request, timeout=10) as response:
        body = response.read(MAX_RESPONSE + 1)
    if len(body) > MAX_RESPONSE:
        raise ValueError('发行版信息过大，未采用')
    return body


def _release(tag, download_url, release_url):
    if not isinstance(tag, str):
        raise ValueError('发行版标签无效')
    version = tag[1:] if tag.startswith('v') else tag
    _version(version)
    expected_download = f'{REPOSITORY}/releases/download/{tag}/DotVoice-{version}-windows-x64.zip'
    expected_release = f'{REPOSITORY}/releases/tag/{tag}'
    if download_url != expected_download or release_url != expected_release:
        raise ValueError('发行版链接不属于点声官方仓库或与版本不符')
    return {'version': version, 'tag': tag, 'download_url': download_url, 'release_url': release_url}


def _parse(body):
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE:
        raise ValueError('发行版信息格式或大小无效')
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError('无法读取发行版信息') from exc
    if not isinstance(data, dict) or data.get('draft') is not False or data.get('prerelease') is not False:
        raise ValueError('未找到可用的正式发行版')
    tag = data.get('tag_name')
    if not isinstance(tag, str):
        raise ValueError('发行版标签无效')
    version = tag[1:] if tag.startswith('v') else tag
    _version(version)
    assets = data.get('assets')
    if not isinstance(assets, list):
        raise ValueError('发行版缺少 Windows 下载包')
    matches = [asset for asset in assets if isinstance(asset, dict)
               and asset.get('name') == f'DotVoice-{version}-windows-x64.zip']
    if len(matches) != 1:
        raise ValueError('发行版的 Windows 下载包缺失或重复')
    return _release(tag, matches[0].get('browser_download_url'), data.get('html_url'))


class UpdateWatch:
    def __init__(self, path, current_version, *, fetch=None, clock=time.time):
        self.path = Path(path)
        self.current_version = current_version
        self._current = _version(current_version)
        self._fetch = fetch or _fetch_release
        self._clock = clock
        self._lock = threading.RLock()
        self._request_lock = threading.Lock()
        self._release = None
        self._last_checked = None
        self._retry_at = None  # after a network failure: the next automatic check, earlier
        self._error = ''
        self._dismissed = ''
        self._checking = False
        self._load()

    def _load(self):
        try:
            with self.path.open('rb') as source:
                body = source.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                return
            data = json.loads(body)
            if not isinstance(data, dict):
                return
            cached = data.get('release')
            if isinstance(cached, dict):
                self._release = _release(cached.get('tag'), cached.get('download_url'), cached.get('release_url'))
            checked = data.get('last_checked')
            if type(checked) in (int, float) and math.isfinite(checked) and checked >= 0:
                self._last_checked = checked
            retry = data.get('retry_at')
            if type(retry) in (int, float) and math.isfinite(retry) and retry >= 0:
                self._retry_at = retry  # a restart soon after a failed check still retries early
            ignored = data.get('dismissed_version')
            if isinstance(ignored, str) and VERSION.fullmatch(ignored):
                self._dismissed = ignored
            error = data.get('error')
            if isinstance(error, str):
                self._error = error[:300]
        except (OSError, ValueError, TypeError, RecursionError, OverflowError):
            pass  # A corrupt optional cache cannot prevent the app from opening.

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix('.tmp')
            temp.write_text(json.dumps({
                'release': self._release, 'last_checked': self._last_checked,
                'retry_at': self._retry_at, 'dismissed_version': self._dismissed, 'error': self._error,
            }, ensure_ascii=False), encoding='utf-8')
            temp.replace(self.path)
        except OSError:
            self._error = (self._error + '；' if self._error else '') + '更新检查结果未能保存，重启后可能需要重新检查'

    def due(self):
        with self._lock:
            if self._checking:
                return False
            now = self._clock()
            if self._retry_at is not None and now >= self._retry_at:
                return True
            age = None if self._last_checked is None else now - self._last_checked
            return age is None or age < 0 or age >= CHECK_INTERVAL

    def snapshot(self):
        with self._lock:
            release = self._release or {}
            latest = release.get('version', '')
            available = bool(latest and _version(latest) > self._current)
            status = ('checking' if self._checking else 'error' if self._error else
                      'available' if available else 'current' if latest else 'idle')
            return {
                'status': status, 'current_version': self.current_version, 'latest_version': latest,
                'download_url': release.get('download_url', ''),
                'release_url': release.get('release_url', REPOSITORY + '/releases'),
                'last_checked': self._last_checked, 'error': self._error,
                'available': available, 'dismissed': bool(available and self._dismissed == latest),
                'dismissed_version': self._dismissed,
            }

    def check(self, force=False):
        """Blocking network operation: call from a worker, not the audio/UI thread."""
        if not self._request_lock.acquire(blocking=False):
            return self.snapshot()
        try:
            with self._lock:
                if not force and not self.due():
                    return self.snapshot()
                self._checking = True
                self._last_checked = self._clock()
            release, error, transient = None, '', False
            try:
                release = _parse(self._fetch())
            except HTTPError as exc:
                error = ('尚无公开发行版，请稍后重试' if exc.code == 404 else
                         'GitHub 暂时限制更新检查，请稍后重试' if exc.code in (403, 429) else
                         'GitHub 更新服务暂不可用，请稍后重试')
                transient = exc.code >= 500
            except (URLError, OSError, TimeoutError, HTTPException):
                error = '无法连接 GitHub，请检查网络后重试'
                transient = True
            except (ValueError, TypeError) as exc:
                error = str(exc) or '发行版信息无效'
            with self._lock:
                if release is not None:
                    self._release = release
                self._error = error
                # A network failure retries soon; a rate limit or a missing release keeps the
                # six-hour interval.
                self._retry_at = self._clock() + RETRY_AFTER_ERROR if transient else None
                self._checking = False
                self._save()
                return self.snapshot()
        finally:
            with self._lock:
                self._checking = False
            self._request_lock.release()

    def dismiss(self, version=None):
        with self._lock:
            state = self.snapshot()
            version = state['latest_version'] if version is None else version
            if not state['available'] or version != state['latest_version']:
                raise ValueError('只能忽略当前可用的更新版本')
            self._dismissed = version
            self._save()
            return self.snapshot()
