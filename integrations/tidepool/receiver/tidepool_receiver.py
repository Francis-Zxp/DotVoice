#!/usr/bin/env python3
"""Tidepool 电脑端接收器 v4.0.4 (M7) —— 交接文档 §7 的电脑侧。

职责：
  · UDP 17875 监听，收 TP_HELLO 回 TP_READY，收心跳
  · 【v4.0.2】USB 线有线链路(USB-Serial-JTAG，VID 303A PID 1001)：在线时优先走它，
    拔线/没装 pyserial 就退回 Wi-Fi/UDP。协议见 SerialLink 的说明。
  · 收 [seq][640B PCM] 音频流，过 3 包 jitter buffer，写进虚拟声卡
  · 【v4.0.2】TP_START <session> replay：设备补发的旧录音只落盘到 <outdir>/replay/，
    绝不送进 OpenLess/虚拟声卡(那会把过期的听写打进当前焦点窗口)
  · 每约 1 秒回 TP_STATUS vmic=.. asr=.. hk=..（三段真实探测，探不到就报 unknown）
  · 每约 2 秒回 TP_AGENT（助手会话真实状态）+ TP_PROJ（最近项目）
  · 每 30 分钟拉 Open-Meteo，下发 TP_WEATHER / TP_SUN
  · 【v4.0.2】每 60 秒下发 TP_TIME <unix_ms> <POSIX TZ>（本机时区）；
    --auto-location 时按 Windows 定位 → IP 定位 → 命令行坐标 自动选天气地点

【诚实边界】
  · vmic/asr/hk 三段只在真的探测成功时才报 ok。没装 VB-CABLE 就报 missing，
    不报 ok —— 设备上的状态环宁可显示"未知"，也不能显示一个假的"正常"。
  · 没装 sounddevice 时音频只落盘成 .wav，状态照实报 vmic=nodriver。
  · 没配 --lat/--lon 就不发 TP_WEATHER，设备会显示"天气未配置"。
  · 助手状态见下面 "助手真实状态" 一节。哪一项读不到，对应的键就【不发】，
    设备显示"未上报"。尤其是额度：本机没有任何一个文件写着剩余额度，
    所以 quota 这个键永远不出现 —— 编一个 78% 比空着坏得多。

【助手真实状态的来源，逐项】
  会话在不在       ~/.claude/sessions/<pid>.json，再用 OpenProcess + 进程创建
                   时间核对 pid 没被回收。文件在但进程死了 = 不算在跑。
  忙/闲            上面那个文件里的 status 字段（Claude Code 自己写的）。
                   只认 busy/idle，别的字符串一律报 unknown，不硬塞。
  模型             会话记录 ~/.claude/projects/*/<sessionId>.jsonl 里最后一条
                   assistant 消息的 message.model —— 这一轮真用的那个模型。
  会话名/时长      同一个 sessions/<pid>.json 的 name / startedAt。
  今日会话数       ~/.claude/projects/*/*.jsonl 里今天被改过的文件个数
                   （一个文件 = 一个会话）。
  最近项目         Claude：~/.claude.json 的 projects 键（真实绝对路径），
                   新旧看该项目会话记录的最后修改时间。
                   Codex：~/.codex/sessions/**/rollout-*.jsonl 第一行
                   session_meta 里的 cwd。
  额度             【无来源】。永远报未知。
  项目状态         只分得清"这个目录正是活着的会话的 cwd"(run)和"分不清"。
                   待授权/已完成/出错都无从判断，所以从不发这三种。

依赖（可选，缺了会降级而不是崩）：
  pip install sounddevice numpy      # 写虚拟声卡
  pip install pyserial               # USB 线有线链路；没有就只走 Wi-Fi/UDP
只用标准库也能跑：音频会存成 wav 文件。
"""
import argparse
import base64
import binascii
import calendar
from collections import deque
import ctypes
import datetime
import glob
import json
import math
import os
import re
import queue
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.parse
import uuid
import wave

PORT = 17875   # 默认值；--port 可改，见下方说明
FRAME_BYTES = 640          # 320 样本 × 16bit，单声道
FRAME_BYTES2 = 1280        # 320 样本 × 2 声道 × 16bit，双声道原始素材
RATE = 16000
JITTER = 3                 # 3 包 = 60ms，文档建议值
JITTER_MAX = 12            # 缓冲硬上限(240ms)：超过就强制出包，不让延迟无限堆积
WORKER_QUEUE_MAX = 750     # 网络线程→输出线程的待处理帧上限(15 秒)。v4.0.4：OpenLess 开始握手
                           # 重试期间(最多约 9 秒)音频都积在这里，开始成功后顺序补喂，不丢字
GAP_FILL_MAX = 10          # 最多补 10 包(200ms)静音；更大的缺口直接重同步
SILENCE = b"\x00" * FRAME_BYTES
VERSION = "4.0.8"

# ---- USB 有线链路(v4.0.2) ----
# 串口对端没有 IP。给它一个假地址，这样 say(payload, to)、AudioSession 的
# (session, peer) 键、peer 变量全都不用改形状 —— 只有 say() 需要认识它。
USB_ADDR = ("usb", 0)
USB_VID, USB_PID = 0x303A, 0x1001      # Espressif USB-Serial-JTAG
SERIAL_BAUD = 115200                   # USB-Serial-JTAG 忽略波特率，但 pyserial 要一个值
SERIAL_LINE_MAX = 8 * 1024             # 单行上限；正常最长约 2KB(双声道 base64)
SERIAL_TX_MAX = 600                    # "TPSHOT link " 之后的最大字节数(固件缓冲区)
SERIAL_ALIVE_S = 5.0                   # 5 秒内收到过 @@TP 行 = 有线对端在线
SERIAL_RETRY_S = 2.0
SERIAL_INBOX_MAX = 1500                # 约 30 秒音频；满了就反压(停读)，不无限堆
SERIAL_PAUSE_FILE = os.path.join(tempfile.gettempdir(), "tidepool_serial.pause")

try:
    import sounddevice as sd
except Exception:
    sd = None

try:
    import websocket
except Exception:
    websocket = None

try:
    import serial
    import serial.tools.list_ports
except Exception:
    serial = None


# ---------------------------------------------------------------- 状态探测
def refresh_audio_devices():
    """让 PortAudio 重新枚举声卡。

    【为什么非做不可】PortAudio 在 Pa_Initialize() 时把设备列表缓存下来，
    之后再也不会更新。电脑睡眠/恢复、插拔耳机、驱动重载之后，索引会整体错位 ——
    而进程里查到的还是旧表。于是 sd.RawOutputStream(device=13) 打开的是
    另一个完全不相干的设备，write() 照样返回成功，音频写进虚空。

    这不是假设：2026-09-20 实测到接收器连续几次"写入虚拟声卡 device=13、
    无任何错误"，而 CABLE Output 那头是 -90dBFS 的死寂，OpenLess 收到 6.68s
    全零、报 emptyTranscript。重启接收器进程立刻恢复。
    日志里一切正常、功能却坏掉，是最难查的一类故障，必须从根上堵。
    """
    if sd is None:
        return
    try:
        sd._terminate()
        sd._initialize()
    except Exception as exc:
        print("[audio] 重新枚举声卡失败(继续用旧表): %s" % exc)


def probe_vmic(device_name):
    """能否真的往虚拟声卡写 16kHz 单声道。

    【为什么不能只看枚举】同一块虚拟声卡会以多个宿主 API 出现：
    MME / DirectSound 接受 16kHz 单声道(内部重采样)，WASAPI 共享模式只认
    设备原生的 48kHz，直接报 Invalid sample rate。光"枚举到"就报 ok，
    到真正录音时才失败 —— 那种"正常"是假的。所以这里试开一次再下结论。
    另外要按前缀精确匹配："CABLE In 16ch" 不是我们要的那个端点。
    """
    if sd is None:
        return "nodriver", None
    try:
        devices = list(enumerate(sd.query_devices()))
    except Exception:
        return "error", None
    want = device_name.lower()
    cands = [(i, d) for i, d in devices
             if d["max_output_channels"] > 0 and d["name"].lower().startswith(want)]
    if not cands:
        cands = [(i, d) for i, d in devices
                 if d["max_output_channels"] > 0 and want in d["name"].lower()]
    if not cands:
        return "missing", None
    for i, _d in cands:
        try:
            st = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16", device=i)
            st.start(); st.stop(); st.close()
            return "ok", i
        except Exception:
            continue
    # 枚举得到、但没有一个能按我们的格式打开：这和"没装"是两回事，分开报。
    return "badformat", None


def probe_asr(process_name):
    """识别软件进程在不在。没给进程名就是 unknown，不猜。"""
    if not process_name:
        return "unknown"
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % process_name, "/NH"],
                                 capture_output=True, text=True, timeout=2).stdout
            return "ok" if process_name.lower() in out.lower() else "down"
        out = subprocess.run(["pgrep", "-f", "--", process_name], capture_output=True,
                             text=True, timeout=2).stdout.strip()
        return "ok" if out else "down"
    except Exception:
        return "error"


class HotkeyState:
    """全局快捷键。

    交接文档原本让接收器用 RegisterHotKey 抢注一个全局快捷键。但现在热键由
    【设备】通过 BLE HID 直接发给电脑 —— 接收器不需要、也不应该再去抢注：
    抢注会和电脑上其他程序冲突，而且注册失败还得处理。
    所以这里固定上报 bydevice，让设备端自己显示真实的热键状态。
    """
    status = "bydevice"


class SpeechAGC:
    """送识别软件的那一路做温和的自动增益(v4.0.3)。

    【为什么】设备端网罩用 dBFS 映射所以起伏明显；而 OpenLess 的波形按线性
    幅度画，正常说话只有 -30~-20 dBFS(满幅的 3%~10%)，看起来几乎是平的。
    识别本身不受影响，但用户会怀疑"是不是没收到声音"。
    做法：跟踪语音峰值包络，把它推到约 -6 dBFS；增益上限 +18dB，
    快压慢放，只在有语音时抬升(底噪门限 -50 dBFS)，末端软限幅，不削顶。
    只影响送进 OpenLess 的那份拷贝；WAV 证据和补发文件保持原始电平。"""
    TARGET = 0.5          # 约 -6 dBFS
    MAX_GAIN = 8.0        # +18 dB
    GATE = 0.003          # -50 dBFS

    def __init__(self):
        self.reset()

    def reset(self):
        self.gain = 2.0
        self.env = 0.0

    def process(self, pcm):
        try:
            import array
            a = array.array("h")
            a.frombytes(pcm)
        except Exception:
            return pcm
        if not a:
            return pcm
        peak = max(abs(min(a)), abs(max(a))) / 32768.0
        # 包络：上升快(一帧)，下降慢(约 1 秒)
        self.env = peak if peak > self.env else self.env * 0.97 + peak * 0.03
        if self.env > self.GATE:
            want = min(self.MAX_GAIN, max(1.0, self.TARGET / self.env))
            # 压得快(防爆音)，抬得慢(防呼吸感)
            k = 0.5 if want < self.gain else 0.04
            self.gain += (want - self.gain) * k
        g = self.gain
        out = array.array("h", bytes(len(a) * 2))
        for i, v in enumerate(a):
            x = v * g / 32768.0
            if x > 0.9 or x < -0.9:          # 软限幅：tanh 形状的平滑拐弯
                x = math.tanh(x)
            out[i] = int(max(-32767, min(32767, x * 32767)))
        return out.tobytes()


class OpenLessBusy(RuntimeError):
    """OpenLess 明说"忙"(上一段还没收尾)。连接本身是好的，稍等再试即可。"""


class OpenLessRemoteBridge:
    """One local, authenticated OpenLess remote-input connection.

    The old path wrote PCM to VB-CABLE while the watch independently emitted a
    toggle hotkey over BLE.  A delivered HID report does not prove that OpenLess
    changed state, so one missed edge permanently inverted the two UIs.  Remote
    Input has explicit start/stop messages and is therefore the primary path
    when enabled.  TLS verification is disabled only for loopback: OpenLess
    creates a per-installation CA and its listener never leaves this computer.
    """
    # v4.0.4：连接保活与开始握手。
    # 【v4.0.3 的真实故障】create_connection(timeout=3) 让监听线程的 recv() 每 3 秒
    # 没消息就抛超时 —— 监听线程把它当成断线退出，后台探测再重连。OpenLess 日志里
    # 是一整天每 3.1 秒一次「WS 连接已关闭 / 配对成功」(约 2.8 万次/天)。
    # 用户恰好在"这条连接快到 3 秒"时开录：start 刚发出去，监听线程就超时了，
    # 而它要改状态时又被 start() 持有的锁卡住，于是 3 秒后报
    # "did not confirm start"，整段话一个字都没进 OpenLess，设备回执 0 帧、
    # 显示"已安全存在本机"，只能再说一遍。实测约 12% 的录音因此失败。
    # 现在：recv 超时只是"醒来看看要不要发心跳"，不算断线；状态用独立的小锁；
    # 开始握手失败会换一条新连接重试，等待期间的音频由 AudioWorker 缓冲，不丢。
    RECV_POLL_S = 1.0          # 监听线程醒来检查心跳的间隔
    PING_EVERY_S = 20.0        # 空闲心跳：发现死连接，而不是靠超时猜
    START_TIMEOUT_S = 5.0      # 单次等 "started" 的上限(OpenLess 要先连云端 ASR)
    START_BUDGET_S = 9.0       # 整个开始握手(含重连、busy 重试)的总预算
    PROBE_BACKOFF_S = 5.0      # OpenLess 没开时，后台别每秒去敲一次门

    def __init__(self, url, pin_file):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "wss" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("OpenLess remote URL must be local wss://127.0.0.1 or localhost")
        self.url, self.pin_file = url, pin_file
        self.agc = SpeechAGC()
        self.ws = None
        self.session_id = None
        self.sequence = 0
        self.status = "offline"
        self.last_error = ""
        self.lock = threading.RLock()          # 连接与会话(ws 的使用权)
        self.status_lock = threading.Lock()    # 只护 status：监听线程绝不能等 self.lock
        self.events = queue.Queue()
        self.listener = None
        self.closed = False
        self.last_probe = float("-inf")
        self.feed_errors = 0

    def _set_status(self, value, error=""):
        with self.status_lock:
            self.status, self.last_error = value, error

    def health(self):
        with self.status_lock:
            return self.status

    @staticmethod
    def _is_timeout(exc):
        timeout_types = [socket.timeout, TimeoutError, queue.Empty]
        wte = getattr(websocket, "WebSocketTimeoutException", None) if websocket else None
        if isinstance(wte, type):
            timeout_types.append(wte)
        return isinstance(exc, tuple(timeout_types))

    def _pin(self):
        try:
            with open(self.pin_file, "r", encoding="utf-8") as f:
                pin = f.read().strip()
        except OSError as exc:
            raise RuntimeError("OpenLess remote pairing PIN unavailable: %s" % exc) from exc
        if not re.fullmatch(r"[0-9]{6}", pin):
            raise RuntimeError("OpenLess remote pairing PIN is invalid")
        return pin

    def _listen(self, ws, events):
        """只属于一条连接(ws/events 显式传进来)：换了连接，旧线程自己退出，
        绝不会把旧连接的错误塞进新连接的事件队列。"""
        last_ping = time.monotonic()
        while not self.closed and self.ws is ws:
            try:
                message = ws.recv()
            except Exception as exc:
                if self.closed or self.ws is not ws:
                    return
                if self._is_timeout(exc):
                    # 空闲不是断线。到点发一次心跳；心跳发不出去才是真断了。
                    if time.monotonic() - last_ping >= self.PING_EVERY_S:
                        last_ping = time.monotonic()
                        ping = getattr(ws, "ping", None)
                        if ping is not None:
                            try:
                                ping()
                            except Exception as ping_exc:
                                exc = ping_exc
                            else:
                                continue
                        else:
                            continue
                    else:
                        continue
                self._set_status("offline", str(exc) or type(exc).__name__)
                events.put({"type": "bridge-error", "reason": str(exc) or type(exc).__name__})
                return
            try:
                if message is None or message == "":
                    raise RuntimeError("OpenLess remote connection closed")
                if not isinstance(message, str):
                    continue
                event = json.loads(message)
            except Exception as exc:
                if isinstance(exc, ValueError):
                    continue            # 一条坏 JSON 不值得拆掉整条连接
                if not self.closed and self.ws is ws:
                    self._set_status("offline", str(exc))
                    events.put({"type": "bridge-error", "reason": str(exc)})
                return
            kind = event.get("kind") if event.get("type") == "status" else None
            if kind in ("recording", "transcribing", "polishing", "done", "error"):
                self._set_status(kind, event.get("message") or "")
            events.put(event)

    def _connect(self):
        if websocket is None:
            raise RuntimeError("websocket-client is not installed in the Tidepool Python runtime")
        if self.ws is not None and self.listener is not None and self.listener.is_alive():
            return
        self.close()
        self.closed = False
        events = queue.Queue()
        # This is deliberately loopback-only (validated in __init__).  The app
        # serves a self-signed installation CA, which normal Windows trust does
        # not expose to Python's cert store.
        ws = websocket.create_connection(
            self.url, timeout=5,
            sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})
        try:
            ws.send(json.dumps({"type": "hello", "pin": self._pin()}))
            auth = json.loads(ws.recv())
            if auth.get("type") != "auth" or not auth.get("ok"):
                raise RuntimeError("OpenLess remote authentication failed")
        except Exception:
            try:
                ws.close()
            except Exception:
                pass
            raise
        # 握手之后 recv 只用短超时"醒一下"，不再拿它判断断线。
        settimeout = getattr(ws, "settimeout", None)
        if settimeout is not None:
            try:
                settimeout(self.RECV_POLL_S)
            except Exception:
                pass
        self.ws, self.events = ws, events
        self._set_status("idle")
        self.listener = threading.Thread(target=self._listen, args=(ws, events),
                                         name="tp-openless-remote", daemon=True)
        self.listener.start()

    def probe(self, force=False):
        """Authenticate once at receiver startup so status is truthful before speech.
        之后只在真断线时由后台重连，而且至少隔 PROBE_BACKOFF_S 秒。"""
        now = time.monotonic()
        if not force and now - self.last_probe < self.PROBE_BACKOFF_S:
            return self.health() != "offline"
        self.last_probe = now
        # 正在开录/录音时锁被占着：不排队等，健康状态由那条路径自己更新。
        if not self.lock.acquire(timeout=0.2):
            return self.health() != "offline"
        try:
            self._connect()
            return True
        except Exception as exc:
            self._set_status("offline", str(exc))
            return False
        finally:
            self.lock.release()

    def _drain_events(self):
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                return

    def _wait_for_start(self, timeout=3.0):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            try:
                event = self.events.get(timeout=max(0.01, until - time.monotonic()))
            except queue.Empty:
                break
            if event.get("type") == "started":
                return event.get("sessionId")
            if event.get("type") == "busy":
                raise OpenLessBusy(event.get("reason") or "OpenLess remote is busy")
            if event.get("type") == "bridge-error":
                raise RuntimeError(event.get("reason") or "OpenLess remote connection lost")
        raise RuntimeError("OpenLess remote did not confirm start")

    def start(self, source_session, channels):
        """开始一段听写。失败不再一次定生死：
          · busy(上一段还在识别/润色)→ 同一连接上稍等再试
          · 没回执/连接坏了 → 丢掉这条连接，换新连接再试
        总预算 START_BUDGET_S。等待期间设备的音频积在 AudioWorker 队列里，
        开始成功后按顺序补喂给 OpenLess，一个字都不丢。"""
        if channels != 1:
            raise RuntimeError("OpenLess remote input requires mono PCM")
        began = time.monotonic()
        deadline = began + self.START_BUDGET_S
        attempt = 0
        last_exc = None
        while True:
            attempt += 1
            with self.lock:
                try:
                    self._connect()
                    self._drain_events()        # 上一段遗留的状态事件与这次无关
                    self.ws.send(json.dumps({"type": "start"}))
                    left = max(0.5, min(self.START_TIMEOUT_S, deadline - time.monotonic()))
                    session_id = self._wait_for_start(left)
                    try:
                        uuid.UUID(session_id)
                    except (ValueError, TypeError) as exc:
                        raise RuntimeError("OpenLess remote returned an invalid session id") from exc
                    self.session_id, self.sequence = session_id, 0
                    self.feed_errors = 0
                    self.agc.reset()
                    self._set_status("recording")
                    waited = time.monotonic() - began
                    print("[openless] remote start confirmed for device session=%s%s"
                          % (source_session,
                             "" if attempt == 1 and waited < 1.5
                             else "（第 %d 次尝试，用时 %.1fs，期间音频已缓冲）" % (attempt, waited)))
                    return
                except OpenLessBusy as exc:
                    last_exc = exc                # 连接是好的，不用重连
                    pause = 0.5
                except Exception as exc:
                    last_exc = exc
                    print("[openless] 开始握手第 %d 次失败(%s)，换一条新连接重试" % (attempt, exc))
                    self.close()
                    pause = 0.2
            if time.monotonic() + pause >= deadline:
                raise RuntimeError("OpenLess 没有接受开始指令(尝试 %d 次，%.1fs)：%s"
                                   % (attempt, time.monotonic() - began, last_exc))
            time.sleep(pause)

    def feed(self, pcm):
        pcm = self.agc.process(pcm)
        with self.lock:
            if not self.session_id or self.ws is None:
                raise RuntimeError("OpenLess remote stream is not active")
            if not pcm or len(pcm) > 64 * 1024:
                raise RuntimeError("invalid OpenLess remote PCM frame")
            frame = b"OL20" + uuid.UUID(self.session_id).bytes + struct.pack(">Q", self.sequence) + pcm
            try:
                self.ws.send(frame, opcode=websocket.ABNF.OPCODE_BINARY)
            except Exception as exc:
                self._set_status("offline", str(exc))
                raise
            self.sequence += 1

    def stop(self):
        with self.lock:
            if self.ws is None or not self.session_id:
                return
            self.ws.send(json.dumps({"type": "stop"}))
            self.session_id = None
            self._set_status("transcribing")
            print("[openless] remote stop sent")

    def cancel(self):
        """Cancel this connection's session, including an in-flight transcription."""
        with self.lock:
            try:
                if self.ws is not None and (self.session_id or self.health() in ("recording", "transcribing", "polishing")):
                    self.ws.send(json.dumps({"type": "cancel"}))
            finally:
                self.close()
                self._set_status("offline")

    def close(self):
        self.closed = True
        ws, self.ws = self.ws, None
        self.session_id = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


# ---------------------------------------------------------------- 音频出口
class AudioSink:
    """Write to the configured OpenLess path, with WAV evidence when requested."""

    def __init__(self, device_index, outdir, tee=False, remote=None):
        self.device_index = device_index
        self.outdir = outdir
        self.remote = remote
        # tee：送虚拟声卡的同时也落一份 WAV。
        # 【为什么需要】四点链路检查要比"接收器收到的"和"OpenLess 听到的"，
        # 而原来只有虚拟声卡打不开时才落盘 —— 正常工作时反而没有证据可查。
        self.tee = tee
        self.stream = None
        self.wav = None
        self.wav_file = None
        self.buf = []
        self.expected = None
        self.frames_in = 0     # 这一段实际收到的音频包数，用于给设备回执
        self.lost = 0
        self.late = 0
        self.dup = 0
        self.resync = 0
        self.channels = 1
        self.frame_bytes = FRAME_BYTES
        self.io_failed = False
        self.underflows = 0
        self.submitted_samples = 0
        self.remote_feed_failures = 0
        self.session = ""
        self.stats_reported = False
        # 补发(replay)会话：只落盘，不碰 OpenLess / 虚拟声卡。
        self.replay = False
        self.wav_path = None

    def _open_wav(self, directory, name, channels):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        try:
            self.wav_file = open(path, "xb")
        except FileExistsError:
            # 同一秒里同一个会话号又补发一次：绝不覆盖先到的那份。
            stem, ext = os.path.splitext(path)
            path = "%s-%s%s" % (stem, uuid.uuid4().hex[:8], ext)
            self.wav_file = open(path, "xb")
        self.wav = wave.open(self.wav_file, "wb")
        self.wav.setnchannels(channels)
        self.wav.setsampwidth(2)
        self.wav.setframerate(RATE)
        self.wav_path = path
        return path

    def start(self, session, channels=1, replay=False):
        self.stop()
        # 会话隔离：新的一段录音从头开始，不继承上一段的游标和统计。
        self.buf, self.expected = [], None
        self.frames_in = 0
        self.lost = self.late = self.dup = self.resync = 0
        self.io_failed = False
        self.underflows = 0
        self.submitted_samples = 0
        self.remote_feed_failures = 0
        self.session = session
        self.stats_reported = False
        self.channels = channels
        self.frame_bytes = FRAME_BYTES2 if channels == 2 else FRAME_BYTES
        self.replay = replay
        self.wav_path = None
        if replay:
            # 【补发的录音为什么绝不能进 OpenLess/虚拟声卡】它是设备上一次
            # 没送达、存下来的旧听写。现在灌进去，识别结果会被"打字"到此刻
            # 恰好有焦点的窗口里 —— 可能是聊天框、终端、别人的文档。
            # 所以补发只落盘，由用户自己决定要不要用。
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = self._open_wav(os.path.join(self.outdir, "replay"),
                                  "replay-%s-%s.wav" % (stamp, session), channels)
            print("[replay] 设备补发的录音 session=%s 只落盘，不送 OpenLess/虚拟声卡 → %s"
                  % (session, path))
            return
        if channels == 2:
            # 双声道是采素材用的：虚拟声卡那条链路要单声道，所以强制落盘。
            print("[audio] 双声道原始素材 —— 直接落盘，不进虚拟声卡")
        if self.remote is not None:
            try:
                self.remote.start(session, channels)
                # Do not also write to VB-CABLE: OpenLess receives this same
                # stream through its explicit session, and dual input would
                # create duplicate/competing recordings.
                return
            except Exception as exc:
                self.io_failed = True
                print("[openless] remote start failed: %s" % exc)
                return
        if channels == 1 and sd is not None and self.device_index is not None:
            try:
                self.stream = sd.RawOutputStream(samplerate=RATE, channels=1,
                                                 dtype="int16", device=self.device_index)
                self.stream.start()
                # 【把打开的到底是哪个设备打出来】只报索引没用 —— 索引错位时
                # 它看起来完全正常。名字对不上就能一眼看见。
                try:
                    opened = sd.query_devices(self.device_index)["name"]
                except Exception:
                    opened = "?"
                print("[audio] 写入虚拟声卡 device=%s (%s)" % (self.device_index, opened))
                if not self.tee:
                    return
            except Exception as exc:
                print("[audio] 虚拟声卡打开失败(%s)，改为落盘" % exc)
                if self.stream is not None:
                    try:
                        self.stream.close()
                    except Exception:
                        pass
                self.stream = None
        suffix = "-stereo" if channels == 2 else ""
        # Device session ids restart after reboot. Never overwrite an older take.
        path = self._open_wav(self.outdir, "session-%s%s-%s.wav" %
                              (session, suffix, uuid.uuid4().hex), channels)
        print("[audio] 落盘 %s" % path)

    def feed(self, seq, pcm):
        """抖动缓冲：按序号排 JITTER 包再出。

        【原来这里有三个确定的缺陷，离线用 0,1,2,3,4,1,5,6,7,8 复现过】
         1. 不丢弃已经播过的旧序号。迟到的重复包会被【再播一次】，
            而且 `self.expected = seq + 1` 会让播放游标【倒退】，
            后面所有正常包又都被当成"缺包"补零。
         2. 缺口补零没有上限：一个大缺口会一次性分配 frame_bytes*gap 字节，
            并把几秒静音灌进声卡，延迟从此再也追不回来。
         3. 重复包完全不计数，丢包统计显示 0，问题被彻底掩盖。

        现在：播放游标只前进；旧包/重复包直接丢弃并计数；缺口补偿有上限，
        超过上限就重新同步(跳过去)而不是灌一大段静音。
        """
        if len(pcm) != self.frame_bytes:
            return
        if any(s == seq for s, _ in self.buf):
            self.dup += 1
            return
        if self.expected is not None:
            if (seq - self.expected) & 0xFFFFFFFF >= 0x80000000:
                self.late += 1          # 已经播过了，丢掉，绝不回放
                return

        self.frames_in += 1       # 回执只计唯一、有效、未过期的帧
        self.buf.append((seq, pcm))
        anchor = self.expected if self.expected is not None else min(s for s, _ in self.buf)
        self.buf.sort(key=lambda item: (item[0] - anchor) & 0xFFFFFFFF)
        # 缓冲上限：抖动缓冲不是"无限缓存"，涨上去只会让延迟越积越多
        while len(self.buf) > JITTER_MAX:
            self._emit_one()
        while len(self.buf) > JITTER:
            self._emit_one()

    def _emit_one(self):
        seq, pcm = self.buf.pop(0)
        if self.expected is not None and seq != self.expected:
            gap = (seq - self.expected) & 0xFFFFFFFF
            if gap <= GAP_FILL_MAX:
                self.lost += gap
                self._out(bytes(self.frame_bytes * gap))   # 小缺口补静音
            else:
                # 大缺口：补一段就够了，剩下的直接重新同步。
                # 灌进去几秒静音只会让后面的语音一直延迟，识别反而更差。
                self.lost += gap
                self.resync += 1
                self._out(bytes(self.frame_bytes * GAP_FILL_MAX))
        self.expected = (seq + 1) & 0xFFFFFFFF
        self._out(pcm)

    def _out(self, data):
        # 【是 if 不是 elif】开了 --tee 时两条路都要走：声卡负责让 OpenLess
        # 听见，WAV 负责留下可以事后比对的证据。原来写成 elif，
        # 声卡一旦打开就永远落不了盘。
        # 补发会话(replay)跳过 remote：见 start() 里的说明。
        if self.remote is not None and not self.replay:
            try:
                self.remote.feed(data)
                self.submitted_samples += len(data) // (2 * self.channels)
            except Exception as exc:
                self.io_failed = True
                # 一段里只说头一次和最后的总数：原来每帧一行，一段话刷出几百行，
                # 把真正的第一条原因淹没掉。
                self.remote_feed_failures += 1
                if self.remote_feed_failures == 1:
                    print("[openless] remote PCM write failed: %s" % exc)
        elif self.stream is None and self.wav is None:
            self.io_failed = True
        if self.stream is not None:
            try:
                if self.stream.write(data):
                    self.underflows += 1
                    # PortAudio accepted this buffer. Underflow means it inserted
                    # a gap between writes, not that every received frame was lost.
                    # Keep a quality warning, but never trigger replay of a whole
                    # delivered recording solely because a gap was inserted.
                self.submitted_samples += len(data) // (2 * self.channels)
            except Exception as exc:
                self.io_failed = True
                print("[audio] 声卡写出失败: %s" % exc)
        if self.wav is not None:
            try:
                self.wav.writeframesraw(data)
            except Exception as exc:
                self.io_failed = True
                print("[audio] 文件写出失败: %s" % exc)

    def cancel(self):
        # Discard buffered PCM; cancellation must not finalize it as dictation.
        self.buf = []
        try:
            if self.remote is not None and not self.replay:
                self.remote.cancel()
        finally:
            if self.stream is not None:
                try:
                    self.stream.abort()
                except Exception:
                    pass
            self.stop()
            self.io_failed = True

    def stop(self):
        # 收尾也走同一条路径：原来这里直接把缓冲里的包按顺序倒出去，
        # 绕过了缺口补偿和游标维护，和正常播放的时基对不上。
        while self.buf:
            self._emit_one()
        if self.remote is not None and not self.replay:
            try:
                self.remote.stop()
            except Exception as exc:
                self.io_failed = True
                print("[openless] remote stop failed: %s" % exc)
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            except Exception as exc:
                self.io_failed = True
                print("[audio] 声卡收尾失败: %s" % exc)
                try:
                    self.stream.close()
                except Exception:
                    pass
            self.stream = None
        if self.wav is not None:
            try:
                self.wav.close()
            except Exception as exc:
                self.io_failed = True
                print("[audio] 文件收尾失败: %s" % exc)
            self.wav = None
        if self.wav_file is not None:
            try:
                self.wav_file.close()
            except Exception:
                self.io_failed = True
            self.wav_file = None
            if self.replay and self.wav_path:
                print("[replay] 恢复的录音已保存：%s（%d 帧，约 %.1f 秒%s）"
                      % (self.wav_path, self.frames_in, self.frames_in * 0.02,
                         "；写盘出过错，文件可能不完整" if self.io_failed else ""))
        if self.remote_feed_failures > 1:
            print("[openless] 本段共有 %d 帧没能送进 OpenLess" % self.remote_feed_failures)
        if self.lost or self.late or self.dup or self.resync:
            print("[audio] 本段统计：缺包 %d(约 %dms) 迟到丢弃 %d 重复丢弃 %d 重同步 %d"
                  % (self.lost, self.lost * 20, self.late, self.dup, self.resync))
        if not self.stats_reported and (self.frames_in or self.io_failed or self.underflows):
            print("[audio] 输出统计 session=%s frames_received_unique=%d submitted_samples=%d "
                  "underflows=%d io_failed=%d"
                  % (self.session, self.frames_in, self.submitted_samples,
                     self.underflows, self.io_failed))
            self.stats_reported = True
        # 保留统计直到下一次 start，供完成回执/诊断读取。


class AudioWorker:
    """只有本线程接触输出流；网络线程不等待声卡、WAV 或设备重枚举。

    最多缓存 WORKER_QUEUE_MAX 帧(15 秒)，只为吸收突发和 OpenLess 开始握手的等待；
    超载丢帧必须令本段确认失败(如实少报回执)，不能累积无限延迟。
    控制命令单独预留 8 个位置，满音频队列也不会吃掉停止请求。
    """
    def __init__(self, sink):
        self.sink = sink
        self.commands = deque()
        self.condition = threading.Condition()
        self.audio_lock = threading.Lock()
        self.pending_frames = self.pending_controls = 0
        self.pending_sessions = 0
        self.closed = False
        self.vmic = "unknown"
        self.thread = threading.Thread(target=self._run, name="tp-audio", daemon=True)
        self.thread.start()

    def _put(self, verb, *args):
        with self.condition:
            if self.closed:
                return False
            if verb == "frame":
                # v4.0.3：工作队列和抖动缓冲是两回事。Wi-Fi 省电模式下包是成串
                # 到的(实测一次 10+ 帧)，12 帧的队列一冲就满，整段 ACK 被判 0，
                # 电脑明明识别出来了设备却显示"已暂存"。队列放宽到 3 秒；
                # 抖动缓冲(JITTER_MAX)仍然限制出声延迟。
                if self.pending_frames >= WORKER_QUEUE_MAX:
                    return False
                self.pending_frames += 1
            else:
                if self.pending_controls >= 8:
                    return False
                self.pending_controls += 1
                if verb == "start":
                    self.pending_sessions += 1
            self.commands.append((verb, args))
            self.condition.notify()
            return True

    def start(self, session, channels=1, replay=False):
        return self._put("start", session, channels, replay)

    def feed(self, seq, pcm):
        return self._put("frame", seq, pcm)

    def has_room(self, margin=2):
        """音频队列还有没有余量。USB 那边一次可能涌来一串帧(补发尤其如此)，
        主循环据此决定先喂多少、剩下的留在串口收件箱里等下一轮 ——
        否则一口气塞满 12 帧的队列就被判溢出，整段回执成 0。"""
        with self.condition:
            return self.pending_frames < WORKER_QUEUE_MAX - margin

    def stop(self, callback):
        return self._put("stop", callback)

    def cancel(self, callback):
        with self.condition:
            self.commands.clear()
            self.pending_frames = self.pending_sessions = 0
            self.pending_controls = 1
            self.commands.append(("cancel", (callback,)))
            self.condition.notify()

    def probe(self, name, rescan=False):
        # Never wait for a blocked write/close; cached health is explicitly unknown
        # until the output worker has opened an actual stream.
        remote = getattr(self.sink, "remote", None)
        if remote is not None:
            # OpenLess 直连模式根本不走虚拟声卡：报 "direct"(设备显示"直连")，
            # 不再把一次识别报错误算成"虚拟声卡出错"。
            return "direct" if remote.health() != "offline" else "down"
        if not self.audio_lock.acquire(blocking=False):
            return self.vmic
        try:
            with self.condition:
                active = self.pending_sessions != 0
            if active:
                return "error" if self.sink.io_failed else self.vmic
            if rescan:
                refresh_audio_devices()
            self.vmic, self.sink.device_index = probe_vmic(name)
            return self.vmic
        finally:
            self.audio_lock.release()

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.commands or self.closed)
                if not self.commands:
                    break
                verb, args = self.commands.popleft()
                if verb == "frame":
                    self.pending_frames -= 1
                else:
                    self.pending_controls -= 1
            cancel_error = None
            try:
                with self.audio_lock:
                    if verb == "start":
                        self.sink.start(*args)
                        # 补发会话根本不开声卡/OpenLess，它的结果不代表 vmic 的健康。
                        if not (len(args) > 2 and args[2]):
                            remote = getattr(self.sink, "remote", None)
                            self.vmic = ("ok" if remote is not None and not self.sink.io_failed
                                         else "ok" if self.sink.stream is not None
                                         else "unavailable")
                    elif verb == "frame":
                        self.sink.feed(*args)
                    elif verb == "cancel":
                        self.sink.cancel()
                    else:
                        self.sink.stop()
            except Exception as exc:
                cancel_error = str(exc)
                self.sink.io_failed = True
                self.vmic = "error"
                print("[audio] 工作线程异常，本段不能确认: %s" % exc)
            if verb == "cancel":
                args[0](cancel_error)
            if verb == "stop":
                # ACK is not ASR acceptance. It means unique PCM frames reached
                # the output without an I/O exception; OpenLess needs its own ACK.
                count = 0 if self.sink.io_failed else self.sink.frames_in
                with self.condition:
                    self.pending_sessions = max(0, self.pending_sessions - 1)
                try:
                    args[0](count)
                except Exception as exc:
                    print("[audio] 回执发送失败: %s" % exc)
        with self.audio_lock:
            self.sink.stop()

    def close(self):
        with self.condition:
            self.closed = True
            self.condition.notify()
        self.thread.join(timeout=3.0)


class AudioSession:
    """Legacy TP_START/STOP session gate. Binary PCM has no session id;
    source/active checks prevent foreign or stopped streams, not authentication.
    """
    def __init__(self, worker, say):
        self.worker, self.say = worker, say
        self.active = None
        self.channels = None
        self.started = False
        self.overflow = False
        self.queue_drops = 0
        self.channel_changes = 0
        self.timed_out = False
        self.last_done = None
        self.last_packet_at = 0.0
        self.replay = False
        self.completions = queue.SimpleQueue()
        self.enabled = True
        self.cancelled = deque(maxlen=32)

    def start(self, session, peer, replay=False):
        if not self.enabled or (session, peer) in self.cancelled:
            return False
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", session):
            return False
        key = (session, peer)
        if self.active:
            return self.active == key   # retransmitted START is idempotent
        self.active, self.channels = key, None
        self.replay = bool(replay)
        self.started = self.overflow = False
        self.queue_drops = self.channel_changes = 0
        self.timed_out = False
        self.last_packet_at = time.monotonic()
        return True

    def feed(self, data, peer):
        if not self.enabled or not self.active or self.active[1] != peer:
            return False
        if len(data) not in (4 + FRAME_BYTES, 4 + FRAME_BYTES2):
            return False
        self.last_packet_at = time.monotonic()
        channels = 2 if len(data) == 4 + FRAME_BYTES2 else 1
        if not self.started:
            # 只有补发才带 replay 参数：老的 worker 替身/接口保持两参数调用。
            ok = (self.worker.start(self.active[0], channels, replay=True) if self.replay
                  else self.worker.start(self.active[0], channels))
            if not ok:
                self.overflow = True
                self.queue_drops += 1
                return False
            self.started, self.channels = True, channels
        if channels != self.channels:
            self.overflow = True
            self.channel_changes += 1
            return False
        if not self.worker.feed(struct.unpack("<I", data[:4])[0], data[4:]):
            self.overflow = True
            self.queue_drops += 1
            return False
        return True

    def stop(self, session, peer):
        key = (session, peer)
        if key in self.cancelled:
            return False
        if self.last_done and self.last_done[0] == key and self.active != key:
            self.say(self.last_done[1], peer)
            return True
        if self.active != key:
            return False
        overflow = self.overflow
        queue_drops, channel_changes, timed_out = (
            self.queue_drops, self.channel_changes, self.timed_out)
        kind = "补发会话" if self.replay else "会话"
        def done(count):
            print("[audio] %s %s output_ack_frames=%d queue_drops=%d "
                  "channel_changes=%d timed_out=%d"
                  % (kind, session, count, queue_drops, channel_changes, timed_out))
            # 丢了帧就如实少报(设备按"收到数 < 发出数"保留副本)，
            # 不再一律报 0 —— 0 会让设备以为电脑什么都没收到。
            self.completions.put((key, 0 if timed_out else max(0, count)))
        if self.started and not self.worker.stop(done):
            return False         # keep active; a repeated STOP can retry
        if not self.started:
            done(0)
        self.active = None
        return True

    def cancel(self, callback):
        if self.active:
            self.cancelled.append(self.active)
        self.active, self.started = None, False
        self.worker.cancel(callback)

    def poll(self):
        if self.active and time.monotonic() - self.last_packet_at > 5.0:
            # A lost STOP or disconnected device must not hold the stream forever.
            # Timeout is not successful completion: preserve the device's copy.
            self.overflow = True
            self.timed_out = True
            self.stop(*self.active)
        while not self.completions.empty():
            key, count = self.completions.get_nowait()
            if key in self.cancelled:
                continue
            payload = ("TP_DONE %s %d" % (key[0], count)).encode()
            self.last_done = (key, payload)
            self.say(payload, key[1])
            print("[audio] %s 已收尾，ack_frames=%d（输出确认，不是接收帧数或识别结果）"
                  % (key[0], count))


# ---------------------------------------------------------------- 助手真实状态
HOME = os.path.expanduser("~")
CLAUDE_DIR = os.path.join(HOME, ".claude")
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CODEX_DIR = os.path.join(HOME, ".codex")
AGENT_SCAN_SEC = 20        # 项目/今日会话数这类要扫目录的，20 秒一次够了
MODEL_TAIL_BYTES = 256 * 1024


def _win_proc_created(pid):
    """进程创建时间(FILETIME)。进程不在就返回 None。

    【为什么不能只看 pid 在不在】pid 会被回收。sessions/<pid>.json 是会话
    退出时不一定清得掉的残留文件，光看"这个 pid 现在有进程"很容易撞上
    一个完全无关的新进程，然后在表上显示一个根本不存在的会话在跑。
    所以把文件里记的 procStart 和系统里的创建时间对一下。
    """
    k = ctypes.windll.kernel32
    k.OpenProcess.restype = ctypes.c_void_p
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
    if not handle:
        return None
    try:
        class FILETIME(ctypes.Structure):
            _fields_ = [("lo", ctypes.c_uint32), ("hi", ctypes.c_uint32)]
        created, exited, kern, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
        k.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(FILETIME)] * 4
        if not k.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                 ctypes.byref(kern), ctypes.byref(user)):
            return None
        return (created.hi << 32) | created.lo
    finally:
        k.CloseHandle(handle)


def pid_alive(pid, proc_start=None):
    """pid 活着吗。给了 proc_start 就连创建时间一起核对(防 pid 回收)。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            created = _win_proc_created(pid)
        except Exception:
            return False
        if created is None:
            return False
        if proc_start:
            try:
                return int(proc_start) == created
            except (TypeError, ValueError):
                pass
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def encode_project_dir(path):
    """真实路径 → ~/.claude/projects 下的目录名。

    Claude Code 把路径里所有非字母数字的字符换成 '-'。这是【正向】算的：
    目录名本身是有损的(中文全被抹成 '-')，反过来猜路径会猜错。
    """
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def _newest_transcript_mtime(project_dir):
    newest = 0.0
    try:
        for name in os.listdir(project_dir):
            if not name.endswith(".jsonl"):
                continue
            try:
                mtime = os.path.getmtime(os.path.join(project_dir, name))
            except OSError:
                continue
            newest = max(newest, mtime)
    except OSError:
        return 0.0
    return newest


_model_cache = {}


def claude_model(session_id):
    """这一轮真正在用的模型 id。读不到就 None，不拿 settings.json 的
    model 顶替 —— 那是"默认配置"，不是"这次用的"，两件事。"""
    if not session_id:
        return None
    hits = glob.glob(os.path.join(CLAUDE_DIR, "projects", "*", "%s.jsonl" % session_id))
    if not hits:
        return None
    path = hits[0]
    try:
        stat = os.stat(path)
    except OSError:
        return None
    key = (path, stat.st_mtime, stat.st_size)
    if _model_cache.get("key") == key:
        return _model_cache.get("model")
    model = None
    try:
        with open(path, "rb") as fh:
            if stat.st_size > MODEL_TAIL_BYTES:
                fh.seek(stat.st_size - MODEL_TAIL_BYTES)
                fh.readline()          # 半行丢掉
            lines = fh.read().splitlines()
        for raw in reversed(lines):
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            msg = rec.get("message")
            if isinstance(msg, dict) and msg.get("model"):
                model = str(msg["model"])
                break
    except OSError:
        model = None
    _model_cache["key"] = key
    _model_cache["model"] = model
    return model


def claude_live_sessions():
    """~/.claude/sessions/<pid>.json 里进程还活着的那些。"""
    out = []
    for path in glob.glob(os.path.join(CLAUDE_DIR, "sessions", "*.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        if not pid_alive(rec.get("pid"), rec.get("procStart")):
            continue
        out.append(rec)
    # v4.0.4：同时开着几个会话时，正在干活的排前面，其次才看最近更新 ——
    # 否则一个刚点开没动的空闲会话会把正在执行的那个挤下去。
    # 后台自动任务(entrypoint 以 sdk- 开头，比如 Codex 批量调用 Claude)排在人开的
    # 会话后面 —— v4.0.4 时它和桌面端会话交替排第一，智能体页的会话名和额度来回跳。
    out.sort(key=lambda r: (not str(r.get("entrypoint") or "").startswith("sdk-"),
                            r.get("status") == "busy", r.get("updatedAt") or r.get("startedAt") or 0),
             reverse=True)
    return out


def claude_today_sessions():
    """今天被改过的会话记录文件数 = 今天有过活动的会话数。
    读不到目录就返回 None(未知)，不返回 0 —— 0 是个结论。"""
    root = os.path.join(CLAUDE_DIR, "projects")
    if not os.path.isdir(root):
        return None
    midnight = time.mktime(datetime.date.today().timetuple())
    count = 0
    try:
        for project in os.listdir(root):
            pdir = os.path.join(root, project)
            if not os.path.isdir(pdir):
                continue
            for name in os.listdir(pdir):
                if not name.endswith(".jsonl"):
                    continue
                try:
                    if os.path.getmtime(os.path.join(pdir, name)) >= midnight:
                        count += 1
                except OSError:
                    continue
    except OSError:
        return None
    return count


def codex_running():
    """有没有 Codex 的 CLI 进程。只说明"进程在"，说明不了它在忙什么。"""
    try:
        if os.name == "nt":
            out = os.popen('tasklist /FI "IMAGENAME eq codex.exe" /NH').read()
            return "codex.exe" in out.lower()
        return bool(os.popen("pgrep -x codex").read().strip())
    except Exception:
        return False


def _claude_projects():
    """[(最后活动时间, 绝对路径)]，来自 ~/.claude.json 的 projects 键。"""
    try:
        with open(CLAUDE_JSON, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return []
    projects = cfg.get("projects")
    if not isinstance(projects, dict):
        return []
    root = os.path.join(CLAUDE_DIR, "projects")
    out = []
    for path in projects:
        mtime = _newest_transcript_mtime(os.path.join(root, encode_project_dir(path)))
        if mtime <= 0:
            continue          # 没有会话记录 = 说不出"最近什么时候用过"，跳过
        out.append((mtime, path, "claude"))
    return out


def _codex_projects(limit=12):
    """Codex 的 rollout 文件，每个第一行 session_meta 里有真实 cwd。"""
    files = []
    for path in glob.iglob(os.path.join(CODEX_DIR, "sessions", "*", "*", "*",
                                         "rollout-*.jsonl")):
        try:
            files.append((os.path.getmtime(path), path))
        except OSError:
            continue
    files.sort(reverse=True)
    out = []
    for mtime, path in files[:limit]:
        try:
            with open(path, "rb") as fh:
                first = fh.readline()
            rec = json.loads(first.decode("utf-8", "replace"))
        except Exception:
            continue
        cwd = (rec.get("payload") or {}).get("cwd")
        if isinstance(cwd, str) and cwd:
            out.append((mtime, cwd, "codex"))
    return out


def fmt_age(seconds):
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return "%d 分钟前" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时前" % (seconds // 3600)
    return "%d 天前" % (seconds // 86400)


def recent_projects(live_cwds, limit=3, now=None):
    """最近项目，Claude + Codex 合并去重，按最后活动时间倒序。"""
    now = time.time() if now is None else now
    rows = _claude_projects() + _codex_projects()
    rows.sort(reverse=True)
    seen, out = set(), []
    live = set(os.path.normcase(os.path.abspath(p)) for p in live_cwds if p)
    for mtime, path, tool in rows:
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        name = os.path.basename(path.rstrip("\\/")) or path
        out.append({
            "name": name,
            "path": key,
            # 状态只有两档：正是活着的会话的 cwd = run；其余一律 idle。
            # 待授权/已完成/出错这三种，本机没有任何真实信号能判断。
            "state": "run" if key in live else "idle",
            "tool": tool,
            "note": "%s · %s" % (fmt_age(max(0, int(now - mtime))),
                                 "Claude Code" if tool == "claude" else "Codex"),
        })
        if len(out) >= limit:
            break
    return out


_scan_cache = {"at": 0.0, "today": None, "projects": None}


class CodexWatcher:
    """从 Codex 自己写的会话记录里读出真实状态(v4.0.4)。

    【为什么】v4.0.3 只看 codex.exe 进程在不在，忙/闲、模型、会话名一概"未知"；
    而且只要有 Claude 会话在跑，就只上报 Claude —— Codex 在跑任务时设备上
    Codex 页写的是"未上报"。
    会话记录 ~/.codex/sessions/**/rollout-*.jsonl 是 Codex 边跑边追加的：
      task_started / task_complete / turn_aborted   这一轮开始 / 结束
      *_approval_request                            在等你批准
      turn_context.payload.model                    这一轮用的模型
    会话名来自 ~/.codex/session_index.jsonl(按会话 id)。
    文件可能十几 MB，所以按偏移增量读，每次只读新追加的部分。"""
    ACTIVE_S = 15 * 60           # 这么久没写记录的会话不算"在跑"
    SCAN_FILES = 16

    def __init__(self, codex_dir=None):
        self.root = codex_dir or CODEX_DIR
        self.files = {}          # path -> 状态

    @staticmethod
    def _new_state():
        return {"offset": 0, "partial": b"", "busy": False, "awaiting": False,
                "pending": {}, "model": None, "turn_at": None}

    @staticmethod
    def _event(st, row):
        """Read structured events, never match lifecycle words inside chat text."""
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return
        event_type, kind = row.get("type"), payload.get("type", "")
        pending = st["pending"]
        call_id = payload.get("call_id") or payload.get("id")
        if event_type == "turn_context":
            model = payload.get("model")
            if isinstance(model, str) and model:
                st["model"] = model
        elif event_type == "event_msg":
            if kind == "task_started":
                st["busy"] = True
                st["turn_at"] = _iso_ts(row.get("timestamp"))
                pending.clear()  # A new user turn supersedes unanswered prior prompts.
            elif kind in ("task_complete", "turn_aborted"):
                st["busy"] = False
                # Async questions can be answered after the assistant finishes.
                for cid in list(pending):
                    if kind == "turn_aborted" or pending[cid]["kind"] != "async":
                        del pending[cid]
            elif kind.endswith("_approval_request"):
                pending[call_id or "legacy"] = {"kind": "approval"}
            elif kind in ("exec_command_end", "patch_apply_end"):
                pending.pop(call_id or "legacy", None)
        elif event_type == "response_item":
            name = payload.get("name", "").rsplit(".", 1)[-1]
            if kind in ("function_call", "custom_tool_call") and name in ("request_user_input", "request_user_input_async") and call_id:
                asynchronous = name.endswith("_async")
                args = payload.get("arguments", payload.get("input", {}))
                try:
                    args = json.loads(args) if isinstance(args, str) else args
                except ValueError:
                    args = {}
                questions = args.get("questions", []) if isinstance(args, dict) else []
                pending[call_id] = {"kind": "async" if asynchronous else "input",
                                    "remaining": set(range(max(1, len(questions))))}
            elif kind in ("function_call_output", "custom_tool_call_output"):
                # Response item id belongs to the output itself, not the invocation.
                cid = payload.get("call_id") or "legacy"
                item = pending.get(cid)
                if item and item["kind"] != "async":
                    del pending[cid]
                elif item:
                    output = payload.get("output", "")
                    try:
                        result = json.loads(output) if isinstance(output, str) else output
                    except ValueError:
                        result = None
                    if isinstance(result, dict) and result.get("accepted") is False:
                        del pending[cid]
            elif kind == "message" and payload.get("role") == "user":
                for content in payload.get("content", []):
                    text = content.get("text", "").strip()
                    match = re.fullmatch(r"<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>", text, re.S)
                    if not match:
                        continue
                    try:
                        answers = json.loads(match.group(1))
                        for answer in answers:
                            qid = json.loads(answer["questionItemId"])
                            cid, index = qid[1], qid[2]
                            item = pending.get(cid)
                            if item and item["kind"] == "async":
                                item["remaining"].discard(index)
                                if not item["remaining"]:
                                    del pending[cid]
                    except (ValueError, TypeError, KeyError, IndexError):
                        continue
        st["awaiting"] = bool(pending)

    def _update(self, path, st):
        try:
            size = os.path.getsize(path)
        except OSError:
            return
        if size < st["offset"]:
            st.clear()
            st.update(self._new_state())
        if size == st["offset"]:
            return
        try:
            with open(path, "rb") as fh:
                fh.seek(st["offset"])
                data = fh.read(size - st["offset"])
        except OSError:
            return
        st["offset"] = size
        lines = (st["partial"] + data).split(b"\n")
        st["partial"] = lines.pop()
        for raw in lines:
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(row, dict):
                self._event(st, row)

    def report(self, now=None):
        now = time.time() if now is None else now
        found = []
        for path in glob.iglob(os.path.join(self.root, "sessions", "*", "*", "*", "rollout-*.jsonl")):
            try:
                found.append((os.path.getmtime(path), path))
            except OSError:
                continue
        found.sort(reverse=True)
        recent = [(m, p) for i, (m, p) in enumerate(found)
                  if i < self.SCAN_FILES or now - m <= self.ACTIVE_S]
        keep = {p for _, p in recent}
        # Do not drop a pending question just because other sessions wrote logs.
        recent += [(m, p) for m, p in found if p not in keep and self.files.get(p, {}).get("awaiting")]
        keep.update(p for _, p in recent)
        for p in list(self.files):
            if p not in keep:
                del self.files[p]
        best = None
        for mtime, path in recent:
            st = self.files.setdefault(path, self._new_state())
            self._update(path, st)
            st["mtime"] = mtime
            active = st["awaiting"] or now - mtime <= self.ACTIVE_S
            rank = (active and st["awaiting"], active and st["busy"], mtime)
            if best is None or rank > best[0]:
                best = (rank, path, st)
        today = sum(1 for m, _ in found if datetime.date.fromtimestamp(m) == datetime.date.fromtimestamp(now))
        report = {"tool": "codex", "today": today}
        if best is None:
            report.update(run=0, state="idle")
            return report
        _, path, st = best
        active = st["awaiting"] or now - st["mtime"] <= self.ACTIVE_S
        running = active or codex_running()
        report["run"] = 1 if running else 0
        report["state"] = ("awaiting" if active and st["awaiting"] else
                           "busy" if active and st["busy"] else "idle")
        if report["state"] == "awaiting":
            report["wait"] = "input" if any(p["kind"] in ("input", "async") for p in st["pending"].values()) else "approval"
        if st["model"]:
            report["model"] = st["model"]
        name = _codex_thread_name(path, self.root)
        if name and running:
            report["sess"] = name
        if report["state"] in ("busy", "awaiting") and st["turn_at"]:
            report["elapsed"] = max(0, int(now - st["turn_at"]))
        return report


def _iso_ts(text):
    if not isinstance(text, str):
        return None
    try:
        return datetime.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_thread_names = {"at": 0.0, "map": {}}


def _codex_thread_name(path, root):
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$", path)
    if not m:
        return None
    if time.time() - _thread_names["at"] > 20:
        _thread_names["at"] = time.time()
        names = {}
        try:
            with open(os.path.join(root, "session_index.jsonl"), encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec.get("thread_name"), str):
                        names[rec.get("id")] = rec["thread_name"]
        except OSError:
            pass
        _thread_names["map"] = names
    return _thread_names["map"].get(m.group(1))


_codex_watcher = CodexWatcher()


def agent_reports():
    """v4.0.4：Claude 和 Codex 各一份。原来只报一份(有 Claude 会话就只报 Claude)。
    项目列表只挂在第一份上，两个助手共用。"""
    claude = agent_report(codex_fallback=False)
    claude.setdefault("tool", "claude")
    try:
        codex = _codex_watcher.report()
    except Exception as exc:
        print("[agent] Codex 状态读取失败: %s" % exc)
        codex = {"tool": "codex", "run": 1 if codex_running() else 0, "state": "unknown"}
    codex.pop("projects", None)
    return [claude, codex]


def agent_report(codex_fallback=True):
    """把上面那些真实探测汇总成一份报告。拿不到的键就【不放进去】。"""
    report = {}
    sessions = claude_live_sessions()
    live_cwds = [s.get("cwd") for s in sessions]
    if sessions:
        top = sessions[0]
        report["tool"] = "claude"
        report["run"] = 1
        # Claude Code 自己写的 status。只认这两个，别的(包括没有这个字段)
        # 都报 unknown —— 把陌生字符串硬映射成"空闲"就是替它下结论。
        status = top.get("status")
        report["state"] = status if status in ("busy", "idle") else "unknown"
        if top.get("name"):
            report["sess"] = top["name"]
        started = top.get("startedAt")
        if isinstance(started, (int, float)) and started > 0:
            report["elapsed"] = max(0, int(time.time() - started / 1000.0))
        model = claude_model(top.get("sessionId"))
        if model:
            report["model"] = model
    elif codex_fallback and codex_running():
        report["tool"] = "codex"
        report["run"] = 1
        # 只探到"有 codex 进程"。它是忙是闲、用的哪个模型，都读不出来。
        report["state"] = "unknown"
    else:
        report["run"] = 0
        report["state"] = "idle"

    # 今日会话数和项目列表都要扫目录，2 秒扫一次太费；这两项 20 秒刷一次。
    now = time.time()
    if now - _scan_cache["at"] >= AGENT_SCAN_SEC or _scan_cache["at"] == 0:
        _scan_cache["at"] = now
        _scan_cache["today"] = claude_today_sessions()
        _scan_cache["projects"] = recent_projects(live_cwds, now=now)
    today = _scan_cache["today"]
    if today is not None:
        report["today"] = today
    # 【额度】不放进 TP_AGENT：v4.0.4 起由单独的 TP_QUOTA 按助手分别上报
    # (Codex 读会话记录里的 rate_limits，Claude 读 statusline 快照)。
    projects = _scan_cache["projects"]
    if projects is not None:
        # 缓存里的 run/idle 可能过期了，用这一轮真实的活会话重标一遍。
        live = set(os.path.normcase(os.path.abspath(p)) for p in live_cwds if p)
        for pj in projects:
            pj["state"] = "run" if pj.get("path") in live else "idle"
        report["projects"] = projects
    return report


def fit_bytes(text, max_bytes):
    """截到 max_bytes 以内，但只在字符边界上切。

    【为什么不能按字节切】设备端的缓冲区是按字节算的(name[48] 之类)，
    而中文一个字三字节。直接 text.encode()[:48] 会把最后一个汉字劈成半个，
    设备上显示成一个问号方块 —— 那不是截断，那是乱码。
    """
    text = str(text)
    while len(text.encode("utf-8")) > max_bytes:
        text = text[:-1]
    return text


def _token(value, max_bytes=30):
    """值里不能有空格(设备按空格切 k=v)，也不能撑破设备端的缓冲区。"""
    return fit_bytes(re.sub(r"[\s|]+", "_", str(value)).strip("_"), max_bytes)


_ELAPSED = re.compile(rb" elapsed=\d+")


def agent_gist(grams):
    """TP_AGENT 报文去掉每秒都在变的 elapsed，用来判断"内容真的变了"。"""
    return tuple(_ELAPSED.sub(b"", gram) for gram in grams)


def build_agent_datagrams(report):
    """报告 → 待发的 UDP 报文列表。纯函数，可单测。

    每个值的字节上限对着 tp_link.h 里的缓冲区：model[32] / session[48] /
    tool[8] / name[48] / note[48]，都留一个字节给结尾的 0。
    """
    caps = {"tool": 7, "model": 31, "sess": 44}
    fields = []
    for key in ("tool", "run", "state", "wait", "model", "elapsed", "today", "quota", "sess"):
        if key in report:
            fields.append("%s=%s" % (key, _token(report[key], caps.get(key, 30))))
    projects = report.get("projects")
    out = []
    if projects is not None:
        fields.append("proj=%d" % len(projects))
    out.append(("TP_AGENT " + " ".join(fields)).encode("utf-8"))
    for i, pj in enumerate(projects or []):
        name = fit_bytes(re.sub(r"[\s|]+", " ", str(pj.get("name", ""))).strip(), 44).strip()
        note = fit_bytes(re.sub(r"[\s|]+", " ", str(pj.get("note", ""))).strip(), 44).strip()
        out.append(("TP_PROJ %d %d %s %s %s|%s"
                    % (i, len(projects), _token(pj.get("tool", "unknown"), 7),
                       _token(pj.get("state", "unknown"), 7), name, note)).encode("utf-8"))
    return out


# ---------------------------------------------------------------- 天气
def fetch_weather(lat, lon, tz):
    # tz="auto"：让 Open-Meteo 按坐标自己选时区，日出日落分钟数才是【那个地方】的本地时间。
    url = ("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
           "&hourly=temperature_2m,weather_code&daily=sunrise,sunset"
           "&forecast_days=1&timezone=%s"
           % (round(float(lat), 4), round(float(lon), 4), urllib.parse.quote(str(tz), safe="/")))
    with urllib.request.urlopen(url, timeout=15) as resp:
        return json.load(resp)


def icon_for(code, night=False):
    """WMO weather_code → 交接文档 §8 的图标名。

    night=True 时晴天/少云换成月亮系图标 —— 原来不分昼夜，
    晚上的天气条上也画太阳，一眼就知道是假的。
    """
    if code == 0:
        return "moon" if night else "sun"
    if code in (1, 2):
        return "cloudmoon" if night else "cloudsun"
    if code in (3, 45, 48):
        return "cloud"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "snow"
    if 95 <= code <= 99:
        return "storm"
    return "cloud"


def minutes_of(iso):
    """'2026-09-13T06:12' → 372"""
    hour, minute = iso.split("T")[1].split(":")[:2]
    return int(hour) * 60 + int(minute)


# ---------------------------------------------------------------- 自动位置(v4.0.2)
LOCATION_REFRESH_S = 1800
LOCATION_MOVE_KM = 3.0
LOCATION_NEAR_KM = 50.0     # 城市名只在"确实就在附近"时才借用
IP_GEO_URLS = ("https://ipwho.is/", "https://ipapi.co/json/")

_WIN_LOCATION_PS = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Device
$w = New-Object System.Device.Location.GeoCoordinateWatcher
if (-not $w.TryStart($true, [TimeSpan]::FromSeconds(4))) { exit 3 }
$deadline = (Get-Date).AddSeconds(3)
while ($w.Status -ne 'Ready' -and $w.Permission -ne 'Denied' -and (Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 200
}
if ($w.Permission -eq 'Denied') { exit 4 }
$c = $w.Position.Location
$w.Stop()
if ($c.IsUnknown) { exit 5 }
$inv = [Globalization.CultureInfo]::InvariantCulture
[Console]::Out.WriteLine($c.Latitude.ToString('R', $inv) + ' ' + $c.Longitude.ToString('R', $inv))
"""


def distance_km(a, b):
    """两点大圆距离(公里)。a/b 是 (lat, lon)。"""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def _valid_coords(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None          # (0,0) 是很多服务"查不到"时的占位值，不是真位置
    return lat, lon


def ascii_label(text, max_len=16):
    """设备字库只有 ASCII。不是纯 ASCII 地名就返回 None —— 让调用方换下一个候选，
    而不是把"금정구"硬转成一串问号。"""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 .'-]*", text):
        return None
    return text[:max_len].strip() or None


def windows_location(timeout=15.0):
    """Windows 定位服务(GeoCoordinateWatcher)。关着/没授权/超时 → None。

    【为什么走 powershell.exe 而不是 pwsh】System.Device 只在 .NET Framework 里，
    PowerShell 7 加载不到。整个查询在后台线程里跑，最多 ~8 秒。
    """
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                              _WIN_LOCATION_PS], capture_output=True, text=True,
                             timeout=timeout)
    except Exception as exc:
        print("[location] Windows 定位不可用: %s" % exc)
        return None
    if out.returncode != 0:
        why = {3: "定位服务没启动", 4: "定位权限被拒", 5: "没有定位结果"}.get(
            out.returncode, "exit %s" % out.returncode)
        print("[location] Windows 定位不可用: %s" % why)
        return None
    parts = (out.stdout or "").strip().split()
    coords = _valid_coords(*parts[:2]) if len(parts) >= 2 else None
    if coords is None:
        print("[location] Windows 定位返回无法解析，跳过")
    return coords


def _http_json(url, timeout=6.0):
    req = urllib.request.Request(url, headers={"User-Agent": "tidepool-receiver/%s" % VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def parse_ip_geo(data):
    """把 ipwho.is / ipapi.co 两种返回统一成 {lat, lon, city, locality, tz}。失败返回 None。"""
    if not isinstance(data, dict) or data.get("success") is False or data.get("error"):
        return None
    coords = _valid_coords(data.get("latitude"), data.get("longitude"))
    if coords is None:
        return None
    tz = data.get("timezone")
    if isinstance(tz, dict):
        tz = tz.get("id")
    return {"lat": coords[0], "lon": coords[1],
            "city": data.get("city"),
            "locality": data.get("district") or data.get("locality"),
            "tz": tz if isinstance(tz, str) and re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+)+", tz)
                  else None}


def ip_location(urls=IP_GEO_URLS, fetch=_http_json):
    """IP 定位：按顺序试几个 HTTPS 服务，第一个给出有效坐标的算数。"""
    for url in urls:
        try:
            geo = parse_ip_geo(fetch(url))
        except Exception as exc:
            print("[location] IP 定位 %s 失败: %s" % (urllib.parse.urlparse(url).hostname, exc))
            continue
        if geo is not None:
            geo["provider"] = urllib.parse.urlparse(url).hostname
            return geo
    return None


_LAST_WIN_FIX = None


def resolve_location(args, win_fn=windows_location, ip_fn=ip_location):
    """Windows 定位 → IP 定位 → 命令行 --lat/--lon。都没有就 None(不编位置)。

    城市名：定位来源自己的 district/locality → city(都得是 ASCII)，
    再退到命令行 --city，最后 "Here"。借用别处的名字有个前提：那个名字对应的
    坐标离实际坐标不超过 50km —— 否则人在首尔，屏幕上写着 Busan，那是假的。
    """
    cli = None
    if getattr(args, "lat", None) is not None and getattr(args, "lon", None) is not None:
        cli = _valid_coords(args.lat, args.lon)
    win = win_fn()
    ip = ip_fn()
    # Windows 定位(Wi-Fi 三角定位)偶尔一次拿不到结果：沿用上一次成功的，
    # 不让一次超时就把地点跳到 IP 定位上去。
    global _LAST_WIN_FIX
    if win:
        _LAST_WIN_FIX = (win, time.monotonic())
    elif _LAST_WIN_FIX and time.monotonic() - _LAST_WIN_FIX[1] < 6 * 3600:
        win = _LAST_WIN_FIX[0]
    # 【IP 定位在韩国很不准】运营商出口常在首尔都市圈，实测釜山被判成城南市
    # (差 320km)。所以同一时区内，用户配置的家(釜山大学)比 IP 可信；
    # 只有 IP 落在另一个时区(真的出门远行了)才用 IP 定位。
    ip_travel = bool(ip and ip.get("tz") and getattr(args, "tz", None)
                     and ip["tz"] != args.tz)
    if win:
        coords, source = win, "windows"
    elif cli and not ip_travel:
        coords, source = cli, "cli"
    elif ip:
        coords, source = (ip["lat"], ip["lon"]), "ip"
    elif cli:
        coords, source = cli, "cli"
    else:
        return None
    near_ip = ip if ip and distance_km(coords, (ip["lat"], ip["lon"])) <= LOCATION_NEAR_KM else None
    city = None
    if near_ip:
        city = ascii_label(near_ip.get("locality")) or ascii_label(near_ip.get("city"))
    if not city and cli and distance_km(coords, cli) <= LOCATION_NEAR_KM:
        city = ascii_label(getattr(args, "city", "") or "")
    return {"lat": coords[0], "lon": coords[1], "source": source,
            "city": city or "Here",
            "tz": (near_ip or {}).get("tz") or "auto"}


# ---------------------------------------------------------------- 本机时区 → POSIX TZ(v4.0.2)
# 常见 Windows 时区键 → 带惯用缩写的 POSIX 串。只是"好看的名字"：用之前会和
# 本机实际偏移核对，对不上(比如用户关了夏令时自动调整)就用下面算出来的那份。
WINDOWS_TZ_POSIX = {
    "Korea Standard Time": "KST-9",
    "China Standard Time": "CST-8",
    "Taipei Standard Time": "CST-8",
    "Tokyo Standard Time": "JST-9",
    "Singapore Standard Time": "<+08>-8",
    "India Standard Time": "IST-5:30",
    "UTC": "UTC0",
    "GMT Standard Time": "GMT0BST,M3.5.0/1,M10.5.0",
    "W. Europe Standard Time": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Romance Standard Time": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Central Europe Standard Time": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Central European Standard Time": "CET-1CEST,M3.5.0,M10.5.0/3",
    "Eastern Standard Time": "EST5EDT,M3.2.0,M11.1.0",
    "Central Standard Time": "CST6CDT,M3.2.0,M11.1.0",
    "Mountain Standard Time": "MST7MDT,M3.2.0,M11.1.0",
    "Pacific Standard Time": "PST8PDT,M3.2.0,M11.1.0",
    "AUS Eastern Standard Time": "AEST-10AEDT,M10.1.0,M4.1.0/3",
}
POSIX_TZ_MAX = 31


def _posix_offset(seconds_east):
    """POSIX 的符号是反的：UTC+9 写成 -9，UTC-5 写成 5。"""
    west = -int(seconds_east)
    sign = "-" if west < 0 else ""
    h, rem = divmod(abs(west), 3600)
    m, s = divmod(rem, 60)
    text = "%s%d" % (sign, h)
    if m or s:
        text += ":%02d" % m
    if s:
        text += ":%02d" % s
    return text


def _posix_abbr(seconds_east):
    """数字缩写 <+09> / <-0330>：不猜 KST/CST 这种会撞名的字母缩写。"""
    sign = "+" if seconds_east >= 0 else "-"
    h, rem = divmod(abs(int(seconds_east)), 3600)
    m = rem // 60
    return "<%s%02d%s>" % (sign, h, "%02d" % m if m else "")


def _posix_rule(utc_instant, old_offset):
    """一次切换 → Mm.w.d[/time]。时间按切换【之前】生效的本地时间写(POSIX 规定)。"""
    local = datetime.datetime(1970, 1, 1) + datetime.timedelta(seconds=utc_instant + old_offset)
    days_in_month = calendar.monthrange(local.year, local.month)[1]
    week = 5 if local.day + 7 > days_in_month else (local.day - 1) // 7 + 1
    dow = (local.weekday() + 1) % 7          # Python 周一=0，POSIX 周日=0
    rule = "M%d.%d.%d" % (local.month, week, dow)
    secs = local.hour * 3600 + local.minute * 60 + local.second
    if secs != 7200:                          # 02:00 是 POSIX 默认值，省略
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        rule += "/%d" % h + (":%02d" % m if m or s else "") + (":%02d" % s if s else "")
    return rule


def _find_transitions(offset_at, year):
    """扫一年，找出 UTC 偏移变化的精确时刻(二分到秒)。"""
    start = calendar.timegm((year, 1, 1, 0, 0, 0))
    end = calendar.timegm((year + 1, 1, 1, 0, 0, 0))
    out = []
    t, prev = start, offset_at(start)
    while t < end:
        nxt = min(t + 86400, end)
        cur = offset_at(nxt)
        if cur != prev:
            lo, hi = t, nxt
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if offset_at(mid) == prev:
                    lo = mid
                else:
                    hi = mid
            out.append((hi, prev, cur))
            prev = cur
        t = nxt
    return out


def posix_tz_from_offsets(offset_at, year, now=None):
    """由"某时刻的 UTC 偏移(秒，东正)"函数推出 POSIX TZ 串。

    有两次切换且只有两种偏移 → 带夏令时规则；否则用 now 时刻的固定偏移。
    结果超过 31 字节(设备缓冲)时同样退回固定偏移 —— 至少现在这一刻是对的。
    """
    now = time.time() if now is None else now
    fixed_off = offset_at(int(now))
    fixed = "UTC0" if fixed_off == 0 else _posix_abbr(fixed_off) + _posix_offset(fixed_off)
    trans = _find_transitions(offset_at, year)
    if not trans:
        return fixed
    offsets = set(o for _t, a, b in trans for o in (a, b))
    if len(trans) != 2 or len(offsets) != 2:
        return fixed
    std, dst = min(offsets), max(offsets)
    start = next(((t, old) for t, old, new in trans if new == dst), None)
    end = next(((t, old) for t, old, new in trans if new == std), None)
    if start is None or end is None:
        return fixed
    text = _posix_abbr(std) + _posix_offset(std) + _posix_abbr(dst)
    if dst != std + 3600:
        text += _posix_offset(dst)
    text += "," + _posix_rule(*start) + "," + _posix_rule(*end)
    return text if len(text) <= POSIX_TZ_MAX else fixed


def _posix_std_east(text):
    """读出 POSIX 串里的标准时偏移(秒，东正)，读不出返回 None。"""
    m = re.match(r"(?:<[^>]+>|[A-Za-z]{3,})([+-]?)(\d{1,2})(?::(\d{2}))?", text)
    if not m:
        return None
    west = int(m.group(2)) * 3600 + int(m.group(3) or 0) * 60
    return west if m.group(1) == "-" else -west


def windows_tz_key():
    """注册表里的 Windows 时区键，如 "Korea Standard Time"。读不到 None。"""
    if os.name != "nt":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SYSTEM\CurrentControlSet\Control\TimeZoneInformation") as key:
            return str(winreg.QueryValueEx(key, "TimeZoneKeyName")[0]).strip("\x00 ")
    except Exception:
        return None


def local_posix_tz(now=None, offset_at=None, key_reader=windows_tz_key):
    """本机当前时区的 POSIX TZ 串。按电脑的时区设置，不按地理位置猜。"""
    now = time.time() if now is None else now
    if offset_at is None:
        offset_at = lambda t: time.localtime(t).tm_gmtoff
    year = time.gmtime(now).tm_year
    computed = posix_tz_from_offsets(offset_at, year, now=now)
    mapped = WINDOWS_TZ_POSIX.get(key_reader() or "")
    if mapped:
        std_computed = min(offset_at(calendar.timegm((year, 1, 1, 0, 0, 0))),
                           offset_at(calendar.timegm((year, 7, 1, 0, 0, 0))))
        if (_posix_std_east(mapped) == std_computed
                and ("," in mapped) == ("," in computed)):
            return mapped
    return computed


# ---------------------------------------------------------------- USB 有线链路(v4.0.2)
def parse_serial_line(line):
    """一行串口输出 → 协议包，或 None(日志噪声)。

    返回的包和 UDP 数据报长得一模一样，主循环不用分辨来源：
      @@TP <text>                → b"TP_..."(文本报文)
      @@TPA <seq十进制> <base64>  → struct.pack('<I', seq) + pcm(音频帧)
    标记必须在行首(只去掉 \\r)：固件日志里完全可能出现 "... @@TP ..." 这种
    子串，把它当协议就等于让一行日志冒充设备说话。
    """
    line = bytes(line).replace(b"\r", b"")
    if line.startswith(b"@@TPA "):
        parts = line[6:].split()
        if len(parts) != 2 or not parts[0].isdigit():
            return None
        seq = int(parts[0])
        if seq > 0xFFFFFFFF:
            return None
        try:
            pcm = base64.b64decode(parts[1], validate=True)
        except (binascii.Error, ValueError):
            return None
        if len(pcm) not in (FRAME_BYTES, FRAME_BYTES2):
            return None
        return struct.pack("<I", seq) + pcm
    if line.startswith(b"@@TP "):
        text = line[5:].strip()
        # 必须是 TP_ 开头：否则主循环会按长度把它当成音频帧。
        return text if text.startswith(b"TP_") else None
    return None


CRASH_MARKS = (b"Guru Meditation", b"abort() was called", b"Backtrace:", b"stack overflow",
               b"assert failed", b"CORRUPT HEAP", b"Task watchdog got triggered",
               b"Interrupt wdt timeout", b"Brownout detector", b"rst:0x", b"Rebooting...")


class SerialLineParser:
    """把任意切分的串口字节流拼成行。半行留到下一次；超长行整行丢掉。"""

    def __init__(self, max_line=SERIAL_LINE_MAX):
        self.max_line = max_line
        self.buf = bytearray()
        self.skipping = False     # 正在丢弃一条超长行，直到下一个换行
        self.crash_lines = 0      # 正在记录一段崩溃输出，还剩几行
        self.noise = 0
        self.dropped = 0

    def reset(self):
        self.buf.clear()
        self.skipping = False

    def feed(self, chunk):
        out = []
        self.buf += chunk
        while True:
            i = self.buf.find(b"\n")
            if i < 0:
                if len(self.buf) > self.max_line:
                    # 一直没有换行的垃圾(或波特率错乱)：不能让缓冲无限长。
                    self.buf.clear()
                    self.skipping = True
                    self.dropped += 1
                break
            line = bytes(self.buf[:i])
            del self.buf[:i + 1]
            if self.skipping:
                self.skipping = False      # 超长行的尾巴，丢
                continue
            if len(line) > self.max_line:
                self.dropped += 1
                continue
            packet = parse_serial_line(line)
            if packet is None:
                self.noise += 1
                # 设备侧的关键状态日志转到接收器日志里：有线时串口被接收器独占，
                # 这是看"首页状态机/补发/校时"真实发生了什么的唯一窗口。
                if (b"DIAG " in line or b"tp_home:" in line or b"tp_voice_audio:" in line
                        or b"tp_link:" in line or b"tp_wifi:" in line):
                    print("[dev] " + line.decode("utf-8", "replace").strip()[:240])
                elif any(k in line for k in CRASH_MARKS) or self.crash_lines:
                    # v4.0.5：设备崩溃时串口上的错误信息原来被当成噪声丢掉了 ——
                    # 两天里有一次 PANIC 重启，事后什么线索都没有。现在整段记下来
                    # (从标记行起最多 40 行，足够盖住寄存器转储和回溯)。
                    if any(k in line for k in CRASH_MARKS):
                        self.crash_lines = 40
                    self.crash_lines -= 1
                    print("[dev-crash] " + line.decode("utf-8", "replace").rstrip()[:300])
            else:
                out.append(packet)
        return out


def find_device_port(list_ports):
    """按 VID/PID 找 Tidepool 的 USB-Serial-JTAG 口。找不到 None。"""
    try:
        ports = list(list_ports())
    except Exception:
        return None
    hits = sorted(str(p.device) for p in ports
                  if getattr(p, "vid", None) == USB_VID and getattr(p, "pid", None) == USB_PID)
    return hits[0] if hits else None


class SerialLink(threading.Thread):
    """USB 线上的有线链路。自己一个线程，读写都在这里，主循环只碰两个队列。

    【为什么要有这条路】用户在图书馆，设备连不上笔记本的 Wi-Fi。USB 线本来就插着
    (供电)，ESP32-S3 的 USB-Serial-JTAG 口同时跑着固件日志 —— 协议行混在日志里，
    靠行首的 @@TP / @@TPA 标记区分，其它一律当噪声。

    【打开时绝不复位芯片】pyserial 默认 open() 时会拉 DTR/RTS，ESP32-S3 的
    USB-Serial-JTAG 把这个组合当成复位信号 —— 每连一次串口设备就重启一次。
    所以先建空对象、把 dtr/rts 设成 False，再 open()。

    【拔线/设备重启】端口会直接消失。任何串口异常都只关掉、2 秒后重新找，
    绝不让它带走整个接收器 —— Wi-Fi 那条路还要继续工作。

    【暂停文件】%TEMP%\\tidepool_serial.pause 存在时 0.5 秒内释放串口，删掉后再接上。
    烧录/截图工具要独占这个口，不能靠杀接收器进程来抢。
    """

    def __init__(self, forced_port=None, serial_mod=None, list_ports=None,
                 pause_file=SERIAL_PAUSE_FILE):
        super().__init__(name="tp-serial", daemon=True)
        self.serial_mod = serial_mod
        self.list_ports = list_ports or (lambda: [])
        self.forced_port = forced_port
        self.pause_file = pause_file
        self.inbox = queue.Queue(maxsize=SERIAL_INBOX_MAX)
        self.tx = queue.Queue(maxsize=64)
        self.control_tx = queue.Queue(maxsize=4)
        self.tx_lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.parser = SerialLineParser()
        self.ser = None
        self.port = None
        self.last_rx = None
        self.paused = False
        self.next_try = 0.0
        self.last_note = None
        self.inbox_drops = self.tx_drops = 0
        self._last_warn = 0.0
        self._write_started = None
        self._watchdog = threading.Thread(target=self._watch, name="tp-serial-wd", daemon=True)

    def start(self):
        super().start()
        self._watchdog.start()

    def _watch(self):
        """【实测卡死过】设备重启/进下载模式的那一刻，Windows 上对 USB-CDC 的
        WriteFile 会无视 write_timeout 一直挂着，整个串口线程随之停摆，
        而且一直占着 COM 口 —— 暂停文件也不管用了。
        这里从另一个线程盯着：一次写超过 2 秒就强行 cancel_write + close，
        让挂住的调用抛出来，主线程照常走"关掉、2 秒后重连"那条路。"""
        while not self.stop_flag.wait(0.5):
            started, ser = self._write_started, self.ser
            if started is None or ser is None or time.monotonic() - started < 2.0:
                continue
            print("[usb] 写串口卡住超过 2 秒(设备可能在重启)，强制释放 %s" % self.port)
            try:
                ser.cancel_write()
            except Exception:
                pass
            try:
                ser.close()
            except Exception:
                pass
            self._write_started = None

    # ---- 主线程调用 ----
    @property
    def is_open(self):
        return self.ser is not None

    def alive(self):
        """5 秒内收到过 @@TP/@@TPA 行。只看端口开着不算：设备可能还在启动。"""
        last = self.last_rx
        return self.ser is not None and last is not None and time.monotonic() - last < SERIAL_ALIVE_S

    def send(self, payload):
        """排一条 PC→设备 文本。真正写串口在本线程里做，主循环不会被卡住。"""
        if self.ser is None:
            return False
        payload = bytes(payload)
        if b"\n" in payload or b"\r" in payload:
            return False
        if len(payload) > SERIAL_TX_MAX:
            # 截断 JSON/报文只会让设备收到一条坏消息，宁可这一条不发。
            self._warn("[usb] 报文 %d 字节超过 %d，有线这条不发: %s"
                       % (len(payload), SERIAL_TX_MAX, payload[:32]))
            return False
        try:
            self.tx.put_nowait(payload)
        except queue.Full:
            self.tx_drops += 1
            return False
        return True

    def close(self):
        self.stop_flag.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=1.0)
        self._close(None)

    def control(self, request):
        """Only a live connection can accept a short-lived wake/sleep write."""
        if request.action not in ("wake", "sleep") or not self.alive() or self.paused:
            request.finish(False, error="USB device is not freshly connected")
            return
        request.deadline = min(request.deadline, time.monotonic() + 0.35)
        try:
            self.control_tx.put_nowait((self.ser, request))
        except queue.Full:
            request.finish(False, error="USB control busy")

    def _pump_control(self):
        try:
            expected, request = self.control_tx.get_nowait()
        except queue.Empty:
            return
        if request.expired() or expected is not self.ser or not self.alive() or self._pause_requested():
            request.finish(False, error="USB control expired or disconnected")
            return
        payload = ("TPSHOT " + request.action + "\n").encode("ascii")
        try:
            with self.tx_lock:
                if request.expired() or self.ser is not expected:
                    request.finish(False, error="USB connection changed")
                    return
                self._write_started = time.monotonic()
                if expected.write(payload) != len(payload):
                    raise RuntimeError("incomplete USB control write")
            request.finish(True, written=True, confirmed=False)
        except Exception as exc:
            request.finish(False, error=str(exc))
        finally:
            self._write_started = None

    # ---- 本线程 ----
    def _warn(self, text, every=10.0):
        now = time.monotonic()
        if now - self._last_warn >= every:
            self._last_warn = now
            print(text)

    def _note(self, key, text):
        """同一种状况只说一次，不每 2 秒刷屏。"""
        if self.last_note != key:
            self.last_note = key
            print(text)

    def _pause_requested(self):
        try:
            return os.path.exists(self.pause_file)
        except Exception:
            return False

    def _open(self):
        port = self.forced_port or find_device_port(self.list_ports)
        if not port:
            self._note("noport", "[usb] 没找到 Tidepool 的 USB 口(VID %04X PID %04X)；"
                                 "插上线会自动接上，Wi-Fi/UDP 照常" % (USB_VID, USB_PID))
            return False
        ser = self.serial_mod.Serial()
        ser.port = port
        ser.baudrate = SERIAL_BAUD
        ser.timeout = 0.05
        ser.write_timeout = 0.5
        # 必须在 open() 之前：见类说明，否则芯片会被复位。
        ser.dtr = False
        ser.rts = False
        try:
            ser.open()
        except Exception as exc:
            self._note("openfail:%s" % exc,
                       "[usb] %s 打不开(%s)；可能被烧录/监视工具占着，每 %d 秒再试"
                       % (port, exc, SERIAL_RETRY_S))
            return False
        with self.tx_lock:
            self.ser, self.port = ser, port
        self.parser.reset()
        self.last_rx = None
        self.last_note = None
        print("[usb] 已打开 %s(DTR/RTS 保持低，不复位芯片)；等设备的 @@TP" % port)
        return True

    def _close(self, reason):
        with self.tx_lock:
            ser, self.ser = self.ser, None
        self.last_rx = None
        self.parser.reset()
        while True:
            try:
                _, request = self.control_tx.get_nowait()
            except queue.Empty:
                break
            request.finish(False, error="USB disconnected")
        while True:              # 断开前排的报文属于上一个连接，不留给下一个
            try:
                self.tx.get_nowait()
            except queue.Empty:
                break
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
            if reason:
                print("[usb] 断开 %s：%s" % (self.port, reason))

    def _is_write_timeout(self, exc):
        cls = getattr(self.serial_mod, "SerialTimeoutException", None)
        return isinstance(cls, type) and isinstance(exc, cls)

    def _flush_tx(self):
        while True:
            try:
                payload = self.tx.get_nowait()
            except queue.Empty:
                return
            with self.tx_lock:
                ser = self.ser
                if ser is None:
                    return
                self._write_started = time.monotonic()
                try:
                    ser.write(b"TPSHOT link " + payload + b"\n")
                except Exception as exc:
                    if not self._is_write_timeout(exc):
                        raise
                    # 设备没在读(比如正忙着刷屏)：丢这一条，周期报文下一轮会补。
                    self.tx_drops += 1
                    self._warn("[usb] 写超时，丢弃一条下行报文(累计 %d)" % self.tx_drops)
                finally:
                    self._write_started = None

    def _deliver(self, packet):
        self.last_rx = time.monotonic()
        try:
            # 满了先等一会儿：等待期间不读串口，压力会顺着 USB 流控回到设备，
            # 比在这里悄悄丢帧好。
            self.inbox.put(packet, timeout=0.5)
        except queue.Full:
            self.inbox_drops += 1
            self._warn("[usb] 收件箱满，丢弃 %d 个包(主循环跟不上)" % self.inbox_drops)

    def _relay_debug_commands(self):
        """验收用：%TEMP%\\tidepool_serial.cmd 里每行一条设备命令(如 voice-start)，
        接收器代发后删掉文件。接收器独占串口时，这是不打断有线链路就能在线上
        做真实录音验收的唯一办法。只转发，不解析，不影响正常链路。"""
        path = self.pause_file[:-len(".pause")] + ".cmd" if self.pause_file.endswith(".pause") else None
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
            os.remove(path)
        except Exception:
            return
        with self.tx_lock:
            ser = self.ser
            if ser is None:
                return
            for line in lines[:8]:
                print("[usb] 代发调试命令: %s" % line)
                ser.write(b"TPSHOT " + line.encode("utf-8") + b"\n")

    def _pump(self):
        self._pump_control()
        self._flush_tx()
        self._relay_debug_commands()
        ser = self.ser
        waiting = ser.in_waiting
        data = ser.read(waiting if waiting else 1)
        if data:
            for packet in self.parser.feed(data):
                self._deliver(packet)

    def run(self):
        last_pause_check = 0.0
        pause = False
        while not self.stop_flag.is_set():
            try:
                now = time.monotonic()
                if self.ser is None or now - last_pause_check >= 0.2:
                    last_pause_check = now
                    pause = self._pause_requested()
                if pause:
                    if not self.paused:
                        self.paused = True
                        print("[usb] 发现 %s：释放串口，让烧录/截图工具用；删掉文件后自动恢复"
                              % self.pause_file)
                    self._close("暂停")
                    self.stop_flag.wait(0.25)
                    continue
                if self.paused:
                    self.paused = False
                    self.next_try = 0.0
                    print("[usb] 暂停文件已删除，恢复有线链路")
                if self.ser is None:
                    if now < self.next_try:
                        self.stop_flag.wait(0.25)
                        continue
                    if not self._open():
                        self.next_try = time.monotonic() + SERIAL_RETRY_S
                        continue
                self._pump()
            except Exception as exc:
                # SerialException / OSError(拔线、设备重启)以及任何意外：关掉重来。
                self._close("%s: %s" % (type(exc).__name__, exc))
                self.next_try = time.monotonic() + SERIAL_RETRY_S
        self._close(None)


def open_serial_link(args):
    """按命令行建有线链路。没装 pyserial / --no-serial → None，只走 UDP。"""
    if getattr(args, "no_serial", False):
        print("[usb] --no-serial：只走 Wi-Fi/UDP")
        return None
    if serial is None:
        print("[usb] 没装 pyserial，USB 有线链路不可用，只走 Wi-Fi/UDP。"
              "要用 USB 线：\"%s\" -m pip install pyserial" % sys.executable)
        return None
    link = SerialLink(getattr(args, "serial", None), serial, serial.tools.list_ports.comports)
    link.start()
    return link


# ---------------------------------------------------------------- 主循环
_resolved = {"name": None, "addr": None, "at": 0.0, "warned": False}


def resolve_device(name):
    """把设备名解析成 IP，结果缓存 30 秒。

    【为什么不写死 IP】接收器原来固定发往 192.168.200.128。路由器换了租约、
    设备跑到 .132 之后，那条招呼就发进了空气——设备上显示"电脑端未连接"，
    两边其实都正常，而且没有任何报错。所以默认改成解析固件注册的
    mDNS 名字 tidepool.local(Windows 10 起原生支持，不需要装东西)。

    解析不出来就原样返回：调用方照常 sendto，失败了也只是这一轮没发出去。
    设备自己还会主动发 TP_HELLO，那条路不依赖这里。
    """
    if not name:
        return name
    # 已经是点分 IP 就不用解析。
    try:
        socket.inet_aton(name)
        return name
    except OSError:
        pass
    now = time.time()
    if _resolved["name"] == name and now - _resolved["at"] < 30 and _resolved["addr"]:
        return _resolved["addr"]
    try:
        addr = socket.getaddrinfo(name, None, socket.AF_INET)[0][4][0]
    except OSError as exc:
        if not _resolved["warned"]:
            print("[link] 解析不到 %s(%s)；等设备主动招呼。"
                  "设备端需要 mDNS 已登记，或用 --device <IP> 指定。" % (name, exc))
            _resolved["warned"] = True
        _resolved["at"] = now
        return name
    if _resolved["addr"] != addr:
        print("[link] %s → %s" % (name, addr))
    _resolved.update(name=name, addr=addr, at=now, warned=False)
    return addr


class BackgroundProbes(threading.Thread):
    """把所有慢操作从实时收音循环里搬出来。

    【为什么必须搬】原来同一个 while 循环要做：UDP 收包、阻塞式声卡写入、
    每秒枚举并试开一个探测声卡流、tasklist 查询 OpenLess 进程、扫 ~/.claude
    目录统计项目、以及超时 15 秒的天气 HTTP 请求。
    任何一项慢下来，收包和播放就一起停 —— 一次天气超时足够让一整段语音
    出现空洞，而这种空洞在识别端表现为"漏字"，很难归因。

    现在这些全部在本线程里按各自的节奏跑，结果放进 snapshot；
    主循环只读 snapshot 再发出去，不做任何可能阻塞的事。
    """

    def __init__(self, args, sink):
        super().__init__(daemon=True)
        self.args = args
        self.sink = sink
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.weather_due = threading.Event()   # 设备重新上线时立刻补发
        self.snapshot = {
            "vmic": "unknown", "asr": "unknown",
            "agent_grams": [], "weather": None, "sun": None,
            "quota": None,
            "device_addr": None,
            "posix_tz": None,
        }
        # 天气用的位置。没开 --auto-location 就是命令行给的那个，永远不变。
        self.location = None
        if getattr(args, "lat", None) is not None and getattr(args, "lon", None) is not None:
            self.location = {"lat": args.lat, "lon": args.lon, "source": "cli",
                             "city": args.city or args.tz.split("/")[-1], "tz": args.tz}

    def get(self):
        with self.lock:
            return dict(self.snapshot)

    def refresh_location(self):
        """重新定位。挪动超过 3km 返回 True(调用方立刻重拉天气)。

        只在本线程里调：Windows 定位最多等 8 秒、IP 定位最多 6 秒×2，
        放进主循环就是一段语音的空洞。
        """
        loc = resolve_location(self.args)
        if loc is None:
            print("[location] 自动定位全部失败且没有 --lat/--lon，本轮不下发天气")
            return False
        old = self.location
        moved = (old is None or distance_km((old["lat"], old["lon"]),
                                            (loc["lat"], loc["lon"])) > LOCATION_MOVE_KM)
        if moved or old.get("source") != loc["source"] or old.get("city") != loc["city"]:
            # 只打两位小数(约 1km)：日志会被复制、被上传，精确坐标不该出现在里面。
            print("[location] 来源=%s 城市=%s 约 %.2f,%.2f tz=%s%s"
                  % (loc["source"], loc["city"], loc["lat"], loc["lon"], loc["tz"],
                     "（位置变了，重拉天气）" if moved and old is not None else ""))
        if moved:
            self.location = loc
        else:
            # 小于 3km 的抖动(IP 定位常见)不换坐标，免得天气反复重拉；名字可以更新。
            self.location = dict(old, city=loc["city"], source=loc["source"], tz=loc["tz"])
        return moved

    def run(self):
        last_probe = last_agent = 0.0
        last_weather = last_quota = 0.0
        last_rescan = 0.0
        last_location = float("-inf")
        last_tz = float("-inf")
        while not self.stop_flag.is_set():
            now = time.time()
            try:
                if now - last_tz >= 60.0:
                    # 用户可能在"设置"里改了时区；每分钟重算一次，很便宜。
                    last_tz = now
                    tz_text = local_posix_tz()
                    with self.lock:
                        self.snapshot["posix_tz"] = tz_text

                if getattr(self.args, "auto_location", False) and now - last_location >= LOCATION_REFRESH_S:
                    last_location = now
                    try:
                        if self.refresh_location():
                            self.weather_due.set()
                    except Exception as exc:
                        print("[location] 定位异常，沿用上次位置: %s" % exc)

                if now - last_probe >= 1.0:
                    last_probe = now
                    # 【定期重新枚举声卡】PortAudio 的设备表是初始化时缓存的，
                    # 电脑睡眠/恢复或插拔声卡之后索引会整体错位，而写入照样
                    # "成功" —— 音频进虚空(见 refresh_audio_devices 的说明)。
                    # 只在没有打开流的时候做：_terminate() 会把正在用的流拆掉。
                    rescan = now - last_rescan >= 30.0
                    if rescan:
                        last_rescan = now
                    # 【正在播放时不另开探测流】声卡已经开着就用它的真实状态，
                    # 再开一个探测流既浪费又可能和播放抢设备。
                    vmic = self.sink.probe(self.args.vmic, rescan=rescan)
                    # self.sink is the AudioWorker; the optional remote bridge
                    # belongs to its output sink.  Looking for it on the worker
                    # raised every second and hid all subsequent probe updates.
                    if self.sink.sink.remote is not None:
                        remote_state = self.sink.sink.remote.health()
                        # "error" 是 OpenLess 对上一段识别的状态事件(比如那一段太短)，
                        # 不代表软件坏了。只有连不上才是真故障；连不上就试着重连一次。
                        if remote_state == "offline":
                            self.sink.sink.remote.probe()
                            remote_state = self.sink.sink.remote.health()
                        asr = "ok" if remote_state != "offline" else "down"
                    else:
                        asr = probe_asr(self.args.asr_process)
                    with self.lock:
                        self.snapshot["vmic"] = vmic
                        self.snapshot["asr"] = asr
                    if self.args.device:
                        resolved = resolve_device(self.args.device)
                        try:
                            socket.inet_aton(resolved)
                        except OSError:
                            resolved = None
                        with self.lock:
                            self.snapshot["device_addr"] = resolved

                if not self.args.no_agent and now - last_agent >= 2.0:
                    last_agent = now
                    try:
                        grams = []
                        for i, rep in enumerate(agent_reports()):
                            if i:
                                rep.pop("projects", None)
                            grams += build_agent_datagrams(rep)
                    except Exception as exc:
                        print("[agent] 本轮不下发: %s" % exc)
                        grams = []
                    with self.lock:
                        self.snapshot["agent_grams"] = grams

                due = self.weather_due.is_set()
                loc = self.location
                if loc is not None and (due or now - last_weather > 1800):
                    self.weather_due.clear()
                    last_weather = now
                    try:
                        data_w = fetch_weather(loc["lat"], loc["lon"], loc["tz"])
                        payload, sun = build_weather_payload(self.args, data_w,
                                                             city=loc["city"])
                    except Exception as exc:
                        print("[weather] 拉取失败，本轮不下发: %s" % exc)
                    else:
                        with self.lock:
                            self.snapshot["weather"] = payload
                            self.snapshot["sun"] = sun
                        print("[weather] 已就绪 %s" % payload)
                if now - last_quota >= 5.0:
                    last_quota = now
                    lines = quota_lines()
                    with self.lock:
                        self.snapshot["quota"] = lines or None
            except Exception as exc:
                print("[probe] 后台探测异常，继续: %s" % exc)
            self.stop_flag.wait(0.25)


QUOTA_FILE = os.path.join(tempfile.gettempdir(), "tidepool_quota.json")
QUOTA_MAX_AGE_S = 900        # 15 分钟没更新就当没有，不拿陈旧额度冒充现在


CODEX_QUOTA_SCAN_FILES = 6          # 只看最近改过的几个会话记录
CODEX_QUOTA_TAIL_BYTES = 512 * 1024  # 每个文件只读尾巴：最后一次 token_count 就在末尾
CODEX_QUOTA_MAX_AGE_S = 30 * 86400


def _codex_windows(rate_limits, now):
    """Codex 的 rate_limits → (5h 已用, 长窗口已用, 重置剩余分钟, 长窗口分钟数)。
    primary/secondary 哪个是 5 小时、哪个是周/月，看 window_minutes，不看位置 ——
    实测 prolite 套餐只有一个 10080 分钟(周)窗口放在 primary。
    窗口已经过了 resets_at：那一轮额度已经重置，已用按 0 报，重置时间未知。"""
    if not isinstance(rate_limits, dict):
        return None
    short = long_ = None
    for key in ("primary", "secondary"):
        w = rate_limits.get(key)
        if not isinstance(w, dict):
            continue
        used, mins = w.get("used_percent"), w.get("window_minutes")
        if not isinstance(used, (int, float)) or not isinstance(mins, (int, float)) or mins <= 0:
            continue
        reset = w.get("resets_at")
        if isinstance(reset, (int, float)) and reset > 1e12:
            reset /= 1000.0                      # 毫秒时间戳也见过
        if not isinstance(reset, (int, float)):
            left = w.get("resets_in_seconds")
            reset = now + left if isinstance(left, (int, float)) else None
        if reset is not None and reset <= now:
            used, reset_min = 0, -1
        else:
            reset_min = int(max(0, reset - now) // 60) if reset is not None else -1
        rec = (max(0, min(100, int(round(used)))), reset_min, int(mins))
        if mins <= 360:
            short = rec
        elif long_ is None or mins < long_[2]:
            long_ = rec
    if short is None and long_ is None:
        return None
    u5 = short[0] if short else -1
    u7 = long_[0] if long_ else -1
    # 卡住你的是用得更多的那个窗口；一样多时报先重置的那个
    pick = max([r for r in (short, long_) if r], key=lambda r: (r[0], -r[1]))
    return u5, u7, pick[1], long_[2] if long_ else -1


def codex_quota(now=None, codex_dir=None):
    """读 Codex 自己写在会话记录里的额度(v4.0.4)。

    【来源】ChatGPT 账号登录的 Codex 每轮对话后都会写一条 token_count 事件，
    里面有 rate_limits(已用百分比、窗口长度、重置时间) —— 这是 Codex 服务端
    发回来的原值。v4.0.3 以为"本机没有任何文件写着额度"，其实漏了这里。
    返回 "TP_QUOTA codex <5h已用> <长窗口已用> <重置分钟> <长窗口分钟>" 或 None。"""
    now = time.time() if now is None else now
    root = os.path.join(codex_dir or CODEX_DIR, "sessions")
    files = []
    for path in glob.iglob(os.path.join(root, "*", "*", "*", "rollout-*.jsonl")):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if now - mtime <= CODEX_QUOTA_MAX_AGE_S:
            files.append((mtime, path))
    files.sort(reverse=True)
    for _mtime, path in files[:CODEX_QUOTA_SCAN_FILES]:
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - CODEX_QUOTA_TAIL_BYTES))
                tail = fh.read()
        except OSError:
            continue
        for raw in reversed(tail.split(b"\n")):
            if b'"rate_limits"' not in raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue                      # 截断的第一行
            payload = rec.get("payload") if isinstance(rec, dict) else None
            rl = payload.get("rate_limits") if isinstance(payload, dict) else None
            got = _codex_windows(rl, now)
            if got is None:
                continue
            u5, u7, reset_min, long_min = got
            return "TP_QUOTA codex %d %d %d %d" % (u5, u7, reset_min, long_min)
    return None


CLAUDE_DESKTOP_DIR = os.path.join(os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming"), "Claude")
PLAN_USAGE_FILE = os.path.join(CLAUDE_DESKTOP_DIR, "plan-usage-history.json")


def plan_usage_candidates():
    """桌面端是 MSIX 应用：它写 %APPDATA%/Claude 实际被系统重定向到
    %LOCALAPPDATA%/Packages/Claude_*/LocalCache/Roaming/Claude。
    从应用里面看是前一个路径，从接收器(包外)看只有后一个是真的 ——
    v4.0.4 第一次接上时就因为只看前一个，接收器里读不到文件。两处都找。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.join(HOME, "AppData", "Local")
    out = glob.glob(os.path.join(local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude",
                                 "plan-usage-history.json"))
    out.append(PLAN_USAGE_FILE)
    return [p for p in out if os.path.exists(p)]
PLAN_USAGE_5H_MAX_AGE_S = 2 * 3600     # 采样太旧就不报 5 小时窗口(可能早已重置)
PLAN_USAGE_7D_MAX_AGE_S = 24 * 3600


def claude_plan_usage(now=None, path=None):
    """Claude 账户(Pro/Max)的套餐用量，来自 Claude 桌面端自己的采样(v4.0.4)。

    【来源】桌面端每 15 分钟把 claude.ai 的用量写进 %APPDATA%/Claude/plan-usage-history.json：
        {"samples":[{"t":<毫秒>,"org":"...","u":{"fh":<5小时已用%>,"sd":<7天已用%>}}, ...]}
    这是本机文件，不需要任何登录凭据。文件里没有重置时间，所以重置时间报未知。
    返回 "TP_QUOTA claude <5h已用> <7天已用> -1 10080" 或 None。"""
    now = time.time() if now is None else now
    samples = []
    for p in ([path] if path else plan_usage_candidates()):
        try:
            with open(p, encoding="utf-8") as fh:
                samples += (json.load(fh) or {}).get("samples") or []
        except Exception:
            continue
    best = None
    for smp in samples:
        if not isinstance(smp, dict) or not isinstance(smp.get("u"), dict):
            continue
        t = smp.get("t")
        if isinstance(t, (int, float)) and (best is None or t > best["t"]):
            best = smp
    if best is None:
        return None
    age = now - best["t"] / 1000.0
    def pct(v):
        return max(0, min(100, int(round(v)))) if isinstance(v, (int, float)) else -1
    u5 = pct(best["u"].get("fh")) if age <= PLAN_USAGE_5H_MAX_AGE_S else -1
    u7 = pct(best["u"].get("sd")) if age <= PLAN_USAGE_7D_MAX_AGE_S else -1
    if u5 < 0 and u7 < 0:
        return None
    return "TP_QUOTA claude %d %d -1 10080" % (u5, u7)


def claude_session_mode(sessions=None, settings_path=None, environ=None):
    """当前在跑的 Claude 会话走的是账户还是中转(v4.0.4)。

    【v4.0.4 第一版的错误】只看全局 settings.json 里有没有中转配置 —— 但那份配置只管
    终端里的 claude(entrypoint=cli)；桌面端 Code 模式(entrypoint=claude-desktop)
    用的是 Claude 账户，于是设备把你的 Pro 会话说成了"走中转 · 按量计费"。
    现在看最前面那个活会话自己的 entrypoint。
    返回 "account" / "relay" / None(没有活会话)。"""
    sessions = claude_live_sessions() if sessions is None else sessions
    if not sessions:
        return None
    entry = sessions[0].get("entrypoint") or ""
    if entry.startswith("claude-desktop") or entry in ("sdk-desktop",):
        return "account"
    return "relay" if claude_uses_api_key(settings_path, environ) else "account"


def claude_uses_api_key(settings_path=None, environ=None):
    """Claude Code 是不是走 API Key / 中转站。那种情况下 Claude Code 根本不给
    订阅额度(rate_limits 只发给 Claude 订阅账号)，设备该说"按量计费"而不是"未上报"。"""
    environ = os.environ if environ is None else environ
    keys = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")
    if any(environ.get(k) for k in keys):
        return True
    try:
        with open(settings_path or os.path.join(CLAUDE_DIR, "settings.json"), encoding="utf-8") as fh:
            env = (json.load(fh) or {}).get("env") or {}
    except Exception:
        return False
    return isinstance(env, dict) and any(env.get(k) for k in keys)


_quota_diag = {"last": None}


def quota_lines():
    """每个助手一条 TP_QUOTA。拿不到就不发那一条，设备保持"未上报"。"""
    out = []
    mode = claude_session_mode()
    # 账户套餐的用量只要有就报(用户关心的是 Pro 额度)；只有拿不到、而且当前会话
    # 走中转时才说"按量计费"。v4.0.4 按"最前面那个会话"决定，两种会话并存时来回跳。
    plan = claude_plan_usage()
    why = "mode=%s plan=%s files=%d" % (mode, plan, len(plan_usage_candidates()))
    if why != _quota_diag["last"]:
        _quota_diag["last"] = why
        print("[quota] Claude 额度来源：%s" % why)
    if plan:
        out.append(plan)
    elif mode == "relay":
        # 终端里走中转的会话：按量计费，没有套餐额度可言
        out.append("TP_QUOTA claude api")
    else:
        # 账户模式(桌面端 Code、或者终端用账户登录)：优先桌面端的套餐采样，
        # 其次 statusline 快照(终端账户登录时才有)
        claude = plan or read_quota_snapshot()
        if claude:
            out.append(claude)
    try:
        codex = codex_quota()
    except Exception:
        codex = None
    if codex:
        out.append(codex)
    return out


def read_quota_snapshot():
    """读 statusline 包装脚本写下的额度快照，转成设备要的那一行。

    【额度为什么只能从这里拿】Claude Code 把 rate_limits(5 小时 / 7 天窗口的
    used_percentage 和 resets_at)只通过 stdin 喂给 statusLine 程序，
    任何文件、任何 API 里都没有。pc/tidepool_statusline.sh 接在那条管道上，
    把它落成一个本地快照，这里再读。

    读不到、太旧、格式不对 —— 一律返回 None，设备继续显示"额度未上报"。
    这里绝不能回退成一个看着合理的数字。
    """
    try:
        with open(QUOTA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    ts = d.get("ts")
    if not isinstance(ts, (int, float)) or time.time() - ts > QUOTA_MAX_AGE_S:
        return None          # 状态栏很久没刷新了，这份数据不能当现在用
    u5 = d.get("used_5h", -1)
    u7 = d.get("used_7d", -1)
    rm = d.get("reset_min", -1)
    if not all(isinstance(v, int) for v in (u5, u7, rm)):
        return None
    if u5 < 0 and u7 < 0:
        return None
    tool = str(d.get("tool") or "claude")[:15]
    return "TP_QUOTA %s %d %d %d" % (tool, u5, u7, rm)


def build_weather_payload(args, data_w, city=None):
    """把 open-meteo 的返回整理成设备要的两条报文。

    【按当前时间挑小时，不再固定取前四条】原来直接切 [:4]，那是当天 00:00
    起的四个小时 —— 晚上看到的是凌晨的天气。现在按返回的时间戳找到"当前
    或下一个整点"，从那里开始取四个，跨天不足就顺延。

    【"当前"是那个地方的当前】自动定位后地点不一定在本机时区里。返回里的
    utc_offset_seconds 是该地的偏移，拿它算当地现在几点；没有就退回本机时间。
    """
    hourly = data_w["hourly"]
    times = hourly["time"]
    temps = hourly["temperature_2m"]
    codes = hourly["weather_code"]
    offset = data_w.get("utc_offset_seconds")
    if isinstance(offset, (int, float)):
        now_local = (datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                     + datetime.timedelta(seconds=offset))
    else:
        now_local = datetime.datetime.now()
    start = 0
    for i, t in enumerate(times):
        try:
            ts = datetime.datetime.fromisoformat(t)
        except ValueError:
            continue
        if ts >= now_local.replace(minute=0, second=0, microsecond=0):
            start = i
            break
    sel = list(zip(temps[start:start + 4], codes[start:start + 4],
                   times[start:start + 4]))
    sunrise_min = minutes_of(data_w["daily"]["sunrise"][0])
    sunset_min = minutes_of(data_w["daily"]["sunset"][0])

    def is_night(tstr):
        """按当地日出日落判断夜间 —— 否则晚上也会画太阳。"""
        try:
            ts = datetime.datetime.fromisoformat(tstr)
        except ValueError:
            return False
        minute = ts.hour * 60 + ts.minute
        return minute < sunrise_min or minute >= sunset_min

    payload = json.dumps(
        {"city": city or args.city or args.tz.split("/")[-1],
         "h": [[round(t), icon_for(c, night=is_night(ts))] for t, c, ts in sel]},
        ensure_ascii=False, separators=(",", ":"))
    sun = "TP_SUN %d %d" % (sunrise_min, sunset_min)
    return payload, sun


def handle_peer_ping(data, addr, peer, active_session, device_addr, port, say):
    """Complete receiver-initiated discovery without trusting arbitrary UDP.

    A device can answer our READY with PING rather than HELLO. Accept only the
    current peer or the configured target resolved by BackgroundProbes, and never
    let another address replace an active recording's source.
    """
    if data != b"TP_PING":
        return False
    if active_session and active_session[1] != addr:
        return False
    # USB 线那头只可能是插在这台电脑上的设备，不需要"配置过的目标"这层核对。
    if addr != peer and addr != USB_ADDR and (not device_addr or addr != (device_addr, port)):
        return False
    say(b"TP_PONG", addr)
    return True


def main():
    parser = argparse.ArgumentParser(description="Tidepool receiver v%s" % VERSION)
    parser.add_argument("--vmic", default="CABLE Input", help="虚拟声卡输入端名字")
    parser.add_argument("--asr-process", default="", help="识别软件进程名，用于 asr 段")
    parser.add_argument("--outdir", default="recordings", help="落盘目录")
    parser.add_argument("--tee", action="store_true",
                        help="送虚拟声卡的同时也落一份 WAV。做 A8/A9 的四点链路"
                             "检查时要开 —— 否则正常工作时反而没有音频证据。")
    parser.add_argument("--openless-remote", action="store_true",
                        help="Use OpenLess Remote Input explicit start/stop protocol instead "
                             "of VB-CABLE plus a BLE toggle hotkey. Requires the local "
                             "OpenLess Remote Input service to be enabled.")
    parser.add_argument("--openless-remote-url", default="wss://127.0.0.1:8443/ws",
                        help="Loopback WebSocket endpoint of OpenLess Remote Input.")
    parser.add_argument("--openless-remote-pin-file",
                        default=os.path.join(os.path.expanduser("~"), "AppData", "Roaming",
                                             "com.openless.app", "remote-input-pin.txt"),
                        help="OpenLess-generated six digit pairing PIN file; its value is never logged.")
    parser.add_argument("--lat", type=float)
    parser.add_argument("--lon", type=float)
    parser.add_argument("--tz", default="Asia/Shanghai")
    parser.add_argument("--city", default="")
    parser.add_argument("--port", type=int, default=PORT,
                        help="UDP 端口。这台电脑上有个开机自启的旧接收器"
                             "(计划任务 Orbit Mic Receiver Watchdog)占着 17875，"
                             "换个端口比抢端口省事；设备端用 TPSHOT link-port 同步。")
    parser.add_argument("--no-agent", action="store_true",
                        help="不下发 TP_AGENT/TP_PROJ。这两项要读 ~/.claude 和 "
                             "~/.codex 下的会话记录并枚举进程；不想让接收器碰"
                             "这些就关掉，设备会显示未上报。")
    parser.add_argument("--device", default="tidepool.local",
                        help="设备地址，默认 tidepool.local(固件注册的 mDNS 主机名)。"
                             "给了就主动往它发招呼，不等它的广播——"
                             "Windows 防火墙默认拦截未授权程序的入站 UDP 广播，"
                             "而主动外发之后的回包是被放行的。"
                             "写死 IP 也行，但路由器换租约就会失效；名字不会。")
    parser.add_argument("--serial", metavar="COMx", default=None,
                        help="强制用这个串口做 USB 有线链路(默认按 VID 303A PID 1001 自动找)。")
    parser.add_argument("--no-serial", action="store_true",
                        help="不碰任何串口，只走 Wi-Fi/UDP。")
    parser.add_argument("--auto-location", action="store_true",
                        help="天气/日出日落的地点自动定位：Windows 定位服务 → IP 定位 → "
                              "--lat/--lon/--city。每 30 分钟刷新，挪动超过 3km 立刻重拉天气。")
    parser.add_argument("--no-control-api", action="store_true", help="Disable the local DotVoice control API.")
    parser.add_argument("--control-endpoint-file", default=os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "Tidepool", "control", "endpoint.json"))
    args = parser.parse_args()
    print("[tidepool] receiver v%s" % VERSION)

    bridge = None
    if args.openless_remote:
        bridge = OpenLessRemoteBridge(args.openless_remote_url, args.openless_remote_pin_file)
        HotkeyState.status = "remote"
        if not bridge.probe():
            print("[openless] remote probe failed: %s" % bridge.last_error)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # A second receiver must fail binding, not silently split the audio stream.
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    # 【Windows 的 UDP 坑，以及为什么这里没有代码】
    # 往一个不可达地址 sendto 之后，对方(或路由器)回一个 ICMP 端口不可达，
    # Windows 会把它变成【下一次 recvfrom 抛 WSAECONNRESET】。UDP 本来就是
    # 无连接的，一个目标不可达不该让整个接收器崩掉 —— 而它确实崩过
    # (日志里的 WinError 10054)。
    #
    # 标准做法是 WSAIoctl(SIO_UDP_CONNRESET, FALSE) 关掉这个行为，但
    # **CPython 做不到**：Windows 上 socket.ioctl() 只认 SIO_RCVALL /
    # SIO_KEEPALIVE_VALS / SIO_LOOPBACK_FAST_PATH 三个命令，socket 模块里
    # 根本没有 SIO_UDP_CONNRESET 这个常量。我试过直接传裸值 0x9800000C，
    # 换来的是 "invalid ioctl command 2550136844"。
    # 所以这里不做无用功，也不留一行每次启动都报错的日志 ——
    # 真正的保护是下面 recv 循环里的 except ConnectionResetError / OSError，
    # 那个已经在长跑中验证过了。要更彻底只能用 ctypes 直接调 WSAIoctl，
    # 为了省一次被接住的异常不值得引入那个复杂度。
    # 【绑定前先看这个端口是不是别人的】SO_REUSEADDR 会让 UDP 绑到一个已经
    # 有人在用的端口上而不报错，两个进程各收到一部分包 —— 表现为"时好时坏"，
    # 是最难查的一类。这台电脑上开机自启的 Orbit Mic Receiver 占着 17875。
    # 这里不抢、不杀别人的进程，只是把占用者指名道姓说出来，然后照常继续。
    try:
        import subprocess
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              "(Get-NetUDPEndpoint -LocalPort %d -EA SilentlyContinue |"
                              " Select-Object -First 1 -Expand OwningProcess)" % args.port],
                             capture_output=True, text=True, timeout=8).stdout.strip()
        if out.isdigit():
            name = subprocess.run(["powershell", "-NoProfile", "-Command",
                                   "(Get-Process -Id %s -EA SilentlyContinue).ProcessName" % out],
                                  capture_output=True, text=True, timeout=8).stdout.strip()
            print("[port] 注意：UDP %d 已经被 %s(pid %s) 占着。"
                  % (args.port, name or "某个进程", out))
            print("[port] 不去抢它。换个端口更稳：--port 17876")
    except Exception:
        pass                      # 查不出来不算错，照常绑
    sock.bind(("0.0.0.0", args.port))
    print("[port] 接收器监听 UDP %d；主动发现使用同一端口，设备配置需一致。"
          % args.port)
    sock.settimeout(0.05)   # async STOP completions must not wait an idle second
    print("[link] 监听 UDP %d" % args.port)

    link = open_serial_link(args)

    def say(payload, to):
        """发一个数据报，发不出去就说一声，绝不把整个接收器带走。

        【为什么必须包起来】实测崩过一次：
            OSError: [WinError 10051] 向一个无法连接的网络尝试了一个套接字操作
        笔记本切 Wi-Fi、睡醒、VPN 上下线都会让目的网络短暂不可达。裸的
        sendto 一抛异常，整个进程就退了 —— 录音链路、听写热键、智能体状态
        全跟着断，而真正的故障只是"这一个包没发出去"。
        发送失败不需要重试：TP_STATUS/TP_AGENT 都是每隔一两秒重发的周期包，
        下一轮自然会补上；设备那边超时就退回"未上报"，不会显示过期状态。

        v4.0.2：to == USB_ADDR 时走 USB 线(TPSHOT link <text>)，其它照旧走 UDP。"""
        if to == USB_ADDR:
            return bool(link is not None and link.send(payload))
        try:
            sock.sendto(payload, to)
            return True
        except OSError as exc:
            print("[link] 发送失败 %s: %s" % (to, exc))
            return False

    vmic, dev_index = (("ok", None) if bridge is not None else probe_vmic(args.vmic))
    print("[probe] vmic=%s asr=%s hk=%s"
          % (vmic, bridge.health() if bridge is not None else probe_asr(args.asr_process),
             HotkeyState.status))
    sink = AudioWorker(AudioSink(dev_index, args.outdir, tee=args.tee, remote=bridge))
    sessions = AudioSession(sink, say)
    # Resolve beside this receiver even when a test/launcher imports it by path.
    import importlib.util
    control_spec = importlib.util.spec_from_file_location(
        "tidepool_local_control", os.path.join(os.path.dirname(__file__), "tidepool_control.py"))
    control_module = importlib.util.module_from_spec(control_spec)
    control_spec.loader.exec_module(control_module)
    ControlServer, ReceiverControl = control_module.ControlServer, control_module.ReceiverControl
    control = ReceiverControl(sessions, link, bridge, VERSION, args.port)
    control_api = None
    if not args.no_control_api:
        try:
            control_api = ControlServer(args.control_endpoint_file)
            control_api.publish(control.state("offline"))
            control_api.start()
            print("[control] DotVoice loopback control available (token is not logged)")
        except Exception as exc:
            control_api = None
            print("[control] Local API unavailable: %s" % type(exc).__name__)
    probes = BackgroundProbes(args, sink)
    probes.start()
    sent_weather = None
    sent_quota = None
    quota_logged = [None]
    last_quota_tx = 0.0

    peer = None
    udp_peer = None          # 最近一次 Wi-Fi 对端；USB 掉了就退回它
    last_agent = 0.0
    sent_status = None
    sent_agent = None
    last_status = 0.0
    sent_tz = None
    last_weather_tx = 0.0
    last_time_tx = float("-inf")
    last_hail = 0.0
    last_usb_hail = float("-inf")
    serial_pending = deque()
    if args.device:
        print("[link] 后台解析设备 %s；等待有效报文确定对端" % args.device)

    def adopt(new_peer, why=None):
        """换对端。换了就把所有周期报文的计时器清零 —— 新对端第一秒就该拿到
        状态、天气、时间，而不是等各自的周期轮到。"""
        nonlocal peer, udp_peer, sent_weather, sent_quota
        nonlocal last_status, last_agent, last_quota_tx, last_weather_tx, last_time_tx
        if new_peer is not None and new_peer != USB_ADDR:
            udp_peer = new_peer
        if new_peer == peer:
            return
        peer = new_peer
        if peer is None:
            return
        probes.weather_due.set()
        sent_weather = sent_quota = None
        last_status = last_agent = last_quota_tx = last_time_tx = float("-inf")
        if why:
            print("[link] 对端 → %s（%s）" % ("USB 线" if peer == USB_ADDR else peer[0], why))

    def wired_alive():
        return link is not None and link.alive()

    def handle(data, addr, snap):
        """一个报文(UDP 数据报，或 USB 线上解出来的同样格式的包)。"""
        nonlocal sent_weather, last_time_tx, sent_tz
        # 有线在线时，Wi-Fi 那头的招呼照样回，但不把对端从 USB 抢走 ——
        # 除非它真开始录音(音频必须回到它来的那条路)。
        keep_wired = addr != USB_ADDR and wired_alive()
        if data[:3] == b"TP_":
            text = data.decode("utf-8", "replace").strip()
            verb = text.split(" ")[0]
            if verb == "TP_HELLO":
                if sessions.active and sessions.active[1] != addr:
                    return
                control.observe("usb" if addr == USB_ADDR else "wifi")
                # 【设备刚上线要立刻补发天气和日出日落】TP_WEATHER/TP_SUN
                # 原来只有 30 分钟一轮的定时器在发。设备重启之后那两份
                # 数据就丢了，要等最多半小时才回来 —— 表盘这段时间显示
                # "天气未配置 / 日出日落未知"，看着像功能坏了。
                # 收到 TP_HELLO 说明对面是新起来的，把计时器清零即可。
                # 设备重新上线：让后台线程立刻重拉一次，并允许重发
                if keep_wired:
                    udp_peer_seen(addr)
                else:
                    adopt(addr)
                    probes.weather_due.set()
                    sent_weather = None
                    last_time_tx = float("-inf")     # 设备刚起来，时钟是 1970
                if say(b"TP_READY", addr):
                    print("[link] %s ← TP_READY" % ("usb" if addr == USB_ADDR else addr[0],))
            elif verb == "TP_PING":
                if handle_peer_ping(data, addr, peer, sessions.active,
                                     snap["device_addr"], args.port, say):
                    control.observe("usb" if addr == USB_ADDR else "wifi")
                    if keep_wired:
                        udp_peer_seen(addr)
                    else:
                        adopt(addr)
            elif verb == "TP_START":
                if not control.ready_for_audio():
                    return
                fields = text.split()
                # v4.0.2：第三个字段 replay = 设备补发存下来的旧录音。
                replay = len(fields) == 3 and fields[2] == "replay"
                if (len(fields) == 2 or replay) and sessions.start(fields[1], addr, replay=replay):
                    control.observe("usb" if addr == USB_ADDR else "wifi")
                    adopt(addr)
                    print("[link] 开始接收%s音频 (%s)%s"
                          % ("补发" if replay else "", text, " via USB" if addr == USB_ADDR else ""))
            elif verb == "TP_INFO":
                # 4.0.8 固件：新接收器上线时报一次自己的版本(TP_BOOT 只在开机时发)。
                if addr == USB_ADDR or addr == peer:
                    for field in text.split()[1:]:
                        if field.startswith("fw="):
                            control.observe_firmware(field[3:])
            elif verb == "TP_BAT":
                # 4.0.6 固件：电量只在变化时随心跳捎带一包，不额外唤醒射频。
                if (addr == USB_ADDR or addr == peer) and control.observe_battery(text):
                    control.observe("usb" if addr == USB_ADDR else "wifi")
            elif verb == "TP_BOOT":
                fields = text.split()
                reasons = {"1": "上电", "3": "软件重启", "4": "PANIC 崩溃", "5": "中断看门狗",
                           "6": "任务看门狗", "7": "其它看门狗", "9": "欠压",
                           "11": "USB 复位(刷写/串口)", "12": "JTAG 复位"}
                code = fields[1] if len(fields) > 1 else "?"
                if len(fields) > 2:
                    control.observe_firmware(fields[2])
                bad = code in ("4", "5", "6", "7", "9")
                print("[boot] %s设备开机 · 固件 %s · 上次复位原因 %s(%s)"
                      % ("⚠ " if bad else "", fields[2] if len(fields) > 2 else "?",
                         reasons.get(code, "代码 " + code), code))
            elif verb == "TP_STOP":
                fields = text.split()
                if len(fields) == 2:
                    sessions.stop(fields[1], addr)
        elif len(data) in (4 + FRAME_BYTES, 4 + FRAME_BYTES2):
            if control.ready_for_audio() and sessions.feed(data, addr):
                control.observe("usb" if addr == USB_ADDR else "wifi")

    def udp_peer_seen(addr):
        nonlocal udp_peer
        udp_peer = addr

    try:
        while True:
            if control_api:
                control_api.drain(control.apply)
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                data, addr = None, None
            except ConnectionResetError:
                # 某个目标回了 ICMP 不可达。丢掉这一轮继续跑，不退出。
                data, addr = None, None
            except OSError as exc:
                print("[link] 收包出错，继续: %s" % exc)
                data, addr = None, None

            snap = probes.get()
            if data:
                handle(data, addr, snap)

            # USB 线上攒下的包。音频帧要看输出队列有没有余量：补发的录音可能
            # 比实时快得多，一口气塞进 12 帧的队列就会被判溢出、整段回执成 0。
            # 没余量就留到下一轮，串口线程那边会自然反压。
            if link is not None:
                for _ in range(64):
                    if not serial_pending:
                        try:
                            serial_pending.append(link.inbox.get_nowait())
                        except queue.Empty:
                            break
                    packet = serial_pending[0]
                    if (packet[:3] != b"TP_" and sessions.active and sessions.started
                            and sessions.active[1] == USB_ADDR and not sink.has_room()):
                        break
                    serial_pending.popleft()
                    handle(packet, USB_ADDR, snap)

            sessions.poll()
            now = time.monotonic()
            if control_api:
                control_api.publish(control.state("usb" if peer == USB_ADDR else "wifi" if peer else "offline"))

            # 【有线优先】USB 线那头在说话，就把对端切过去；正在另一条路上录音时不切，
            # 音频会话和它的回执必须留在来的那条路上。
            if link is not None:
                if wired_alive():
                    if peer != USB_ADDR and not (sessions.active and sessions.active[1] != USB_ADDR):
                        adopt(USB_ADDR, "有线在线，优先")
                elif peer == USB_ADDR and not (sessions.active and sessions.active[1] == USB_ADDR):
                    # 线拔了/设备重启：退回最近的 Wi-Fi 对端(没有就等它招呼)。
                    fallback = udp_peer
                    adopt(None)
                    if fallback is not None:
                        adopt(fallback, "USB 断开，退回 Wi-Fi")
                    else:
                        print("[link] USB 断开，等 Wi-Fi 那头招呼")
                # 端口开着但设备还没说话：每秒招呼一次。
                if link.is_open and not wired_alive() and now - last_usb_hail >= 1:
                    last_usb_hail = now
                    say(b"TP_READY", USB_ADDR)

            # 设备还没说过话时，每 2 秒主动发一次 TP_READY。
            # 它一旦回话(TP_HELLO/心跳/音频)，这里就停下。有线在线时不招呼 Wi-Fi 那头。
            # v4.0.6：原来条件里没看设备有没有回话，在线时也每 2 秒招呼一次。
            heard = control.seen.get("wifi")
            if (snap["device_addr"] and not data and not sessions.active and not wired_alive()
                    and (heard is None or time.monotonic() - heard > 10)
                    and now - last_hail > 2):
                last_hail = now
                try:
                    sock.sendto(b"TP_READY", (snap["device_addr"], args.port))
                except OSError as exc:
                    print("[link] 招呼发送失败: %s" % exc)
                except Exception as exc:
                    print("[link] 招呼发送异常: %s" % exc)

            # 【这一段现在只读后台线程算好的结果，不做任何可能阻塞的事】
            # 声卡探测、进程查询、~/.claude 扫描、天气 HTTP 全在 BackgroundProbes
            # 里跑。主循环慢一次，语音就缺一块，所以这里必须保持轻。

            # v4.0.3 省电：设备每收一包 Wi-Fi 都要醒一次射频。状态变了立刻发，
            # 没变就 3 秒一次当心跳(设备 5 秒判离线)，比原来每秒一次少 2/3 唤醒。
            msg = "TP_STATUS vmic=%s asr=%s hk=%s" % (
                snap["vmic"], snap["asr"] if sessions.enabled else "paused", HotkeyState.status)
            if peer and (now - last_status >= 3 or msg != sent_status):
                last_status = now
                sent_status = msg
                say(msg.encode(), peer)

            # 时间 + 本机时区。设备没有 RTC 电池，也连不上 NTP(图书馆里连 Wi-Fi 都没有)，
            # 所以时钟只能从这里来。60 秒一次足够抵消晶振漂移。
            # v4.0.3：只在新对端/设备重启时发，之后每小时校一次(晶振一小时漂移
            # 不到 0.2 秒)。时区变化(电脑换了时区)也会立刻补发一次。
            if peer and snap.get("posix_tz") and (now - last_time_tx >= 3600
                                                    or snap["posix_tz"] != sent_tz):
                msg = "TP_TIME %d %s" % (int(time.time() * 1000), snap["posix_tz"])
                if say(msg.encode(), peer):
                    if last_time_tx == float("-inf") or snap["posix_tz"] != sent_tz:
                        print("[time] 已下发 %s" % msg)
                    last_time_tx = now
                    sent_tz = snap["posix_tz"]

            # 助手状态：内容变了才发，否则 8 秒一次续命(设备 10 秒判过期)。
            # v4.0.6：elapsed 每秒都在变，不算"内容变了"——4.0.6 固件自己往前数秒，
            # 不然设备射频每秒都要被叫醒一次。
            grams = tuple(snap["agent_grams"])
            gist = agent_gist(grams)
            if peer and not args.no_agent and (gist != sent_agent and now - last_agent >= 1
                                               or now - last_agent >= 8):
                last_agent = now
                sent_agent = gist
                for gram in grams:
                    if not say(gram, peer):
                        break

            # 额度：只有真读到才发，每个助手一条。发不出去/读不到，设备保持"未上报"。
            # 30 秒一次续命(设备 120 秒判过期)，内容变了立刻发。
            quota = tuple(snap.get("quota") or ())
            if peer and quota and (now - last_quota_tx >= 30 or quota != sent_quota):
                last_quota_tx = now
                if all(say(q.encode(), peer) for q in quota) and quota != sent_quota:
                    sent_quota = quota
                    # 重置倒计时每分钟都在变，只在百分比变了时才记一行，免得日志刷屏
                    shown = tuple(" ".join(q.split()[:4]) for q in quota)
                    if shown != quota_logged[0]:
                        quota_logged[0] = shown
                        print("[quota] 已下发 %s" % " | ".join(quota))

            # The same IP may now belong to the rebooted watch. PING has no boot
            # identifier, so replay cached weather/sun every 30 seconds; this
            # does not trigger another HTTP fetch or depend on changed weather.
            weather_packet = (snap["weather"], snap["sun"])
            # 天气：变了才发，否则 10 分钟重发一次(设备重启后 TP_HELLO 会立刻触发补发)。
            if peer and snap["weather"] and (weather_packet != sent_weather
                                              or now - last_weather_tx >= 600):
                if (say(("TP_WEATHER " + snap["weather"]).encode(), peer)
                        and say(snap["sun"].encode(), peer)):
                    sent_weather = weather_packet
                    last_weather_tx = now
                    print("[weather] 已下发 %s" % snap["weather"])
    except KeyboardInterrupt:
        pass
    finally:
        if control_api:
            control_api.close()
        probes.stop_flag.set()
        sink.close()
        if bridge is not None:
            bridge.close()
        if link is not None:
            link.close()
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
