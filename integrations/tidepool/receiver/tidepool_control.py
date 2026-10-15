"""Authenticated loopback control; all device mutations stay on owner threads."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import queue
import re
import secrets
import subprocess
import threading
import time

ACTIONS = frozenset(("pause", "resume", "cancel", "wake", "sleep"))


def parse_battery(text):
    """'TP_BAT 87 1 4012' -> (87, True, 4012); anything malformed -> None."""
    fields = text.split()
    if not 3 <= len(fields) <= 4 or fields[0] != "TP_BAT":
        return None
    try:
        percent, charging = int(fields[1]), fields[2]
        millivolts = int(fields[3]) if len(fields) == 4 else None
    except ValueError:
        return None
    if not 0 <= percent <= 100 or charging not in ("0", "1"):
        return None
    if millivolts is not None and not 2500 <= millivolts <= 4500:
        millivolts = None
    return percent, charging == "1", millivolts


class ReceiverControl:
    """Called by the receiver main loop; worker completion uses only an Event."""
    def __init__(self, sessions, link, bridge, version, port):
        self.sessions, self.link, self.bridge = sessions, link, bridge
        self.version, self.port = version, port
        self.seen = {"usb": None, "wifi": None}
        self.cancel_done = threading.Event()
        self.cancel_done.set()
        self.cancel_error = None
        self.cancel_generation = 0
        self.battery = None  # (percent, charging, millivolts, wall-clock seconds)
        self.firmware = None  # device firmware version from TP_BOOT / TP_INFO

    def observe(self, transport):
        self.seen[transport] = time.monotonic()

    def observe_battery(self, text):
        """TP_BAT <percent> <charging 0|1> [millivolts]; firmware sends it only on change."""
        battery = parse_battery(text)
        if battery is not None:
            self.battery = battery + (time.time(),)
        return battery is not None

    def observe_firmware(self, version):
        version = str(version or "").strip()
        if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.\-+]{0,31}", version):
            self.firmware = version
            return True
        return False

    def state(self, transport):
        last = self.seen.get(transport)
        usb = bool(self.link and self.link.alive())
        online = last is not None and time.monotonic() - last < 10
        battery = self.battery
        return dict(receiver_running=True, receiver_version=self.version, receiver_pid=os.getpid(),
                    port=self.port, enabled=self.sessions.enabled,
                    recording=bool(self.sessions.active and self.sessions.started),
                    device_online=online, usb_connected=usb, link=transport if online else "offline",
                    battery_percent=battery[0] if battery else None,
                    battery_charging=battery[1] if battery else None,
                    battery_millivolts=battery[2] if battery else None,
                    battery_reported_at=battery[3] if battery else None,
                    firmware_version=self.firmware,
                    control_supported=True,
                    cancel_pending=not self.cancel_done.is_set(), cancel_error=self.cancel_error,
                    cancel_generation=self.cancel_generation,
                    asr=self.bridge.health() if self.bridge else "unknown",
                    message="现有 Tidepool 接收链路" if battery
                    else "现有 Tidepool 接收链路；设备电量尚未上报（需固件 4.0.6）")

    def ready_for_audio(self):
        return self.sessions.enabled and self.cancel_done.is_set() and not self.cancel_error

    def apply(self, request):
        if request.action == "resume":
            if not self.cancel_done.is_set() or self.cancel_error:
                request.finish(False, error="Cancellation not yet confirmed")
                return
            self.sessions.enabled = True
            request.finish(True, enabled=True, cancel_pending=False)
        elif request.action in ("pause", "cancel"):
            if request.action == "pause":
                self.sessions.enabled = False  # Both USB and UDP share this gate.
            if self.cancel_done.is_set():
                self.cancel_generation += 1
                self.cancel_error = None
                self.cancel_done.clear()
                def completed(error):
                    self.cancel_error = error
                    self.cancel_done.set()
                self.sessions.cancel(completed)
            request.finish(True, enabled=self.sessions.enabled, cancel_pending=not self.cancel_done.is_set(),
                           cancel_generation=self.cancel_generation)
        elif request.action in ("wake", "sleep"):
            if self.link:
                self.link.control(request)  # The serial owner thread completes it.
            else:
                request.finish(False, error="USB link unavailable")
        else:
            request.finish(False, error="Action not allowed")


class ControlRequest:
    def __init__(self, action, timeout=2):
        self.action = action
        self.deadline = time.monotonic() + timeout
        self.done = threading.Event()
        self.result = None

    def expired(self):
        return self.done.is_set() or time.monotonic() >= self.deadline

    def finish(self, ok, **values):
        self.result = dict(ok=ok, action=self.action, **values)
        self.done.set()


def write_private_endpoint(path, data):
    """Create an empty private file before placing a bearer token inside it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + secrets.token_hex(6))
    try:
        descriptor = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        if os.name == "nt":
            result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                    capture_output=True, text=True, check=True,
                                    creationflags=subprocess.CREATE_NO_WINDOW, timeout=3)
            import re
            match = re.search(r"S-1-5-[0-9-]+", result.stdout)
            if not match:
                raise RuntimeError("Cannot identify current Windows user")
            subprocess.run(["icacls", str(temporary), "/inheritance:r", "/grant:r",
                            "*" + match.group(0) + ":(F)"],
                           capture_output=True, check=True, timeout=3,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        temporary.write_text(json.dumps(data), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class ControlServer:
    def __init__(self, endpoint_path, writer=write_private_endpoint):
        self.endpoint_path = Path(endpoint_path)
        self.token = secrets.token_urlsafe(32)
        self.requests = queue.Queue(maxsize=8)
        self.lock = threading.Lock()
        self.snapshot = {}
        self.updated = 0
        self.server = None
        self.thread = None
        self.writer = writer

    def publish(self, snapshot):
        with self.lock:
            self.snapshot, self.updated = dict(snapshot), time.monotonic()

    def status(self):
        with self.lock:
            result, age = dict(self.snapshot), time.monotonic() - self.updated
        result["fresh"] = age < 2
        result["snapshot_age_seconds"] = round(age, 3)
        if not result["fresh"]:
            result.update(device_online=False, usb_connected=False)
        return result

    def drain(self, handler):
        for _ in range(8):
            try:
                request = self.requests.get_nowait()
            except queue.Empty:
                break
            if request.expired():
                request.finish(False, error="expired")
                continue
            try:
                handler(request)
            except Exception as exc:
                request.finish(False, error=str(exc))

    def start(self):
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, *args):
                pass  # Never log bearer tokens, headers or transcripts.

            def reply(self, code, value):
                body = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def authorized(self):
                expected = "Bearer " + owner.token
                return (self.client_address[0] == "127.0.0.1"
                        and self.headers.get("Origin") is None
                        and self.headers.get("Host") == "127.0.0.1:%d" % owner.server.server_port
                        and hmac.compare_digest(self.headers.get("Authorization", "").encode("utf-8"),
                                                expected.encode("ascii")))

            def do_GET(self):
                if not self.authorized():
                    return self.reply(403, {"ok": False, "error": "unauthorized"})
                if self.path != "/v1/status":
                    return self.reply(404, {"ok": False, "error": "not-found"})
                self.reply(200, dict(ok=True, schema=1, **owner.status()))

            def do_POST(self):
                if not self.authorized():
                    return self.reply(403, {"ok": False, "error": "unauthorized"})
                if self.path != "/v1/control":
                    return self.reply(404, {"ok": False, "error": "not-found"})
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 256 or self.headers.get("Content-Type") != "application/json":
                        raise ValueError("invalid-body")
                    data = json.loads(self.rfile.read(length))
                    if set(data) != {"action"} or data["action"] not in ACTIONS:
                        raise ValueError("invalid-action")
                    request = ControlRequest(data["action"])
                    owner.requests.put_nowait(request)
                except (ValueError, TypeError, queue.Full):
                    return self.reply(400, {"ok": False, "error": "invalid-or-busy"})
                if not request.done.wait(2.2):
                    request.finish(False, error="expired")
                self.reply(200 if request.result["ok"] else 409, request.result)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        try:
            self.writer(self.endpoint_path, {"schema": 1, "url": "http://127.0.0.1:%d" % self.server.server_port,
                                             "token": self.token, "pid": os.getpid()})
        except Exception:
            self.server.server_close()
            raise
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="tp-control-http", daemon=True)
        self.thread.start()

    def close(self):
        if self.thread:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(1)
        try:
            data = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
            if data.get("token") == self.token:
                self.endpoint_path.unlink()
        except (OSError, ValueError):
            pass
