"""Shared machinery for the built-in cloud recognisers (bridge.engines.volc, .dashscope).

One recognition = one StreamSession = one WebSocket owned by one worker thread. The
controller thread only enqueues audio and reads events, so it never waits on DNS,
TLS or a slow network. The worker multiplexes sending and receiving on that socket
(select + SSL pending), so an SSL connection is never read and written from two
threads at once.

Events a session hands to its `emit` callback (from the worker thread):
  {"type": "partial", "text": <whole text so far>, "offset": 0, "final": False, "source": ...}
  {"type": "final", "text": <whole text>}                      exactly once, or instead:
  {"kind": "error", "message": <Chinese, actionable>, "source": ..., "logid"?: ...}
Nothing is emitted after cancel(). Never enable websocket.enableTrace(): it prints the
handshake, which carries the API key.

Packet size is per engine (StreamSession.packet): 100 ms by default, 200 ms for Volcengine.
A server that refuses an optional request field (hotwords, language hints) before it has
accepted the request makes the session raise Retry: the worker reconnects once without that
field and sends every packet again, so no audio is lost (see StreamSession._retry).
"""
import ipaddress
import queue
import select
import socket
import ssl
import struct
import sys
import threading
import time
from urllib.parse import urlparse

import websocket

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2          # S16LE mono
PACKET = BYTES_PER_SECOND // 10             # 100 ms: the default packet (DashScope recommends ~100 ms)
MAX_CHUNK = BYTES_PER_SECOND // 5           # a backlog (e.g. while connecting) goes out 200 ms per message
MAX_BACKLOG = BYTES_PER_SECOND * 120        # unsent audio beyond this: the network is not keeping up
REPLAY_LIMIT = BYTES_PER_SECOND * 60        # audio kept for a retry while the server has not answered
LOOPBACK = ['127.0.0.1', 'localhost', '::1']

_FINISH = object()
_CANCEL = object()


class EngineError(RuntimeError):
    """A recognition failure whose message the user can act on (Chinese)."""


class Retry(Exception):
    """Raised by a protocol hook: the server refused the request, perhaps because of an
    optional field the session can leave out. The worker reconnects once (restart() drops the
    field) and replays the audio; `message` (Chinese) is the error if it cannot retry.
    `refused`: the server said the request was at fault (an error naming it), not merely
    ended the connection before answering - only then is the field certainly the reason."""

    def __init__(self, message, refused=True):
        super().__init__(message)
        self.message = message
        self.refused = refused


def resolve(value):
    """A setting given as a value or as a zero-argument callable, read when used (so a key
    changed in settings applies to the next recording without rebuilding anything)."""
    value = value() if callable(value) else value
    return value.strip() if isinstance(value, str) else value


def is_loopback(host):
    if not host:
        return False
    if host.lower() == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


def check_ws_url(url, label):
    """wss:// anywhere; plain ws:// only to this PC (tests, local relays): keys never travel unencrypted."""
    try:
        parsed = urlparse(url or '')
        host = parsed.hostname
    except ValueError:
        host = None
        parsed = None
    if parsed is not None and host and (parsed.scheme == 'wss' or (parsed.scheme == 'ws' and is_loopback(host))):
        return url
    raise ValueError(f'{label}的服务地址无效，必须以 wss:// 开头')


def join_text(left, right):
    """Join recognised pieces: CJK runs join directly, Latin words keep a space between them."""
    if not left:
        return right or ''
    if not right:
        return left
    if (left[-1].isascii() and not left[-1].isspace() and right[0].isascii() and right[0].isalnum()
            and (left[-1].isalnum() or left[-1] in '.,!?;:%)"\'')):
        return left + ' ' + right
    return left + right


def body_text(body, limit=200):
    if body is None:
        return ''
    if isinstance(body, bytes):
        body = body.decode('utf-8', 'replace')
    return str(body)[:limit]


class StreamSession:
    """Base class: subclasses implement endpoint/open/send_audio/finish_audio/on_text/on_binary
    and call self.partial(), self.final() or raise EngineError; call self.accept() when the
    server has accepted the request (audio is only sent after that, or after ack_grace).
    A subclass that may raise Retry also implements retryable() and restart()."""
    name = '识别服务'          # Chinese service name used in messages
    source = 'cloud'
    connect_timeout = 6        # DNS excluded: TCP + TLS + WebSocket handshake
    ready_timeout = 8          # after the request: the server must accept it this fast
    ack_grace = None           # seconds: start streaming without an acknowledgement (None = wait for it)
    final_timeout = 20         # after the last audio: the final text must arrive this fast
    io_timeout = 5             # one send, or the rest of a frame already started, may take this long
    packet = PACKET            # bytes of audio per message while streaming (the last one may be shorter)
    retry_limit = 1            # reconnects after a Retry (one: a second refusal is reported)

    def __init__(self, emit):
        self._emit = emit
        self.audio = queue.Queue()
        self.cancelled = threading.Event()
        self.ended = threading.Event()     # final/error delivered, or cancelled
        self.lock = threading.Lock()
        self.backlog = 0
        self.overflow = False
        self.thread = None
        self.ws = None
        self.logid = ''
        self.ready = False
        self.finish_sent = False
        self.last_partial = ''
        self.stats = {'connected': None, 'first_partial': None, 'finish_sent': None, 'final': None}
        self.started_at = None
        self.retries = 0
        self.replay = None                 # audio sent on this connection before the server accepted it
        self.opened = None
        self.final_deadline = None

    # -- controller side: never blocks on the network --------------------------------
    def start(self):
        self.started_at = time.monotonic()
        self.thread = threading.Thread(target=self._run, name=self.source + '-asr', daemon=True)
        self.thread.start()

    def feed(self, pcm):
        if not pcm or self.ended.is_set() or self.overflow:
            return
        pcm = bytes(pcm)
        with self.lock:
            if self.backlog + len(pcm) > MAX_BACKLOG:
                self.overflow = True   # the worker reports it; it may be stuck in a send right now
                return
            self.backlog += len(pcm)
        self.audio.put(pcm)

    def finish(self):
        self.audio.put(_FINISH)

    def cancel(self):
        self.cancelled.set()
        self.ended.set()
        self.audio.put(_CANCEL)

    # -- helpers for subclasses --------------------------------------------------------
    def partial(self, text):
        if not isinstance(text, str) or text == self.last_partial:
            return
        self.last_partial = text
        if self.stats['first_partial'] is None:
            self.stats['first_partial'] = time.monotonic()
        self._deliver({'type': 'partial', 'text': text, 'offset': 0, 'final': False, 'source': self.source})

    def final(self, text):
        self.stats['final'] = time.monotonic()
        self._deliver({'type': 'final', 'text': text or '', 'source': self.source}, terminal=True)

    def accept(self):
        """The server accepted the request: audio may follow, and nothing sent so far needs to
        be kept for a retry."""
        self.ready = True
        self.replay = None

    def error(self, message):
        event = {'kind': 'error', 'message': message, 'source': self.source}
        if self.logid:
            event['logid'] = self.logid   # for a support ticket; never shown as the message
        self._deliver(event, terminal=True)

    def _deliver(self, event, terminal=False):
        with self.lock:
            if self.ended.is_set():
                return False
            if terminal:
                self.ended.set()
        try:
            self._emit(event)
        except Exception:
            pass   # a listener bug must not kill the socket owner
        return True

    # -- protocol hooks ----------------------------------------------------------------
    def endpoint(self):
        """(url, headers dict) for the WebSocket handshake."""
        raise NotImplementedError

    def open(self, ws):
        """Send the request that starts recognition."""
        raise NotImplementedError

    def send_audio(self, ws, chunk):
        raise NotImplementedError

    def finish_audio(self, ws, remainder):
        """Send the last audio (may be empty) and the end-of-audio marker."""
        raise NotImplementedError

    def on_text(self, ws, text):
        pass

    def on_binary(self, ws, data):
        pass

    def on_close(self, code, reason):
        """The server closed the connection before the session ended."""
        raise EngineError(self.closed_error(code, reason))

    def retryable(self):
        """After open(): whether a Retry is possible on this connection (audio is then kept
        until the server accepts the request)."""
        return False

    def restart(self, retry):
        """Before reconnecting after `retry`: leave out what the server refused."""

    def handshake_error(self, status, body, headers):
        if status in (401, 403):
            return f'{self.name}鉴权失败，请检查 API Key'
        if status == 429:
            return f'{self.name}请求过于频繁或额度不足，请稍后再试'
        if status >= 500:
            return f'{self.name}暂时不可用（HTTP {status}），请稍后重试'
        return f'{self.name}拒绝了连接（HTTP {status}）'

    def closed_error(self, code, reason):
        detail = f'（{code}{" " + reason if reason else ""}）' if code else ''
        return f'{self.name}提前关闭了连接{detail}，本段未完成'

    # -- worker ------------------------------------------------------------------------
    def _run(self):
        try:
            self._session()
        except EngineError as exc:
            self.error(str(exc))
        except websocket.WebSocketBadStatusException as exc:
            headers = exc.resp_headers or {}
            self.logid = self.logid or str(headers.get('x-tt-logid', '') or '')
            self.error(self.handshake_error(exc.status_code, body_text(exc.resp_body), headers))
        except (websocket.WebSocketTimeoutException, socket.timeout, TimeoutError):
            self.error(f'连接{self.name}超时，请检查网络后重试')
        except (websocket.WebSocketAddressException, socket.gaierror):
            self.error(f'无法连接{self.name}：域名解析失败，请检查网络')
        except ssl.SSLError:
            self.error(f'与{self.name}的加密连接失败（可能被代理或防火墙拦截）')
        except websocket.WebSocketProxyException:
            self.error(f'无法通过代理连接{self.name}，请检查代理设置')
        except ConnectionRefusedError:
            self.error(f'无法连接{self.name}（连接被拒绝），请检查网络')
        except (websocket.WebSocketConnectionClosedException, ConnectionError):
            self.error(f'与{self.name}的连接中断，本段未完成')
        except OSError:
            self.error(f'无法连接{self.name}，请检查网络')
        except websocket.WebSocketException:
            self.error(f'{self.name}的连接协议出错，本段未完成')
        except Exception as exc:   # an unexpected reply or a bug: report it, never die silently
            self.error(f'{self.name}返回了无法处理的数据（{type(exc).__name__}）')
        finally:
            self._close()
            self.ended.set()

    def _session(self):
        ws = self._connect()
        pending = bytearray()
        finishing = False
        while ws is not None and not self.ended.is_set():
            if self.overflow:
                raise EngineError(f'网络太慢，{self.name}跟不上录音（积压超过 2 分钟），本段已取消')
            if self.ready and not self.finish_sent:
                finishing = self._take(pending) or finishing
                packet = self.packet
                per_message = max(1, MAX_CHUNK // packet) * packet
                while len(pending) >= packet:
                    if self.ended.is_set() or self.overflow:
                        break   # cancelled (or doomed): the rest of a backlog is not uploaded
                    size = min(len(pending) // packet * packet, per_message)
                    self._send(ws, bytes(pending[:size]))
                    del pending[:size]
                    with self.lock:
                        self.backlog -= size
                if finishing and not self.ended.is_set() and not self.overflow:
                    # The rest goes at once, however short: release-to-final latency never
                    # waits for a packet to fill up.
                    with self.lock:
                        self.backlog -= len(pending)
                    self._finish(ws, bytes(pending))
                    pending.clear()
            if self._readable(ws, 0.01):
                try:
                    self._receive(ws)
                except Retry as retry:
                    ws = self._retry(retry, pending)
                    continue
            now = time.monotonic()
            if not self.ready and self.ack_grace is not None and now > self.opened + self.ack_grace:
                self.ready = True   # the protocol does not require the acknowledgement; frames stay in order
            if not self.ready and now > self.opened + self.ready_timeout:
                raise EngineError(f'{self.name}没有响应识别请求，请稍后重试')
            if self.final_deadline is not None and now > self.final_deadline and not self.ended.is_set():
                raise EngineError(f'{self.name}在 {self.final_timeout} 秒内没有返回最终结果，本段未完成')

    def _connect(self):
        """Open the WebSocket and send the request; None when cancelled meanwhile."""
        url, headers = self.endpoint()
        try:
            check_ws_url(url, self.name)
        except ValueError as exc:
            raise EngineError(str(exc)) from None
        host = urlparse(url).hostname
        options = {'header': headers, 'timeout': self.connect_timeout, 'suppress_origin': True,
                   'enable_multithread': True}
        if is_loopback(host):
            options['http_no_proxy'] = LOOPBACK   # (otherwise the user's proxy settings apply)
        ws = websocket.create_connection(url, **options)
        self.ws = ws
        if self.ended.is_set():
            return None
        if self.stats['connected'] is None:
            self.stats['connected'] = time.monotonic()
        response_headers = ws.getheaders() or {}
        self.logid = str(response_headers.get('x-tt-logid', '') or '')
        ws.settimeout(self.io_timeout)
        self.ready = False
        self.final_deadline = None
        self.open(ws)
        # Until the server accepts the request, the audio sent is kept: if it refuses an
        # optional field (Retry), the same session is sent again in full without it.
        self.replay = bytearray() if self.retries < self.retry_limit and self.retryable() else None
        self.opened = time.monotonic()
        return ws

    def _send(self, ws, chunk):
        self.send_audio(ws, chunk)
        self._keep(chunk)

    def _finish(self, ws, remainder):
        self.finish_audio(ws, remainder)
        self._keep(remainder)
        self.finish_sent = True
        self.stats['finish_sent'] = time.monotonic()
        self.final_deadline = time.monotonic() + self.final_timeout

    def _keep(self, chunk):
        if self.replay is None or not chunk:
            return
        if len(self.replay) + len(chunk) > REPLAY_LIMIT:
            self.replay = None   # no answer for a minute: a refusal now could not be undone anyway
        else:
            self.replay += chunk

    def _retry(self, retry, pending):
        """The server refused the request (Retry) before accepting it: reconnect once, send the
        request without the refused field, then every packet sent so far, in order, ahead of
        the audio still waiting. A finished recording is finished again on the new connection."""
        if self.replay is None or self.retries >= self.retry_limit or self.ended.is_set():
            raise EngineError(retry.message) from None
        self.retries += 1
        with self.lock:
            self.backlog += len(self.replay)   # unsent again: counted until it goes out a second time
        pending[:0] = self.replay
        self.replay = None
        self.finish_sent = False
        self._close(timeout=0.2)
        self.restart(retry)
        return self._connect()

    def _take(self, pending):
        """Move queued audio into `pending`; True once finish() was seen. Audio in `pending`
        still counts as backlog until it is sent."""
        finishing = False
        while True:
            try:
                item = self.audio.get_nowait()
            except queue.Empty:
                return finishing
            if item is _CANCEL:
                return finishing
            if item is _FINISH:
                finishing = True
                continue
            if finishing:   # fed after finish(): dropped
                with self.lock:
                    self.backlog -= len(item)
            else:
                pending += item

    @staticmethod
    def _readable(ws, timeout):
        sock = ws.sock
        if sock is None:
            raise websocket.WebSocketConnectionClosedException('socket closed')
        pending = getattr(sock, 'pending', None)
        if pending is not None and pending():
            return True   # already decrypted by the SSL layer: select() would not see it
        readable, _, _ = select.select([sock], [], [], timeout)
        return bool(readable)

    def _receive(self, ws):
        opcode, frame = ws.recv_data_frame(True)
        if opcode == websocket.ABNF.OPCODE_CLOSE:
            data = frame.data or b''
            code = struct.unpack('!H', data[:2])[0] if len(data) >= 2 else None
            reason = data[2:].decode('utf-8', 'replace')[:80]
            self.on_close(code, reason)
            raise EngineError(self.closed_error(code, reason))   # (an on_close that returned)
        if opcode == websocket.ABNF.OPCODE_TEXT:
            data = frame.data
            self.on_text(ws, data.decode('utf-8', 'replace') if isinstance(data, bytes) else data)
        elif opcode == websocket.ABNF.OPCODE_BINARY:
            self.on_binary(ws, frame.data)

    def _close(self, timeout=0.5):
        ws, self.ws = self.ws, None
        if ws is None:
            return
        try:
            if not self.cancelled.is_set():
                ws.close(timeout=timeout)
        except Exception:
            pass
        try:
            ws.shutdown()
        except Exception:
            pass


def probe(client, timeout=12):
    """User-triggered connection test (e.g. a settings "测试" button): one short session with
    0.5 s of silence. Returns (ok, Chinese message). Never called automatically."""
    try:
        client.check()
    except ValueError as exc:
        return False, str(exc)
    events = queue.Queue()
    session = client.session(events.put)
    session.start()
    session.feed(bytes(BYTES_PER_SECOND // 2))
    session.finish()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.1)
        except queue.Empty:
            continue
        if event.get('type') == 'final':
            return True, f'{client.name}连接正常'
        if event.get('kind') == 'error':
            return False, event['message']
    session.cancel()
    return False, f'{client.name}在 {timeout} 秒内没有响应'


# -- manual check: python -m bridge.engines.volc --wav file.wav --------------------------
def read_wav(path):
    """16 kHz mono S16LE PCM from a WAV file (converted with audioop if needed)."""
    import wave
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        import audioop
    with wave.open(str(path), 'rb') as wav:
        channels, width, rate = wav.getnchannels(), wav.getsampwidth(), wav.getframerate()
        data = wav.readframes(wav.getnframes())
    if width != 2:
        data = audioop.lin2lin(data, width, 2)
    if channels == 2:
        data = audioop.tomono(data, 2, 0.5, 0.5)
    elif channels != 1:
        raise ValueError('只支持单声道或双声道 WAV')
    if rate != SAMPLE_RATE:
        data, _ = audioop.ratecv(data, 2, 1, rate, SAMPLE_RATE, None)
    return data


def run_wav(client, path, realtime=True, chunk_ms=100, out=None):
    """Stream a WAV through client.session() like the pen would and print what comes back.
    Returns 0 on success. Keys are never printed."""
    out = out or sys.stdout
    pcm = read_wav(path)
    events = queue.Queue()
    session = client.session(events.put)
    chunk = BYTES_PER_SECOND * chunk_ms // 1000
    began = time.monotonic()
    stopped = None

    def outcome(event):
        """0 for the final text, 2 for an error (a session delivers one of them once), else None."""
        _show(event, began, out)
        if event.get('type') == 'final':
            after = f'（松开后 {time.monotonic() - stopped:.2f} 秒）' if stopped is not None else ''
            out.write(f'\n最终结果{after}：{event["text"]}\n')
            return 0
        if event.get('kind') == 'error':
            out.write('\n')
            return 2
        return None

    session.start()
    result = None
    for start in range(0, len(pcm), chunk):
        session.feed(pcm[start:start + chunk])
        if realtime:
            time.sleep(chunk_ms / 1000)
        while result is None and not events.empty():
            result = outcome(events.get())
        if result is not None:
            break   # e.g. a refused key: no point feeding the rest
    if result is None:
        stopped = time.monotonic()
        session.finish()
        deadline = stopped + session.final_timeout + 10
        while result is None and time.monotonic() < deadline:
            try:
                event = events.get(timeout=0.1)
            except queue.Empty:
                continue
            result = outcome(event)
    if result is None:
        out.write('\n等待结果超时\n')
        session.cancel()
        result = 1
    stats = session.stats
    if stats.get('connected') and session.started_at:
        out.write(f'连接耗时 {stats["connected"] - session.started_at:.2f} 秒')
        if stats.get('first_partial'):
            out.write(f'；首个实时文字 {stats["first_partial"] - session.started_at:.2f} 秒')
        out.write(f'；音频 {len(pcm) / BYTES_PER_SECOND:.1f} 秒\n')
    if session.logid:
        out.write(f'logid: {session.logid}\n')
    out.flush()
    return result


def _show(event, began, out):
    if event.get('type') == 'partial':
        out.write(f'\r[{time.monotonic() - began:5.2f}s] {event["text"]}')
    elif event.get('kind') == 'error':
        out.write(f'\n错误：{event["message"]}')
    out.flush()


def cli_wav_parser(description):
    import argparse
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--wav', required=True, help='16 kHz 单声道 WAV（其他格式会自动转换）')
    parser.add_argument('--fast', action='store_true', help='不按实时速度发送（默认按真实时长逐包发送）')
    return parser


def configure_console():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
