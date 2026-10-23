"""Control the existing Tidepool receiver through its authenticated loopback API.

Legacy installations remain read-only and explicitly require the API update.
This adapter never binds UDP, opens serial/audio or starts another receiver.
"""
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def probe_udp_owner(port):
    """Read Windows UDP ownership; hidden subprocess, no changes to the owner."""
    port = int(port)
    if not 1 <= port <= 65535:
        raise ValueError("Tidepool UDP 端口无效")
    script = (
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
        f"$items=@(Get-NetUDPEndpoint -ErrorAction Stop | Where-Object LocalPort -eq {port} | "
        "ForEach-Object { $proc=Get-CimInstance Win32_Process "
        "-Filter ('ProcessId=' + $_.OwningProcess); "
        "[pscustomobject]@{pid=$_.OwningProcess;exe=$proc.ExecutablePath;"
        "command=$proc.CommandLine} }); ConvertTo-Json -Compress -InputObject $items"
    )
    result = subprocess.run(
        ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", timeout=8,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=True,
    )
    value = json.loads(result.stdout.strip() or "[]")
    return value if isinstance(value, list) else [value]


def matches_receiver(owner, receiver_path):
    executable = ntpath.basename(str(owner.get("exe") or "")).casefold()
    if executable not in ("python.exe", "pythonw.exe"):
        return False
    command = str(owner.get("command") or "").replace("/", "\\")
    expected = str(receiver_path).replace("/", "\\")
    return bool(re.search(r'(?:^|[\s"])' + re.escape(expected) + r'(?=[\s"]|$)',
                          command, flags=re.IGNORECASE))


class TidepoolAdapter:
    """snapshot() is read-only. Call off the UI thread and cache for ~10 seconds."""
    def __init__(self, install_root=None, port_probe=None, request=None):
        self.root = Path(install_root or Path(os.environ.get("LOCALAPPDATA", "")) / "Tidepool")
        self.port_probe = port_probe or probe_udp_owner
        self.request = request or self._request
        self.presenter_pause = None

    def _request(self, method, route, payload=None):
        try:
            endpoint = json.loads((self.root / "control" / "endpoint.json").read_text(encoding="utf-8"))
            parsed = urllib.parse.urlparse(endpoint["url"])
            token = endpoint["token"]
            if (endpoint.get("schema") != 1 or parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
                    or not parsed.port or parsed.username or parsed.password or parsed.path
                    or parsed.query or parsed.fragment or not isinstance(token, str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)):
                raise ValueError("invalid endpoint")
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *args, **kwargs):
                    return None
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
            data = json.dumps(payload).encode("utf-8") if payload is not None else None
            request = urllib.request.Request(endpoint["url"] + route, data=data, method=method,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with opener.open(request, timeout=3) as response:
                raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("oversized response")
            result = json.loads(raw)
            if not isinstance(result, dict) or not result.get("ok"):
                raise ValueError("unsuccessful response")
            if method == "GET" and result.get("receiver_pid") != endpoint.get("pid"):
                raise ValueError("receiver identity changed")
            return result
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise RuntimeError("Tidepool 控制接口不可用；需要更新并启用接收器控制 API") from exc

    def snapshot(self):
        receiver = self.root / "receiver" / "tidepool_receiver.py"
        result = {
            "id": "tidepool", "name": "Tidepool OS", "installed": receiver.is_file(),
            "receiver_version": None, "receiver_running": None, "receiver_pid": None,
            "port": None, "port_conflict": False, "device_online": None,
            "battery_percent": None, "battery_charging": None, "battery_reported_at": None, "firmware_version": None, "recording": None, "control_supported": False,
            "enabled": None, "usb_connected": False, "link": "unknown",
            "cancel_pending": None, "cancel_error": None, "api_upgrade_required": True,
            "audio_owner": "external_receiver", "log_updated_at": None,
            "message": "设备在线、录音和电量尚无可查询接口；现有接收器独立负责语音",
        }
        if not result["installed"]:
            result["message"] = "未发现已安装的 Tidepool 接收器"
            return result
        try:
            with receiver.open("r", encoding="utf-8-sig") as source:
                version = re.search(r'^VERSION\s*=\s*[\"\']([0-9.]+)[\"\']',
                                    source.read(12000), flags=re.MULTILINE)
            result["receiver_version"] = version.group(1) if version else None
            config = json.loads((self.root / "config.json").read_text(encoding="utf-8-sig"))
            port = int(config["port"])
            if not 1 <= port <= 65535:
                raise ValueError("端口超出范围")
            result["port"] = port
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result["message"] = f"无法读取 Tidepool 安装信息：{type(exc).__name__}"
            return result
        log = self.root / "logs" / "rx.log"
        try:
            result["log_updated_at"] = datetime.fromtimestamp(
                log.stat().st_mtime, timezone.utc).isoformat()
        except OSError:
            pass
        try:
            live = self.request("GET", "/v1/status")
        except RuntimeError as exc:
            # Only a missing or incompatible API needs the receiver update; a refused or
            # timed-out connection, or a receiver that just restarted, is temporary.
            cause = exc.__cause__
            legacy = isinstance(cause, (FileNotFoundError, KeyError, urllib.error.HTTPError)) or (
                isinstance(cause, ValueError) and str(cause) in ("invalid endpoint", "unsuccessful response"))
            result["api_upgrade_required"] = legacy
            result["message"] = ("现有接收器需更新并启用控制 API；目前仅可查看进程，不能控制设备" if legacy
                                 else "暂时连不上 Tidepool 接收器的控制接口，稍后自动重试")
        else:
            result["api_upgrade_required"] = False
            if live.get("fresh"):
                keys = ("receiver_running", "receiver_version", "receiver_pid", "port", "enabled",
                        "recording", "device_online", "usb_connected", "link", "battery_percent",
                        "battery_charging", "battery_reported_at", "firmware_version", "control_supported", "cancel_pending", "cancel_error", "asr", "message")
                result.update({key: live[key] for key in keys if key in live})
                return result
            result["message"] = "Tidepool 接收器状态未更新，请检查设备与接收器"
        try:
            owners = self.port_probe(port)
            matches = [owner for owner in owners if matches_receiver(owner, receiver)]
            result["receiver_running"] = bool(matches)
            if len(matches) == 1:
                result["receiver_pid"] = int(matches[0]["pid"])
            result["port_conflict"] = bool(owners) and len(matches) != len(owners)
            if result["port_conflict"]:
                result["message"] = "配置端口由其它进程占用；DotVoice 不会抢占端口"
            elif not matches:
                result["message"] = "未检测到现有接收器监听；DotVoice 不会启动第二个接收器"
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            result["message"] = "无法核实接收器进程；设备状态保持未知"
        return result

    def control(self, action, temporary=False):
        if action not in ("pause", "resume", "cancel", "wake", "sleep"):
            raise RuntimeError("不支持的 Tidepool 控制操作")
        if action in ("pause", "resume") and not temporary:
            self.presenter_pause = None  # An explicit user choice takes precedence.
        before = self.request("GET", "/v1/status")
        if not before.get("fresh") or not before.get("control_supported"):
            raise RuntimeError("Tidepool 控制接口尚未就绪")
        result = self.request("POST", "/v1/control", {"action": action})
        if not result.get("ok"):
            raise RuntimeError("Tidepool 未确认控制操作")
        if action in ("pause", "cancel"):
            until = time.monotonic() + 15
            while time.monotonic() < until:
                state = self.request("GET", "/v1/status")
                if not state.get("fresh") or state.get("cancel_error"):
                    raise RuntimeError("Tidepool 取消会话未成功，不能切换录音设备")
                if (state.get("cancel_generation") == result.get("cancel_generation")
                        and state.get("cancel_pending") is False
                        and (action != "pause" or state.get("enabled") is False)):
                    if action == "pause" and temporary:
                        prior = (before.get("receiver_pid"), before.get("cancel_generation"))
                        if before.get("enabled") is True or self.presenter_pause == prior:
                            self.presenter_pause = (state.get("receiver_pid"), state.get("cancel_generation"))
                    return {"ok": True, "action": action, "enabled": state.get("enabled"), "cancel_pending": False}
                time.sleep(0.1)
            raise RuntimeError("Tidepool 取消会话超时；保持暂停，未启动其它录音设备")
        if action == "resume":
            until = time.monotonic() + 5
            while time.monotonic() < until:
                state = self.request("GET", "/v1/status")
                if state.get("cancel_error"):
                    raise RuntimeError("Tidepool 会话取消存在错误，无法确认恢复")
                if (state.get("fresh") and state.get("enabled") is True
                        and state.get("cancel_pending") is False):
                    return {"ok": True, "action": action, "enabled": True, "cancel_pending": False}
                time.sleep(0.1)
            raise RuntimeError("Tidepool 恢复尚未获得新状态确认；请刷新状态后重试")
        return result

    def restore_presenter_pause(self):
        """Restore only our temporary pause on the same receiver/session generation."""
        owner = self.presenter_pause
        if owner is None:
            return False
        state = self.request("GET", "/v1/status")
        if not state.get("fresh"):
            return False  # cannot tell yet: the marker stays for the next try
        if (owner[0] is not None and owner[1] is not None
                and owner == (state.get("receiver_pid"), state.get("cancel_generation"))
                and state.get("enabled") is False and state.get("cancel_pending") is False
                and not state.get("cancel_error")):
            self.control("resume", temporary=True)  # raises: the marker stays for the next try
            self.presenter_pause = None
            return True
        self.presenter_pause = None  # not ours any more (resumed, another session or receiver)
        return False
