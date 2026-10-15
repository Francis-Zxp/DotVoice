"""WinMM capture of the named Baseus input. PCM stays in memory."""
import ctypes as c
from ctypes import wintypes as w
import threading
import time

RATE = 16000
CHUNK_BYTES = 1600  # 50 ms, S16LE mono
STALL_CHECK = 2.0      # s without a buffer: make sure the receiver is still plugged in
STALL_LIMIT = 8.0      # s without a buffer from a receiver that is still there: give up
PRESENCE_EVERY = 10.0  # s between identity re-checks while data flows


class Caps(c.Structure):
    _fields_ = [("mid", w.WORD), ("pid", w.WORD), ("version", w.DWORD),
                ("name", w.WCHAR * 32), ("formats", w.DWORD),
                ("channels", w.WORD), ("reserved", w.WORD)]


class Format(c.Structure):
    _pack_ = 2
    _fields_ = [("tag", w.WORD), ("channels", w.WORD), ("rate", w.DWORD),
                ("avg", w.DWORD), ("align", w.WORD), ("bits", w.WORD), ("extra", w.WORD)]


class Header(c.Structure):
    _fields_ = [("data", c.c_void_p), ("length", w.DWORD), ("recorded", w.DWORD),
                ("user", c.c_size_t), ("flags", w.DWORD), ("loops", w.DWORD),
                ("next", c.c_void_p), ("reserved", c.c_size_t)]


win = c.WinDLL("winmm")
win.waveInGetDevCapsW.argtypes = [c.c_size_t, c.POINTER(Caps), w.UINT]
win.waveInMessage.argtypes = [c.c_void_p, w.UINT, c.c_size_t, c.c_size_t]
win.waveInMessage.restype = w.UINT
DRV_QUERYDEVICEINTERFACE, DRV_QUERYDEVICEINTERFACESIZE = 0x80C, 0x80D
SETTLE = 0.5  # seconds dropped after opening: the open can produce a transient
win.waveInOpen.argtypes = [c.POINTER(c.c_void_p), w.UINT, c.POINTER(Format),
                         c.c_size_t, c.c_size_t, w.DWORD]
for name in ["waveInPrepareHeader", "waveInAddBuffer", "waveInUnprepareHeader"]:
    getattr(win, name).argtypes = [c.c_void_p, c.POINTER(Header), w.UINT]
for name in ["waveInStart", "waveInReset", "waveInClose"]:
    getattr(win, name).argtypes = [c.c_void_p]


def check(result, operation):
    if result:
        raise RuntimeError(f"倍思音频 {operation} 失败，Windows 错误 {result}")


def interface_path(index):
    """Device interface path behind a waveIn index (no device is opened), or None."""
    try:
        size = w.ULONG()
        if win.waveInMessage(c.c_void_p(index), DRV_QUERYDEVICEINTERFACESIZE, c.addressof(size), 0) or not size.value:
            return None
        buffer = c.create_string_buffer(size.value + 2)
        if win.waveInMessage(c.c_void_p(index), DRV_QUERYDEVICEINTERFACE, c.addressof(buffer), size.value):
            return None
        return c.wstring_at(c.addressof(buffer)) or None
    except (OSError, ValueError):
        return None


def wave_inputs():
    """[(index, name)] for every WinMM input."""
    found = []
    for index in range(win.waveInGetNumDevs()):
        caps = Caps()
        if win.waveInGetDevCapsW(index, c.byref(caps), c.sizeof(caps)) == 0:
            found.append((index, caps.name))
    return found


def find_device(device='baseus', parent=None, tree=None):
    """The microphone of the receiver `parent` (its USB instance id) when given and
    mappable; otherwise the single microphone whose name matches, as before."""
    from .devices import get_profile
    from .rawinput import devices
    profile = get_profile(device)
    if not any(profile['identity'] in d['path'] for d in devices(all_supported=True)):
        raise RuntimeError('未连接所选设备：' + profile['name'])
    inputs = wave_inputs()
    named = [(index, name) for index, name in inputs
             if profile['microphone'].casefold() in name.casefold()]
    if parent:
        from .instances import usb_parent
        # Same-named inputs first (~5 ms per lookup); a renamed endpoint is still found.
        for group in (named, [item for item in inputs if item not in named]):
            mine = []
            for index, name in group:
                path = interface_path(index)
                if path and usb_parent(path, profile['identity'], tree) == parent.upper():
                    mine.append((index, name))
            if len(mine) == 1:
                return mine[0]
    matches = named
    if len(matches) != 1:
        if parent and len(matches) > 1:
            raise RuntimeError('插着 %d 个接收器，无法确定这支笔的麦克风；请拔下其他接收器后重试' % len(matches))
        raise RuntimeError('未发现唯一的 ' + profile['microphone'] + ' 麦克风；不会改用系统默认输入')
    return matches[0]


class StallWatch:
    """When a capture that stopped returning buffers has failed.

    Any 2 s without a buffer used to cancel the session outright, so a brief stall (a
    USB hiccup, the receiver re-syncing with a pen at the edge of its range) threw away
    a recording that was going fine. Unplugging is still caught within STALL_CHECK: the
    presence check runs as soon as buffers stop, then once a second. A receiver that is
    still plugged in gets until STALL_LIMIT to resume. `present` raises when it is gone.
    """

    def __init__(self, present, clock=time.monotonic):
        self.present, self.clock = present, clock
        self.last_data = self.last_presence = clock()

    def data(self):
        self.last_data = self.clock()

    def check(self):
        now = self.clock()
        silent = now - self.last_data
        if silent > STALL_CHECK:
            # First look as soon as the stall is noticed, then once a second.
            due = (self.last_presence < self.last_data + STALL_CHECK
                   or now - self.last_presence >= 1.0)
        else:
            due = now - self.last_presence >= PRESENCE_EVERY
        if due:
            self.last_presence = now
            self.present()
        if silent > STALL_LIMIT:
            raise RuntimeError("倍思麦克风停止返回数据，请重连接收器")


class AudioCapture:
    def __init__(self, callback, on_error, device='baseus', target=None, settle=SETTLE):
        self.callback, self.on_error = callback, on_error
        self.device = device
        # target = {'id', 'parent'} from bridge.instances: record from that receiver.
        self.target = target
        self.parent = (target or {}).get('parent')
        self.settle = settle
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.error = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name="baseus-audio", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            self.close()
            raise RuntimeError("倍思麦克风启动超时")
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        handle = c.c_void_p()
        headers, buffers = [], []
        try:
            index, _ = find_device(self.device, self.parent)
            fmt = Format(1, 1, RATE, RATE * 2, 2, 16, 0)
            check(win.waveInOpen(c.byref(handle), index, c.byref(fmt), 0, 0, 0), "open")
            for _ in range(8):
                buf = c.create_string_buffer(CHUNK_BYTES)
                buffers.append(buf)
                header = Header(c.addressof(buf), CHUNK_BYTES, 0, 0, 0, 0, None, 0)
                check(win.waveInPrepareHeader(handle, c.byref(header), c.sizeof(header)), "prepare")
                headers.append(header)
                check(win.waveInAddBuffer(handle, c.byref(header), c.sizeof(header)), "queue")
            check(win.waveInStart(handle), "start")
            began = time.monotonic()
            # Unplugging is caught within STALL_CHECK; the periodic identity re-check
            # only guards a different device reusing the name.
            watch = StallWatch(self._check_present)
            self.ready.set()
            # WinMM fills input buffers in the order they were queued: take them in that order
            # too, or after a stall (6, 7 and 0 done) the newest 50 ms would be sent first.
            cursor = 0
            while not self.stopped.is_set():
                for _ in headers:   # at most one lap per pass
                    header = headers[cursor]
                    if not header.flags & 1:
                        break
                    pcm = c.string_at(header.data, header.recorded)
                    watch.data()
                    # Device opening can generate a transient; don't send it as speech.
                    if watch.last_data - began >= self.settle and pcm:
                        self.callback(pcm)
                    header.recorded = 0
                    check(win.waveInAddBuffer(handle, c.byref(header), c.sizeof(header)), "requeue")
                    cursor = (cursor + 1) % len(headers)
                watch.check()
                self.stopped.wait(0.005)
        except Exception as exc:
            self.error = str(exc)
            self.ready.set()
            if not self.stopped.is_set():
                self.on_error(self.error)
        finally:
            if handle.value:
                win.waveInReset(handle)
                for header in headers:
                    win.waveInUnprepareHeader(handle, c.byref(header), c.sizeof(header))
                win.waveInClose(handle)

    def _check_present(self):
        """Raise when this receiver is gone or another device took the microphone's name."""
        if self.parent:
            from .instances import node_present
            if not node_present(self.parent):
                raise RuntimeError("这支笔的接收器已拔出")
            return
        try:
            find_device(self.device)
        except OSError:
            # Raw Input enumeration can fail while another device arrives; that is not
            # proof of removal, and STALL_LIMIT still ends a capture that stays silent.
            pass

    def close(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(3)
            if self.thread.is_alive():
                raise RuntimeError('麦克风尚未完全释放，请稍后重试')
