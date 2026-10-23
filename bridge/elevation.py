"""Synthetic input versus administrator windows (UIPI).

Windows drops SendInput aimed at a window whose process runs at a higher integrity level
than the sender: a non-elevated DotVoice cannot click, scroll, type or send a hotkey into
an elevated (administrator) window - e.g. 红果短剧 inside Tencent's Androws.exe. Nothing
says so: SendInput usually still reports success. The pen pointer keeps moving (real HID).

So right before DotVoice sends synthetic input it asks UipiWatch.blocked(kind): is the
foreground window's process elevated while DotVoice itself is not? The input is still sent
(nothing is lost by trying); the caller tells the user once per window why nothing
happened, and typed text that could not go in is kept (bridge.core copies it to the
clipboard). Settings → 通用 → 兼容管理员权限窗口 runs DotVoice elevated (the shell's
elevation.rs), after which nothing here reports anything.

A process that cannot be opened (another user's, a protected one) counts as unknown, which
is treated as not elevated: no notice rather than a wrong one.
"""
from collections import OrderedDict
import ctypes as c
from ctypes import wintypes as w
import threading
import time

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_TYPE, TOKEN_ELEVATION = 18, 20   # TOKEN_INFORMATION_CLASS
ELEVATION_DEFAULT, ELEVATION_FULL, ELEVATION_LIMITED = 1, 2, 3
CACHE_SECONDS = 5.0     # a pid's answer is reused this long (cheap enough for 10 Hz scrolling)
WINDOWS_REMEMBERED = 64  # windows already told about (once per window)

SETTING = '设置 → 通用 → 兼容管理员权限窗口'
NOTICE = f'该窗口以管理员权限运行，系统拦截了点声的模拟点击/输入。可在 {SETTING} 开启'
# The account cannot elevate (a standard user): pointing at the setting would not help.
NOTICE_NO_ADMIN = '该窗口以管理员权限运行，系统拦截了点声的模拟点击/输入；当前 Windows 账户不是管理员，无法兼容'


def kept_message(copied, can_elevate=True, reason='elevated'):
    """Text that did not reach the window is kept: what the user is told. reason is the
    result's not_typed: 'elevated' (an administrator window), 'partial' or 'refused'
    (Windows refused some or all keystrokes: lock screen, UAC prompt)."""
    if reason == 'partial':   # the clipboard holds the full text: pasting it as is would repeat the start
        where = ('全文已复制到剪贴板：请先删掉已输入的部分，再按 Ctrl+V 粘贴' if copied
                 else '剪贴板被占用，全文保留在点声主窗口的本次结果里')
        return f'部分文字没能输入：系统中途拒绝了点声的模拟输入（可能是锁屏或 UAC 提示）。{where}。'
    where = ('文字已复制到剪贴板，按 Ctrl+V 粘贴' if copied
             else '剪贴板被占用，文字保留在点声主窗口的本次结果里')
    if reason != 'elevated':
        return f'文字没能输入：系统拒绝了点声的模拟输入（可能是锁屏或 UAC 提示）。{where}。'
    fix = f'在 {SETTING} 开启后可直接输入' if can_elevate else '当前 Windows 账户不是管理员，无法兼容'
    return f'文字没能输入：该窗口以管理员权限运行，系统拦截了点声的模拟输入。{where}；{fix}。'

kernel32 = c.WinDLL('kernel32', use_last_error=True)
advapi32 = c.WinDLL('advapi32', use_last_error=True)
user32 = c.WinDLL('user32', use_last_error=True)  # own instances: argtypes stay local
kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
kernel32.OpenProcess.restype = w.HANDLE
kernel32.GetCurrentProcess.restype = w.HANDLE
kernel32.CloseHandle.argtypes = [w.HANDLE]
kernel32.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
advapi32.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
advapi32.GetTokenInformation.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)]
user32.GetForegroundWindow.restype = w.HWND
user32.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
user32.GetWindowThreadProcessId.restype = w.DWORD


class ElevatedTarget(OSError):
    """Text could not be typed: the foreground window runs elevated and DotVoice does not.
    `info` is UipiWatch.blocked()'s answer (kind, process, first...)."""

    def __init__(self, info):
        super().__init__(NOTICE)
        self.info = dict(info or {})


def _token_values(process):
    """(TokenElevation, TokenElevationType) of a process handle, or None."""
    token = w.HANDLE()
    if not advapi32.OpenProcessToken(process, TOKEN_QUERY, c.byref(token)):
        return None
    try:
        elevated, kind, size = w.DWORD(), w.DWORD(), w.DWORD()
        if not advapi32.GetTokenInformation(token, TOKEN_ELEVATION, c.byref(elevated), 4, c.byref(size)):
            return None
        if not advapi32.GetTokenInformation(token, TOKEN_ELEVATION_TYPE, c.byref(kind), 4, c.byref(size)):
            kind.value = 0
        return bool(elevated.value), kind.value
    finally:
        kernel32.CloseHandle(token)


def process_elevated(pid):
    """True / False, or None when the process (or its token) cannot be opened."""
    if not pid:
        return None
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        values = _token_values(handle)
        return None if values is None else values[0]
    finally:
        kernel32.CloseHandle(handle)


def own_status():
    """This process: {'elevated', 'can_elevate'}. can_elevate = an administrator account
    behind UAC (a split token) or already elevated; a standard user cannot."""
    values = _token_values(kernel32.GetCurrentProcess())
    if values is None:
        return {'elevated': False, 'can_elevate': False}
    elevated, kind = values
    return {'elevated': elevated, 'can_elevate': elevated or kind == ELEVATION_LIMITED}


def foreground_window():
    """(hwnd, pid) of the foreground window; (0, 0) when there is none."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return 0, 0
    pid = w.DWORD()
    user32.GetWindowThreadProcessId(hwnd, c.byref(pid))
    return int(hwnd), pid.value


def process_name(pid):
    """Executable name ('androws.exe'), '' when unknown."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid) if pid else None
    if not handle:
        return ''
    try:
        size = w.DWORD(520)
        buffer = c.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, c.byref(size)):
            return buffer.value.rsplit('\\', 1)[-1].lower()
        return ''
    finally:
        kernel32.CloseHandle(handle)


class UipiWatch:
    """Asked right before synthetic input, from any thread (also inside the keyboard hook):
    cheap (a cached answer per pid), never raises, never blocks on anything but the lock."""

    def __init__(self, clock=time.monotonic, foreground=foreground_window, elevated=process_elevated,
                 own=None, name=process_name, ttl=CACHE_SECONDS):
        self.clock, self.foreground, self.elevated, self.name, self.ttl = clock, foreground, elevated, name, ttl
        self._own = own
        self.lock = threading.Lock()
        self.cache = {}                 # pid -> (answer, at)
        self.told = OrderedDict()       # hwnd -> True: windows already told about
        self.count = 0                  # blocked inputs seen (diagnosis)

    def own(self):
        """DotVoice's own token, read once (it never changes while running)."""
        if self._own is None:
            try:
                self._own = own_status()
            except Exception:
                self._own = {'elevated': False, 'can_elevate': False}
        return self._own

    def notice(self):
        return NOTICE if self.own().get('can_elevate') else NOTICE_NO_ADMIN

    def blocked(self, kind):
        """None when the input can go through (or nobody knows). Otherwise a dict: kind,
        hwnd, pid, process, message, and first = this window was not told about yet (the
        window is marked as told now; the caller says it)."""
        try:
            if self.own().get('elevated'):
                return None
            hwnd, pid = self.foreground()
            if not hwnd or not pid:
                return None
            if not self._elevated(pid):
                return None
            with self.lock:
                self.count += 1
                first = hwnd not in self.told
                if first:
                    self.told[hwnd] = True
                    while len(self.told) > WINDOWS_REMEMBERED:
                        self.told.popitem(last=False)
            process = ''
            if first:  # only the notice names it (a held wheel key asks 10 times a second)
                try:
                    process = self.name(pid) or ''
                except Exception:
                    pass
            return {'kind': kind, 'hwnd': hwnd, 'pid': pid, 'process': process, 'first': first,
                    'message': self.notice()}
        except Exception:
            return None  # a broken probe never stops a click

    def _elevated(self, pid):
        now = self.clock()
        with self.lock:
            cached = self.cache.get(pid)
            if cached is not None and now - cached[1] < self.ttl:
                return cached[0]
        try:
            answer = self.elevated(pid)
        except Exception:
            answer = None
        with self.lock:
            if len(self.cache) > 256:
                self.cache.clear()
            self.cache[pid] = (answer, now)
        return answer

    def guard_typer(self, typer):
        """Wrap a text typer (bridge.fast_insert.type_text): it still types, but raises
        ElevatedTarget when the text went to an elevated window (dropped by Windows)."""
        def typed(text):
            info = self.blocked('type')
            try:
                typer(text)
            except OSError as error:
                if info is not None and not isinstance(error, ElevatedTarget):
                    raise ElevatedTarget(info) from error
                raise
            if info is not None:
                raise ElevatedTarget(info)
        typed.inner = typer
        return typed
