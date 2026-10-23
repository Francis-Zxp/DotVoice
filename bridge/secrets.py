"""API keys at rest, encrypted with Windows DPAPI for the current Windows user.

Each value is protected with CryptProtectData (CRYPTPROTECT_UI_FORBIDDEN, current-user
scope, an app- and name-specific entropy) and stored base64-encoded in
<runtime>/secrets.json. Only the same Windows user on the same PC can decrypt it; a copy of
the file is useless elsewhere. Values are never logged or put into exceptions; masked() is
what a UI may show.

    from bridge import secrets
    secrets.configure(runtime_dir)        # once, by the core (defaults to DotVoice's runtime/)
    secrets.save('volc.api_key', value); secrets.load('volc.api_key') -> str | None
    secrets.delete('volc.api_key');      secrets.masked('volc.api_key') -> '••••abcd' | ''
"""
import base64
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

CRYPTPROTECT_UI_FORBIDDEN = 0x1
FILE_NAME = 'secrets.json'
MASK = '••••'
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}')
_ENTROPY = b'DotVoice secret v1\x00'
MAX_VALUE = 4096
READ_RETRY = .25   # another program (antivirus, backup) briefly holding the file


class _Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


_api = None


def _dpapi():
    global _api
    if _api is None:
        if sys.platform != 'win32':
            raise OSError('密钥加密存储需要 Windows（DPAPI）')
        crypt32 = ctypes.WinDLL('crypt32', use_last_error=True)
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        protect = crypt32.CryptProtectData
        protect.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p,
                            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        protect.restype = wintypes.BOOL
        unprotect = crypt32.CryptUnprotectData
        unprotect.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p,
                              ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
        unprotect.restype = wintypes.BOOL
        free = kernel32.LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        _api = (protect, unprotect, free)
    return _api


def _in_blob(data):
    buffer = ctypes.create_string_buffer(bytes(data), max(1, len(data)))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def _call(function, data, entropy, description=None):
    source, source_buffer = _in_blob(data)
    extra, extra_buffer = _in_blob(entropy)
    out = _Blob()
    try:
        args = [ctypes.byref(source), description, ctypes.byref(extra), None, None,
                CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)]
        if not function(*args):
            raise OSError(ctypes.get_last_error(), 'DPAPI 操作失败')
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            ctypes.memset(out.pbData, 0, out.cbData)
            _dpapi()[2](ctypes.cast(out.pbData, ctypes.c_void_p))
    finally:
        ctypes.memset(source_buffer, 0, len(source_buffer))   # best effort: drop the plaintext copy
        ctypes.memset(extra_buffer, 0, len(extra_buffer))


def protect(data, entropy=b''):
    """DPAPI-encrypt bytes for the current user (no UI)."""
    return _call(_dpapi()[0], data, entropy, 'DotVoice')


def unprotect(blob, entropy=b''):
    """Decrypt bytes made by protect() for this user on this PC; OSError otherwise."""
    return _call(_dpapi()[1], blob, entropy)


def _check_name(name):
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError('密钥名称无效')
    return name


def mask(value):
    """'••••' plus at most the last 4 characters (fewer for short values)."""
    if not value:
        return ''
    tail = value[-4:] if len(value) >= 12 else value[-2:] if len(value) >= 8 else ''
    return MASK + tail


class SecretStore:
    """Encrypted name -> value store in one JSON file. Thread-safe; never logs values."""

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def __repr__(self):
        return f'SecretStore({str(self.path)!r})'

    @staticmethod
    def _entropy(name):
        return _ENTROPY + name.encode('utf-8')   # a value only decrypts under its own name

    def _read(self, strict=False):
        """{} when the file is missing or damaged. When it cannot be read (antivirus or backup
        holding it): {} for a lookup; strict (before a write) retries once, then raises, so a
        save never overwrites the keys it could not read."""
        for attempt in (0, 1):
            try:
                data = json.loads(self.path.read_text(encoding='utf-8'))
                break
            except FileNotFoundError:
                return {}
            except ValueError:   # damaged: it decrypts nothing, a save may start over
                return {}
            except OSError:
                if not strict:
                    return {}
                if attempt:
                    raise
                time.sleep(READ_RETRY)
        items = data.get('items') if isinstance(data, dict) else None
        return {k: v for k, v in items.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(items, dict) else {}

    def _write(self, items):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(self.path.name + '.tmp')
        temp.write_text(json.dumps({'version': 1, 'items': items}, indent=1), encoding='utf-8')
        os.replace(temp, self.path)

    def save(self, name, value):
        """Encrypt and store; an empty value deletes. Surrounding whitespace is dropped
        (keys pasted with a trailing space or newline are a classic failure)."""
        _check_name(name)
        if value is None:
            return self.delete(name)
        if not isinstance(value, str):
            raise ValueError('密钥必须是文本')
        value = value.strip()
        if not value:
            return self.delete(name)
        if len(value) > MAX_VALUE or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('密钥格式无效（过长或含控制字符）')
        blob = protect(value.encode('utf-8'), self._entropy(name))
        with self.lock:
            items = self._read(strict=True)
            items[name] = base64.b64encode(blob).decode('ascii')
            self._write(items)

    def load(self, name):
        """The stored value, or None if missing or not decryptable here (other user / PC, damaged)."""
        _check_name(name)
        with self.lock:
            encoded = self._read().get(name)
        if not encoded:
            return None
        try:
            return unprotect(base64.b64decode(encoded, validate=True), self._entropy(name)).decode('utf-8')
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def delete(self, name):
        _check_name(name)
        with self.lock:
            items = self._read(strict=True)
            if items.pop(name, None) is not None:
                self._write(items)

    def masked(self, name):
        return mask(self.load(name))

    def has(self, name):
        _check_name(name)
        with self.lock:
            return name in self._read()

    def names(self):
        with self.lock:
            return sorted(self._read())


def default_runtime_dir():
    """DotVoice's runtime/ folder, chosen as core_main.py does (installed: %LOCALAPPDATA%\\DotVoice)."""
    if getattr(sys, 'frozen', False):
        program = Path(sys.executable).parent
        if (program.parent / 'installed.json').exists():
            return Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'DotVoice' / 'runtime'
        return program.parent / 'runtime'
    return Path(__file__).resolve().parents[1] / 'runtime'


_store = None
_store_lock = threading.Lock()


def configure(runtime_dir):
    """Use <runtime_dir>/secrets.json (the core calls this with its own runtime folder)."""
    global _store
    with _store_lock:
        _store = SecretStore(Path(runtime_dir) / FILE_NAME)
    return _store


def store():
    global _store
    with _store_lock:
        if _store is None:
            _store = SecretStore(default_runtime_dir() / FILE_NAME)
        return _store


def save(name, value):
    store().save(name, value)


def load(name):
    return store().load(name)


def delete(name):
    store().delete(name)


def masked(name):
    return store().masked(name)
