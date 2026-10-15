"""Observe only the Baseus receiver. Never suppress or inject input events.

The one exception to "only the receiver": while the shortcut guard runs, other
keyboards' modifier keys (Shift/Ctrl/Alt/Win up/down, never any other key) are
passed to `on_foreign_key`, so the guard knows a Ctrl+P typed on a real keyboard
is not the pen's."""
import ctypes as c
from ctypes import wintypes as w
import struct
import threading
import time

VID_PID = "VID_ABC8&PID_CA08"
MODIFIER_VKS = frozenset((0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5))
RID_INPUT, RID_HEADER = 0x10000003, 0x10000005
WM_INPUT, WM_INPUT_DEVICE_CHANGE, WM_TIMER = 0xFF, 0xFE, 0x113
WM_APP_MOUSE = 0x8001
RIDEV_REMOVE, RIDEV_INPUTSINK, RIDEV_DEVNOTIFY = 0x1, 0x100, 0x2000
FALLBACK_SCAN_MS = 30000  # device changes arrive as WM_INPUT_DEVICE_CHANGE
user32 = c.WinDLL("user32", use_last_error=True)
kernel32 = c.WinDLL("kernel32", use_last_error=True)
LRESULT = c.c_ssize_t
WNDPROC = c.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)


class WindowClass(c.Structure):
    _fields_ = [("style", w.UINT), ("proc", WNDPROC), ("extra", c.c_int),
                ("window_extra", c.c_int), ("instance", w.HINSTANCE),
                ("icon", w.HICON), ("cursor", w.HANDLE), ("background", w.HBRUSH),
                ("menu", w.LPCWSTR), ("name", w.LPCWSTR)]


class RawDevice(c.Structure):
    _fields_ = [("handle", w.HANDLE), ("kind", w.DWORD)]


class Registration(c.Structure):
    _fields_ = [("page", w.USHORT), ("usage", w.USHORT),
                ("flags", w.DWORD), ("target", w.HWND)]


class Header(c.Structure):
    _fields_ = [("kind", w.DWORD), ("size", w.DWORD),
                ("device", w.HANDLE), ("param", w.WPARAM)]


class DeviceInfo(c.Structure):
    _fields_ = [("size", w.DWORD), ("kind", w.DWORD), ("data", w.DWORD * 6)]


user32.GetRawInputDeviceList.argtypes = [c.c_void_p, c.POINTER(w.UINT), w.UINT]
user32.GetRawInputDeviceInfoW.argtypes = [w.HANDLE, w.UINT, c.c_void_p, c.POINTER(w.UINT)]
user32.GetRawInputData.argtypes = [w.HANDLE, w.UINT, c.c_void_p, c.POINTER(w.UINT), w.UINT]
user32.RegisterRawInputDevices.argtypes = [c.POINTER(Registration), w.UINT, w.UINT]
user32.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassW.argtypes = [c.POINTER(WindowClass)]
user32.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
                                  c.c_int, c.c_int, c.c_int, c.c_int,
                                  w.HWND, w.HMENU, w.HINSTANCE, c.c_void_p]
user32.CreateWindowExW.restype = w.HWND
user32.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
user32.DestroyWindow.argtypes = [w.HWND]
user32.UnregisterClassW.argtypes = [w.LPCWSTR, w.HINSTANCE]
user32.SetTimer.argtypes = [w.HWND, c.c_size_t, w.UINT, c.c_void_p]
kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
kernel32.GetModuleHandleW.restype = w.HMODULE


def device_name(handle):
    size = w.UINT()
    user32.GetRawInputDeviceInfoW(handle, 0x20000007, None, c.byref(size))
    buf = c.create_unicode_buffer(size.value + 1)
    if user32.GetRawInputDeviceInfoW(handle, 0x20000007, buf, c.byref(size)) == 0xFFFFFFFF:
        return ""
    return buf.value.upper()


def devices(all_supported=False):
    count = w.UINT()
    user32.GetRawInputDeviceList(None, c.byref(count), c.sizeof(RawDevice))
    entries = (RawDevice * count.value)()
    result = user32.GetRawInputDeviceList(entries, c.byref(count), c.sizeof(RawDevice))
    if result == 0xFFFFFFFF:
        raise c.WinError(c.get_last_error())
    found = []
    for entry in entries[:count.value]:
        name = device_name(entry.handle)
        if VID_PID not in name and not (all_supported and 'VID_0611&PID_3001' in name):
            continue
        info = DeviceInfo(size=c.sizeof(DeviceInfo))
        size = w.UINT(c.sizeof(info))
        user32.GetRawInputDeviceInfoW(entry.handle, 0x2000000B, c.byref(info), c.byref(size))
        page, usage = (1, 2) if entry.kind == 0 else (1, 6)
        if entry.kind == 2:
            page, usage = info.data[3] & 0xFFFF, info.data[3] >> 16
        found.append({"handle": entry.handle, "path": name, "kind": entry.kind,
                      "page": page, "usage": usage})
    return found


def parse_mouse(data):
    """RAWMOUSE: usFlags, (pad), usButtonFlags, usButtonData, ulRawButtons, lLastX, lLastY."""
    if len(data) < 20:
        return None
    flags, buttons, _, _, dx, dy = struct.unpack_from('<H2xHHIii', data)
    return {'dx': dx, 'dy': dy, 'buttons': buttons, 'absolute': bool(flags & 1)}


def parse_keyboard(data):
    """RAWKEYBOARD: MakeCode, Flags, Reserved, VKey, Message. RI_KEY_BREAK = 1."""
    if len(data) < 12:
        return None
    make, flags, _, vk, message = struct.unpack_from('<HHHHI', data)
    return {'vk': vk, 'make': make, 'down': not flags & 1, 'message': message}


class RawMonitor:
    def __init__(self, callback, error_callback=lambda message: None, devices_callback=None):
        self.callback, self.error_callback = callback, error_callback
        self.devices_callback = devices_callback
        self.hwnd = None
        self.ready = threading.Event()
        self.thread = None
        self.paths = {}
        self.known = {}
        self.registered = set()
        self.mouse_handles = set()
        self.want_mouse = False
        self.mouse_registered = False
        self.on_foreign_key = None  # (vk, down) for other keyboards' modifiers; set by the core
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="baseus-hid", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("HID 监听器启动超时")
        if self.error:
            raise RuntimeError(self.error)

    def set_mouse(self, enabled):
        """Mouse raw input is registered only while the air-mouse remap needs it."""
        self.want_mouse = bool(enabled)
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_MOUSE, 0, 0)

    def _register(self):
        current = devices(all_supported=True)
        baseus = [d for d in current if VID_PID in d["path"]]
        self.paths = {d["handle"]: d["path"] for d in baseus}
        self.known = dict(self.paths)
        self.mouse_handles = {d["handle"] for d in baseus if d["kind"] == 0}
        # Raw Input is registered by usage, then filtered by receiver identity.
        # No RIDEV_NOLEGACY: native mouse/keyboard delivery remains intact.
        usages = {(d["page"], d["usage"]) for d in baseus if d["kind"] != 0}
        for page, usage in usages - self.registered:
            flags = RIDEV_INPUTSINK | RIDEV_DEVNOTIFY | (0x20 if usage == 0 else 0)
            reg = Registration(page, usage, flags, self.hwnd)
            if not user32.RegisterRawInputDevices(c.byref(reg), 1, c.sizeof(reg)):
                raise c.WinError(c.get_last_error())
            self.registered.add((page, usage))
        self._apply_mouse()
        if self.devices_callback:
            self.devices_callback(current)

    def _apply_mouse(self):
        want = self.want_mouse and bool(self.mouse_handles)
        if want and not self.mouse_registered:
            reg = Registration(1, 2, RIDEV_INPUTSINK | RIDEV_DEVNOTIFY, self.hwnd)
            if not user32.RegisterRawInputDevices(c.byref(reg), 1, c.sizeof(reg)):
                raise c.WinError(c.get_last_error())
            self.mouse_registered = True
        elif not want and self.mouse_registered:
            reg = Registration(1, 2, RIDEV_REMOVE, None)
            user32.RegisterRawInputDevices(c.byref(reg), 1, c.sizeof(reg))
            self.mouse_registered = False

    def _input(self, raw_handle):
        # Header first: other keyboards/mice are rejected without copying data.
        header = Header()
        size = w.UINT(c.sizeof(Header))
        if user32.GetRawInputData(raw_handle, RID_HEADER, c.byref(header), c.byref(size),
                                  c.sizeof(Header)) == 0xFFFFFFFF:
            return
        path = self.known.get(header.device)
        if path is None:
            # Injected input has no device; unknown devices are looked up once.
            path = device_name(header.device) if header.device else ""
            if len(self.known) < 512:
                self.known[header.device] = path
        foreign = self.on_foreign_key
        if VID_PID not in path:
            if header.kind == 1 and foreign is not None and header.device:  # (injected input has no device)
                data = self._data(raw_handle)
                key = parse_keyboard(data) if data else None
                if key and key['vk'] in MODIFIER_VKS:
                    foreign(key['vk'], key['down'])
            return
        data = self._data(raw_handle)
        if not data:
            return
        now = time.monotonic()
        if header.kind == 0:
            mouse = parse_mouse(data)
            if mouse:
                self.callback({"time": now, "path": path, "kind": 0, "hex": data.hex(), "mouse": mouse})
            return
        if header.kind == 1:
            key = parse_keyboard(data)
            if key:
                self.callback({"time": now, "path": path, "kind": 1, "hex": data.hex(), "keyboard": key})
            return
        if header.kind == 2 and len(data) >= 8:
            unit = int.from_bytes(data[:4], "little")
            count = int.from_bytes(data[4:8], "little")
            if not unit or 8 + unit * count > len(data):
                return
            reports = [data[8 + i * unit:8 + (i + 1) * unit] for i in range(count)]
        else:
            reports = [data]
        for report in reports:
            self.callback({"time": now, "path": path,
                           "kind": int(header.kind), "hex": report.hex()})

    @staticmethod
    def _data(raw_handle):
        size = w.UINT()
        user32.GetRawInputData(raw_handle, RID_INPUT, None, c.byref(size), c.sizeof(Header))
        if not size.value:
            return b''
        buf = c.create_string_buffer(size.value)
        result = user32.GetRawInputData(raw_handle, RID_INPUT, buf, c.byref(size), c.sizeof(Header))
        if result == 0xFFFFFFFF:
            return b''
        return buf.raw[c.sizeof(Header):result]

    def _run(self):
        instance = kernel32.GetModuleHandleW(None)
        class_name = "BaseusVoiceHid" + str(id(self))
        @WNDPROC
        def proc(hwnd, msg, wp, lp):
            try:
                if msg == WM_INPUT:
                    self._input(lp)
                elif msg in (WM_INPUT_DEVICE_CHANGE, WM_TIMER):
                    self._register()
                elif msg == WM_APP_MOUSE:
                    self._apply_mouse()
                elif msg == 0x10:
                    user32.DestroyWindow(hwnd)
                    return 0
                elif msg == 2:
                    user32.PostQuitMessage(0)
                    return 0
            except Exception as exc:
                self.error_callback(str(exc))
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        self._proc = proc
        wc = WindowClass(proc=proc, instance=instance, name=class_name)
        try:
            if not user32.RegisterClassW(c.byref(wc)):
                raise c.WinError(c.get_last_error())
            self.hwnd = user32.CreateWindowExW(0, class_name, "", 0, 0, 0, 0, 0,
                                              w.HWND(-3), None, instance, None)
            if not self.hwnd:
                raise c.WinError(c.get_last_error())
            self._register()
            user32.SetTimer(self.hwnd, 1, FALLBACK_SCAN_MS, None)
            self.ready.set()
            message = w.MSG()
            while user32.GetMessageW(c.byref(message), None, 0, 0) > 0:
                user32.TranslateMessage(c.byref(message))
                user32.DispatchMessageW(c.byref(message))
        except Exception as exc:
            self.error = str(exc)
            self.error_callback(self.error)
            self.ready.set()
        finally:
            for page, usage in self.registered | ({(1, 2)} if self.mouse_registered else set()):
                reg = Registration(page, usage, RIDEV_REMOVE, None)
                user32.RegisterRawInputDevices(c.byref(reg), 1, c.sizeof(reg))
            user32.UnregisterClassW(class_name, instance)

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, 0x10, 0, 0)
        if self.thread:
            self.thread.join(3)
