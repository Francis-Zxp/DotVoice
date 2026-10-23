"""Backend boundary. OpenLess performs insertion; the bridge never types twice."""
from abc import ABC, abstractmethod
import json
import os
from pathlib import Path
import re
import ssl
import struct
import time
from urllib.parse import urlparse
import uuid
import websocket

# OpenLess quit, crashed or restarted (e.g. for an update) without closing the socket cleanly.
LOST = 'OpenLess 连接中断（可能已退出或重启）；不会重放本段音频'
UNKNOWN_REPLY = 'OpenLess 返回了无法识别的数据，此版本可能需要更新协议适配'


class BackendBusy(RuntimeError):
    """The recognizer is serving another session; retrying later is safe."""


class VoiceBackend(ABC):
    @abstractmethod
    def connect(self): ...
    @abstractmethod
    def start(self): ...
    @abstractmethod
    def feed(self, pcm): ...
    @abstractmethod
    def stop(self): ...
    @abstractmethod
    def poll(self): ...
    @abstractmethod
    def cancel(self): ...
    @abstractmethod
    def close(self): ...


def audio_frame(session_id, sequence, pcm):
    if not pcm or len(pcm) % 2 or len(pcm) > 65536:
        raise ValueError("PCM 必须为非空 S16LE 音频，单帧不超过 64 KiB")
    return b"OL20" + uuid.UUID(session_id).bytes + struct.pack(">Q", sequence) + pcm


class OpenLessBackend(VoiceBackend):
    def __init__(self, url="wss://127.0.0.1:8443/ws", pin_file=None, preferences_file=None):
        parsed = urlparse(url)
        if parsed.scheme != "wss" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("OpenLess 地址必须是本机 wss 连接")
        self.url = url
        self.preferences_file = Path(preferences_file or Path(os.environ.get('APPDATA', '')) /
                                     'OpenLess' / 'preferences.json')
        self.follow_local_port = url == 'wss://127.0.0.1:8443/ws'
        self.pin_file = Path(pin_file or Path(os.environ.get("APPDATA", "")) /
                             "com.openless.app" / "remote-input-pin.txt")
        self.ws = None
        self.session_id = None
        self.sequence = 0
        self.last_ping = 0
        self.insert_text = True

    def endpoint(self):
        """Re-read the local app's port on every reconnect; never scan the LAN."""
        if self.follow_local_port and self.preferences_file.exists():
            try:
                prefs = json.loads(self.preferences_file.read_text(encoding='utf-8-sig'))
            except (OSError, ValueError):
                raise RuntimeError('无法读取 OpenLess 远程输入配置，请在 OpenLess 中检查设置') from None
            if prefs.get('remoteInputEnabled') is False:
                raise RuntimeError('OpenLess 的远程输入已关闭，请在其设置中开启')
            port = prefs.get('remoteInputPort', 8443)
            if type(port) is not int or not 1 <= port <= 65535:
                raise RuntimeError('OpenLess 远程输入端口无效')
            return f'wss://127.0.0.1:{port}/ws'
        return self.url

    def connect(self):
        if self.ws:
            return
        endpoint = self.endpoint()
        try:
            pin = self.pin_file.read_text(encoding="utf-8-sig").strip()
        except OSError:
            raise RuntimeError('未找到 OpenLess 配对信息，请先启动 OpenLess 并开启远程输入') from None
        if not re.fullmatch(r"\d{6}", pin):
            raise RuntimeError("OpenLess 配对信息无效，请检查远程输入设置")
        # OpenLess uses a per-installation self-signed certificate. This exception
        # is restricted above to loopback; the pairing PIN is never logged.
        try:
            ws = websocket.create_connection(endpoint, timeout=5,
                    sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False},
                    http_no_proxy=["127.0.0.1", "localhost", "::1"])
        except (OSError, websocket.WebSocketException):
            raise RuntimeError('OpenLess 远程输入暂不可达；保持启用时将自动重连') from None
        try:
            # "partials": opt into live transcript frames (design/openless-live-text);
            # OpenLess versions without that support ignore the extra field.
            try:
                ws.send(json.dumps({"type": "hello", "pin": pin, "partials": True}))
                auth = json.loads(ws.recv())
            except (websocket.WebSocketTimeoutException, TimeoutError):
                raise RuntimeError('OpenLess 没有回应配对请求（超时），请确认 OpenLess 已启动') from None
            except (websocket.WebSocketException, OSError):
                raise RuntimeError('OpenLess 远程输入暂不可达；保持启用时将自动重连') from None
            except ValueError:
                auth = None
            if not isinstance(auth, dict) or auth.get('type') != 'auth':
                raise RuntimeError('OpenLess 握手格式已变化，此版本需要更新协议适配')
            if not auth.get("ok"):
                raise RuntimeError("OpenLess 认证失败，请检查远程输入设置")
            ws.settimeout(0.02)
            self.ws = ws
            self.last_ping = time.monotonic()
        except Exception:
            try:
                ws.close()
            except Exception:
                pass   # the error above is the one to report
            raise

    def _send(self, kind):
        if not self.ws:
            raise RuntimeError("OpenLess 未连接")
        self._write(json.dumps({"type": kind}))

    def _write(self, data, **options):
        try:
            self.ws.send(data, **options)
        except websocket.WebSocketTimeoutException:
            raise RuntimeError('OpenLess 没有及时接收数据（可能卡住）；不会重放本段音频') from None
        except (websocket.WebSocketException, OSError):
            raise RuntimeError(LOST) from None

    def start(self):
        self.connect()
        if self.session_id:
            raise RuntimeError("上一段识别尚未结束")
        # Ordered on the same socket before start. This is per-connection, so
        # it does not alter OpenLess shortcuts, settings, or other clients.
        self._write(json.dumps({"type": "set_insert", "value": self.insert_text}))
        self._send("start")
        deadline = time.monotonic() + 9
        while time.monotonic() < deadline:
            event = self.poll()
            if not event:
                continue
            if event.get("type") == "started":
                session_id = str(uuid.UUID(event["sessionId"]))
                self.session_id, self.sequence = session_id, 0
                return
            if event.get("type") == "busy":
                raise BackendBusy("OpenLess 正忙，请等待当前识别完成后重新按键")
            if event.get("kind") == "error":
                raise RuntimeError(event.get("message") or "OpenLess 开始录音失败")
        # The server may have accepted start even if its acknowledgement was lost.
        self._send("cancel")
        raise RuntimeError("OpenLess 未确认开始；本段已取消，请重新录音")

    def feed(self, pcm):
        if not self.session_id:
            raise RuntimeError("没有已确认的录音会话")
        self._write(audio_frame(self.session_id, self.sequence, pcm), opcode=websocket.ABNF.OPCODE_BINARY)
        self.sequence += 1

    def stop(self):
        if self.session_id:
            self._send("stop")

    def poll(self):
        if not self.ws:
            return None
        try:
            if time.monotonic() - self.last_ping > 20:
                self.last_ping = time.monotonic()   # (a ping that timed out is not retried every poll)
                self.ws.ping()
            value = self.ws.recv()
        except websocket.WebSocketTimeoutException:
            return None
        except (websocket.WebSocketException, OSError):
            raise RuntimeError(LOST) from None
        if value == "" or value is None:
            raise RuntimeError(LOST)
        if not isinstance(value, str):
            return None
        try:
            event = json.loads(value)
        except ValueError:
            event = None
        if not isinstance(event, dict):
            raise RuntimeError(UNKNOWN_REPLY)
        if event.get("type") == "result" or event.get("kind") == "error":
            self.session_id = None
        return event

    def cancel(self):
        try:
            if self.ws:
                self._send("cancel")
        finally:
            self.session_id = None

    def close(self):
        if self.ws:
            self.ws.close(timeout=1)
            self.ws = None
        self.session_id = None
