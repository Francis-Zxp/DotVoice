# SPDX-License-Identifier: GPL-3.0-only
"""Hanvon PV60 protocol adapter; native mouse/keyboard interfaces stay untouched.

Protocol constants and challenge algorithm adapted from Remote Bridge Hub
(xxb26553663-star, GPL-3.0), commit 8a93f321ac71a602300c6cd77f7256fa4b63068e.
See design/PV60.md and licenses/remote-bridge-hub-GPL-3.0.txt.
Import and enumeration never open a HID handle. Only explicit start() writes.
"""
import queue
import threading
import time

VID, PID, INTERFACE, USAGE_PAGE = 0x0611, 0x3001, 5, 0xFF00
MICROPHONE_NAME = "Ai Pointer"
KEEPALIVE_SECONDS = 0.2
WARMUP_SECONDS = 1.0
OPEN_DELAY_SECONDS = 0.08
ACK_SECONDS = 1.0


def _report(hex_head):
    return bytes.fromhex(hex_head).ljust(33, b"\0")


SESSION_PING = _report("00 01 00 00 00 00 03 18 01")
MIC_ON = _report("00 06 00 00 00 00 08 04 00 01")
MIC_REFRESH = _report("00 06 00 00 00 00 03 0a 01")
DYNAMIC_ACK = bytes.fromhex("06 00 00 00 00 02 05 11")
SESSION_ACK = bytes.fromhex("01 00 00 00 00 03 18 01")
MIC_ACK = bytes.fromhex("06 00 00 00 00 08 04 10 01")


def dynamic_report(seed):
    """Reproduce the receiver's documented MSVC-rand challenge (33 bytes)."""
    state = int(seed) & 0xFFFFFFFF
    values = []
    for _ in range(2):
        state = (state * 214013 + 2531011) & 0xFFFFFFFF
        values.append((state >> 16) & 0x7FFF)
    token = (values[0] << 16) | values[1]
    signed = token if token < 0x80000000 else token - 0x100000000
    quotient = signed // 200 if signed >= 0 else -((-signed) // 200)
    checksum = (token + quotient * 0x38) & 0xFF
    return (bytes.fromhex("00 06 00 00 00 00 02 05 01")
            + bytes([checksum]) + token.to_bytes(4, "little")).ljust(33, b"\0")


def matches_interface(device):
    return all(device.get(key) == value for key, value in (
        ("vendor_id", VID), ("product_id", PID),
        ("interface_number", INTERFACE), ("usage_page", USAGE_PAGE)))


def list_devices(hid_module=None):
    """Read-only enumeration; never calls open/write and rejects other products."""
    if hid_module is None:
        import hid as hid_module
    return [device for device in hid_module.enumerate(VID, PID)
            if matches_interface(device)]


def available():
    return len(list_devices()) == 1


def open_transport():
    import hid
    devices = list_devices(hid)
    if len(devices) != 1:
        raise RuntimeError("请连接一个汉王 PV60 接收器；未发现唯一的 0611:3001 / MI_05 接口")
    handle = hid.device()
    try:
        handle.open_path(devices[0]["path"])
        handle.set_nonblocking(0)
        return devices[0], handle
    except Exception:
        handle.close()
        raise


def parse_key(report):
    """Return (keycode, down) for a well-formed key report, else None."""
    if (len(report) < 7 or report[:5] != bytes.fromhex("0b 30 58 00 10")
            or report[6] not in (0, 1)):
        return None
    return report[5], report[6] == 1


def voice_button(report):
    """Return down/up only for the voice key; malformed/page/mouse reports None."""
    key = parse_key(report)
    if key is None or key[0] != 23:
        return None
    return key[1]


class PV60Session:
    """One thread owns handshake, reads and keepalive. No automatic reconnect.

    start() waits for required ACKs; handshake-time button presses are discarded.
    on_button(bool) runs on the worker and should only enqueue an action.
    refresh() is synchronous: it returns after refresh + ON were written, or
    raises. close() cancels waits, stops writes and releases the HID handle.
    There is no verified MIC_OFF command; close is not a radio-power-off claim.
    """
    def __init__(self, on_button, on_error, transport_factory=None):
        self.on_button, self.on_error = on_button, on_error
        self.transport_factory = transport_factory or open_transport
        self.stopped = threading.Event()
        self.ready = threading.Event()
        self.requests = queue.Queue(maxsize=5)
        self.thread = None
        self.error = None
        self.device = None
        self.handle = None
        self.last_down = False
        self.last_keys = {}
        self.on_key = None  # optional (keycode, down) for non-voice keys
        self.next_ping = None
        self.ticks = 0

    def start(self):
        if self.thread is not None:
            raise RuntimeError("PV60 会话不可重复启动；请创建新的会话")
        self.thread = threading.Thread(target=self._run, name="pv60-hid", daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            self.close()
            raise RuntimeError("PV60 麦克风握手超时")
        if self.error:
            self.close()
            raise RuntimeError(self.error)
        if self.stopped.is_set():
            raise RuntimeError("PV60 连接已取消")

    def _write(self, report):
        if self.stopped.is_set():
            raise RuntimeError("PV60 连接已取消")
        if self.handle.write(report) != len(report):
            raise RuntimeError("PV60 HID 指令未完整写入；已关闭本次会话")

    def _keepalive(self):
        now = time.monotonic()
        if self.next_ping is not None and now >= self.next_ping:
            self._write(SESSION_PING)
            self.ticks += 1
            if self.ticks % 10 == 0:
                self._write(MIC_REFRESH)
                self._write(MIC_ON)
            self.next_ping = now + KEEPALIVE_SECONDS

    def _read(self):
        self._keepalive()
        return bytes(self.handle.read(64, 20))

    def _ack(self, prefix, timeout, required=True):
        end = time.monotonic() + timeout
        while not self.stopped.is_set() and time.monotonic() < end:
            if self._read().startswith(prefix):
                return
        if self.stopped.is_set() or required:
            raise RuntimeError("PV60 麦克风握手未收到确认；本次连接已取消")

    def _delay(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.stopped.wait(min(0.02, max(0, end - time.monotonic()))):
                raise RuntimeError("PV60 连接已取消")
            self._keepalive()

    def refresh(self, timeout=2):
        if not self.ready.is_set() or self.stopped.is_set() or self.error:
            raise RuntimeError("PV60 麦克风会话尚未就绪")
        if threading.current_thread() is self.thread:
            raise RuntimeError("PV60 refresh 必须从控制器线程调用")
        done, outcome = threading.Event(), []
        self.requests.put_nowait((done, outcome))
        if not done.wait(timeout):
            # Do not leave a delayed write queued after the caller has cancelled.
            self.close()
            raise RuntimeError("PV60 开麦刷新超时；已关闭会话")
        if outcome:
            raise RuntimeError(outcome[0])

    def _run(self):
        try:
            self.device, self.handle = self.transport_factory()
            if not matches_interface(self.device):
                raise RuntimeError("拒绝向非 PV60 厂商接口发送指令")
            self._write(dynamic_report(int(time.time())))
            self._ack(DYNAMIC_ACK, ACK_SECONDS)
            self._delay(OPEN_DELAY_SECONDS)
            self._write(SESSION_PING)
            self._ack(SESSION_ACK, 0.25, required=False)
            self.next_ping = time.monotonic() + KEEPALIVE_SECONDS
            self._delay(WARMUP_SECONDS + OPEN_DELAY_SECONDS)
            self._write(MIC_ON)
            self._ack(MIC_ACK, ACK_SECONDS)
            self.ready.set()
            while not self.stopped.is_set():
                try:
                    done, outcome = self.requests.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        self._write(MIC_REFRESH)
                        self._write(MIC_ON)
                    except Exception as exc:
                        outcome.append(str(exc))
                        raise
                    finally:
                        done.set()
                key = parse_key(self._read())
                if key is None:
                    continue
                code, down = key
                if code == 23:
                    if down != self.last_down:
                        self.last_down = down
                        self.on_button(down)
                elif down != self.last_keys.get(code, False):
                    # Page keys keep their native keyboard path; this only observes.
                    self.last_keys[code] = down
                    if self.on_key:
                        self.on_key(code, down)
        except Exception as exc:
            self.error = str(exc)
            if not self.stopped.is_set():
                self.on_error(self.error)
        finally:
            self.stopped.set()
            if self.handle is not None:
                try:
                    self.handle.close()
                except Exception:
                    pass
            while True:
                try:
                    done, outcome = self.requests.get_nowait()
                except queue.Empty:
                    break
                outcome.append(self.error or "PV60 会话已关闭")
                done.set()
            self.ready.set()

    def close(self):
        self.stopped.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(3)
            if self.thread.is_alive():
                raise RuntimeError("PV60 HID 线程尚未退出，请勿启动第二个会话")
