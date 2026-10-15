"""Put text on the Windows clipboard; the capsule window never takes focus."""
import ctypes as c
from ctypes import wintypes as w
import time

CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
user32 = c.WinDLL('user32', use_last_error=True)
kernel32 = c.WinDLL('kernel32', use_last_error=True)
user32.OpenClipboard.argtypes = [w.HWND]
user32.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
user32.SetClipboardData.restype = w.HANDLE
kernel32.GlobalAlloc.argtypes = [w.UINT, c.c_size_t]
kernel32.GlobalAlloc.restype = w.HGLOBAL
kernel32.GlobalLock.argtypes = [w.HGLOBAL]
kernel32.GlobalLock.restype = c.c_void_p
kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
kernel32.GlobalFree.argtypes = [w.HGLOBAL]


def copy_text(text):
    if not isinstance(text, str) or len(text) > 1_000_000:
        raise ValueError('无法复制这段文字')
    data = c.create_unicode_buffer(text)
    size = c.sizeof(data)
    for _ in range(10):  # another program may hold the clipboard briefly
        if user32.OpenClipboard(None):
            break
        time.sleep(.03)
    else:
        raise OSError('剪贴板正被其它程序占用，请稍后再试')
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not handle:
            raise OSError('剪贴板内存不足')
        target = kernel32.GlobalLock(handle)
        c.memmove(target, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise OSError('复制失败')
    finally:
        user32.CloseClipboard()
