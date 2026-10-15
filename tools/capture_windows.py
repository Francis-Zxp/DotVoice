"""Capture DotVoice's own top-level windows (PrintWindow) for visual checks.

Only windows owned by DotVoice.exe processes are captured; nothing else on the
screen is read. Usage: python tools/capture_windows.py OUT_DIR [--pid N]
"""
import ctypes as c
import ctypes.wintypes as w
from pathlib import Path
import struct
import subprocess
import sys
import zlib

user32 = c.WinDLL('user32', use_last_error=True)
gdi32 = c.WinDLL('gdi32')
user32.SetProcessDpiAwarenessContext(c.c_void_p(-4))  # per-monitor v2: physical pixels

EnumProc = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)


def dotvoice_pids():
    out = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq DotVoice.exe', '/FO', 'CSV', '/NH'],
                         capture_output=True, text=True).stdout
    return {int(line.split('","')[1]) for line in out.splitlines() if line.startswith('"DotVoice.exe"')}


def windows(pids):
    found = []

    def visit(hwnd, _):
        pid = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, c.byref(pid))
        if pid.value in pids:
            title = c.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, title, 256)
            rect = w.RECT()
            user32.GetWindowRect(hwnd, c.byref(rect))
            found.append((hwnd, title.value, bool(user32.IsWindowVisible(hwnd)),
                          (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)))
        return True
    user32.EnumWindows(EnumProc(visit), 0)
    return found


def png(path, width, height, bgra):
    rows = b''.join(b'\x00' + bytes(bgra[y * width * 4:(y + 1) * width * 4]) for y in range(height))
    # BGRA -> RGBA per pixel
    data = bytearray(rows)
    for y in range(height):
        base = y * (width * 4 + 1) + 1
        for x in range(width):
            i = base + x * 4
            data[i], data[i + 2] = data[i + 2], data[i]
    def chunk(kind, body):
        return struct.pack('>I', len(body)) + kind + body + struct.pack('>I', zlib.crc32(kind + body) & 0xffffffff)
    Path(path).write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
                           + chunk(b'IDAT', zlib.compress(bytes(data), 6)) + chunk(b'IEND', b''))


class BITMAPINFOHEADER(c.Structure):
    _fields_ = [('biSize', w.DWORD), ('biWidth', w.LONG), ('biHeight', w.LONG), ('biPlanes', w.WORD),
                ('biBitCount', w.WORD), ('biCompression', w.DWORD), ('biSizeImage', w.DWORD),
                ('biXPelsPerMeter', w.LONG), ('biYPelsPerMeter', w.LONG), ('biClrUsed', w.DWORD),
                ('biClrImportant', w.DWORD)]


def capture(hwnd, width, height):
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    info = BITMAPINFOHEADER(c.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
    bits = c.c_void_p()
    bitmap = gdi32.CreateDIBSection(dc, c.byref(info), 0, c.byref(bits), None, 0)
    old = gdi32.SelectObject(dc, bitmap)
    ok = user32.PrintWindow(hwnd, dc, 2)  # PW_RENDERFULLCONTENT
    data = c.string_at(bits, width * height * 4)
    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen)
    return ok, data


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else '.')
    out.mkdir(parents=True, exist_ok=True)
    pids = dotvoice_pids()
    for index, (hwnd, title, visible, (x, y, width, height)) in enumerate(windows(pids)):
        print(f'{hwnd:#x} visible={visible} {width}x{height}@{x},{y} {title!r}')
        if not visible or width <= 0 or height <= 0 or width * height > 40_000_000:
            continue
        ok, data = capture(hwnd, width, height)
        name = out / f'win-{index}-{(title or "untitled").replace(" ", "_")[:24]}.png'
        png(name, width, height, data)
        print('  ->', name, 'ok' if ok else 'PrintWindow failed')


if __name__ == '__main__':
    main()
