"""Doubao / Volcengine large-model streaming ASR (豆包流式语音识别, "sauc" bigmodel).

Protocol: https://www.volcengine.com/docs/6561/1354869 (大模型流式语音识别API).
Every WebSocket message is binary: a 4-byte header, then (for messages that carry one) a
4-byte big-endian sequence number, a 4-byte big-endian payload size and the payload.

  byte 0: protocol version (0b0001) << 4 | header size in 4-byte words (0b0001)
  byte 1: message type << 4 | message-type-specific flags
            types: 0b0001 full client request, 0b0010 audio only request,
                   0b1001 full server response, 0b1111 error message from server
            flags: 0b0000 no sequence, 0b0001 positive sequence follows,
                   0b0010 last packet without sequence, 0b0011 last packet, negative sequence
  byte 2: serialization (0b0000 none, 0b0001 JSON) << 4 | compression (0b0000 none, 0b0001 gzip)
  byte 3: reserved (0x00)
  error message: header + error code (uint32) + message size (uint32) + UTF-8 message

Like Volcengine's own demos (sauc_python), DotVoice numbers its packets: the full client
request is sequence 1, audio packets 2, 3, ... and the last packet carries the negated next
number with flags 0b0011. (`sequenced=False` sends the documented unnumbered variant.)

Auth (handshake headers): new console (豆包语音新版控制台) - X-Api-Key only; old console -
X-Api-App-Key (APP ID) + X-Api-Access-Key (Access Token). Both need X-Api-Resource-Id.

Audio goes in 200 ms packets ("单包为200ms大小时性能最优"); the rest after release goes at
once. Hotwords ride in request.corpus.context ({"hotwords": [{"word": ...}]}, about 100
tokens for bidirectional streaming). Should the service refuse them (45000001 or a close
before its first answer), the session is sent again at once without them, every packet
included, and this process stops sending them to that service - after a close that faults
no part of the request (a restart, "try again later") only when that keeps happening.

Manual check with a real key (values are read from the environment and never printed):
  set VOLC_API_KEY=...            (new console)   or   VOLC_APP_ID=... + VOLC_ACCESS_TOKEN=...
  set VOLC_RESOURCE_ID=volc.seedasr.sauc.duration   (optional)
  python -m bridge.engines.volc --wav sample.wav
"""
import gzip
import json
import os
import re
import struct
import uuid

import websocket

from .stream import BYTES_PER_SECOND, EngineError, Retry, StreamSession, check_ws_url, probe, resolve

HOST = 'wss://openspeech.bytedance.com/api/v3/sauc/'
# bigmodel_async: 双向流式优化版, answers only when the text changes (recommended by the docs);
# bigmodel: 双向流式, one answer per packet; bigmodel_nostream: 流式输入, text only at the end
# (or every 15 s) but more accurate.
ENDPOINTS = {'async': HOST + 'bigmodel_async', 'stream': HOST + 'bigmodel', 'nostream': HOST + 'bigmodel_nostream'}
RESOURCES = {
    'volc.seedasr.sauc.duration': '豆包流式语音识别模型 2.0 小时版',
    'volc.seedasr.sauc.concurrent': '豆包流式语音识别模型 2.0 并发版',
    'volc.bigasr.sauc.duration': '豆包流式语音识别模型 1.0 小时版',
    'volc.bigasr.sauc.concurrent': '豆包流式语音识别模型 1.0 并发版',
}
DEFAULT_RESOURCE = 'volc.seedasr.sauc.duration'

PROTOCOL_VERSION = 0b0001
HEADER_SIZE = 0b0001          # in 4-byte words
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ACK = 0b1011           # not used by ASR; parsed for robustness like the official demo
SERVER_ERROR = 0b1111
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010         # last packet, no sequence number
NEG_WITH_SEQUENCE = 0b0011    # last packet, negative sequence number
EVENT_FLAG = 0b0100           # newer protocol revisions add a 4-byte event number
NO_SERIALIZATION = 0b0000
JSON_SERIALIZATION = 0b0001
NO_COMPRESSION = 0b0000
GZIP = 0b0001

SUCCESS = 20000000
INVALID_PARAMETER = 45000001
EMPTY_AUDIO = 45000002
AUDIO_TIMEOUT = 45000081
BAD_AUDIO_FORMAT = 45000151
SERVER_ERRORS = {
    INVALID_PARAMETER: '请求参数无效',
    AUDIO_TIMEOUT: '等待音频超时（网络可能中断过）',
    BAD_AUDIO_FORMAT: '音频格式不正确',
    55000031: '服务器繁忙，请稍后重试',
}
# 20 ms of silence ends a session whose audio was an exact number of packets: the last
# packet then still carries audio, as in Volcengine's own demos.
SILENCE = bytes(640)
# The docs: 100-200 ms per packet, "单包为200ms大小时性能最优". VolcASR(packet_ms=None) keeps the
# shared 100 ms default; DotVoice's own configuration (cloud.create_backend) passes this.
PACKET_MS = 200
HOTWORD_BUDGET = 100          # the streaming API takes about 100 tokens of direct hotwords
CORPUS_WORDS = ('corpus', 'context', 'hotword', 'boosting')
# WebSocket close codes (RFC 6455) that reject what the request carried: unsupported (1003) or
# invalid (1007) data, a policy violation (1008), a message too big (1009). Before the
# service's first answer the hotwords may be why. Any other close - going away (1001),
# internal error (1011), restart (1012), try again later (1013) - says nothing about them.
REQUEST_CLOSES = (1003, 1007, 1008, 1009)
CLOSE_STRIKES = 2             # such closes in a row, each got past without hotwords, before they stop
# (resource, url) of the services that refused the hotword list in this process...
_corpus_refused = set()
# ...and of those that closed a request carrying it for no stated reason: {key: closes in a row}.
_corpus_doubts = {}


def header(message_type, flags, serialization, compression):
    return bytes([(PROTOCOL_VERSION << 4) | HEADER_SIZE, (message_type << 4) | flags,
                  (serialization << 4) | compression, 0x00])


def _gzip(data):
    return gzip.compress(data, compresslevel=6, mtime=0)


def full_client_request(payload, sequence=1):
    """gzip-compressed JSON request; sequence=None sends the unnumbered variant."""
    body = _gzip(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    if sequence is None:
        return header(FULL_CLIENT_REQUEST, NO_SEQUENCE, JSON_SERIALIZATION, GZIP) + struct.pack('>I', len(body)) + body
    return (header(FULL_CLIENT_REQUEST, POS_SEQUENCE, JSON_SERIALIZATION, GZIP)
            + struct.pack('>iI', sequence, len(body)) + body)


def audio_request(pcm, sequence=None, last=False):
    """gzip-compressed raw audio. The last packet gets a negative sequence (or, unnumbered, flag 0b0010)."""
    body = _gzip(bytes(pcm))
    if sequence is None:
        flags = NEG_SEQUENCE if last else NO_SEQUENCE
        return header(AUDIO_ONLY_REQUEST, flags, NO_SERIALIZATION, GZIP) + struct.pack('>I', len(body)) + body
    if last:
        sequence = -abs(sequence)
    elif sequence <= 0:
        raise ValueError('非最后一包的序号必须为正数')
    flags = NEG_WITH_SEQUENCE if last else POS_SEQUENCE
    return header(AUDIO_ONLY_REQUEST, flags, NO_SERIALIZATION, GZIP) + struct.pack('>iI', sequence, len(body)) + body


class Frame:
    __slots__ = ('version', 'message_type', 'flags', 'serialization', 'compression',
                 'sequence', 'last', 'event', 'code', 'payload')

    def __init__(self, **values):
        for name in self.__slots__:
            setattr(self, name, values.get(name))

    def __repr__(self):
        return (f'Frame(type={self.message_type:#06b}, flags={self.flags:#06b}, seq={self.sequence}, '
                f'last={self.last}, code={self.code})')


def parse_frame(data):
    """Decode any message of this protocol (server responses, errors, and client requests)."""
    data = bytes(data)
    if len(data) < 4:
        raise ValueError('数据帧过短')
    size = (data[0] & 0x0F) * 4
    if size < 4 or len(data) < size:
        raise ValueError('数据帧头长度无效')
    frame = Frame(version=data[0] >> 4, message_type=data[1] >> 4, flags=data[1] & 0x0F,
                  serialization=data[2] >> 4, compression=data[2] & 0x0F)
    body = data[size:]
    if frame.flags & POS_SEQUENCE:
        frame.sequence = struct.unpack('>i', body[:4])[0]
        body = body[4:]
    frame.last = bool(frame.flags & NEG_SEQUENCE)
    if frame.flags & EVENT_FLAG:
        frame.event = struct.unpack('>i', body[:4])[0]
        body = body[4:]
    if frame.message_type == SERVER_ERROR:
        if len(body) < 8:
            raise ValueError('错误帧不完整')
        frame.code, length = struct.unpack('>II', body[:8])
        payload = body[8:8 + length]
    elif len(body) < 4:
        if frame.message_type != SERVER_ACK:
            raise ValueError('数据帧缺少负载长度')
        length, payload = 0, b''
    else:
        (length,) = struct.unpack('>I', body[:4])
        payload = body[4:4 + length]
    if len(payload) != length:
        raise ValueError('数据帧不完整')
    if payload and frame.compression == GZIP:
        payload = gzip.decompress(payload)
    if frame.serialization == JSON_SERIALIZATION and payload:
        text = payload.decode('utf-8', 'replace')
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text   # error messages are sometimes plain text despite the JSON flag
    elif frame.message_type == SERVER_ERROR:
        payload = payload.decode('utf-8', 'replace')
    frame.payload = payload
    return frame


def response_text(payload):
    """The whole recognised text in a full server response (None when there is none)."""
    if not isinstance(payload, dict):
        return None
    result = payload.get('result')
    if isinstance(result, list):   # older revisions returned a list of candidates
        result = result[0] if result else None
    if not isinstance(result, dict):
        return None
    text = result.get('text')
    if isinstance(text, str):
        return text
    utterances = result.get('utterances')
    if isinstance(utterances, list):
        return ''.join(u.get('text', '') for u in utterances if isinstance(u, dict) and isinstance(u.get('text'), str))
    return None


def _detail(payload):
    if isinstance(payload, dict):
        for key in ('error', 'message', 'msg'):
            if isinstance(payload.get(key), str):
                return payload[key]
        return json.dumps(payload, ensure_ascii=False)
    return str(payload or '')


def server_error_message(code, payload):
    detail = _detail(payload).strip()[:80]
    known = SERVER_ERRORS.get(code)
    if known:
        message = f'豆包语音：{known}'
    elif code is not None and 55000000 <= code <= 55099999:
        message = f'豆包语音服务内部错误（{code}），请稍后重试'
    else:
        message = f'豆包语音识别失败（错误码 {code}）'
    return message + (f'：{detail}' if detail else '')


def corpus_refusal(code, payload):
    """Could this error be the hotword list's fault? 45000001 (invalid parameter), or another
    request error (45xxxxxx) that names it. Audio errors and timeouts never are."""
    if code == INVALID_PARAMETER:
        return True
    if not isinstance(code, int) or not 45000000 <= code <= 45099999:
        return False
    if code in (EMPTY_AUDIO, AUDIO_TIMEOUT, BAD_AUDIO_FORMAT):
        return False
    detail = _detail(payload).lower()
    return any(word in detail for word in CORPUS_WORDS)


class VolcSession(StreamSession):
    name = '豆包语音'
    source = 'volc'
    # Volcengine's demos wait for the answer to the full client request before sending
    # audio; the documented flow does not require it, so a late answer never stalls a session.
    ack_grace = 1.0

    def __init__(self, client, emit):
        super().__init__(emit)
        self.client = client
        self.request_id = str(uuid.uuid4())
        self.sequence = 1
        for name in ('connect_timeout', 'ready_timeout', 'final_timeout', 'ack_grace'):
            if getattr(client, name, None):
                setattr(self, name, getattr(client, name))
        self.packet = client.packet_bytes() or self.packet
        self.corpus = client.corpus_allowed()   # hotwords, unless this service refused them before
        self.sent_corpus = False
        self.confirmed = False                  # the server answered the request (no Retry after that)
        self.doubtful = False                   # retried after a close that named no reason

    def endpoint(self):
        return self.client.url, self.client.headers(self.request_id)

    def open(self, ws):
        payload = self.client.request_payload(corpus=self.corpus)
        self.sent_corpus = 'corpus' in payload['request']
        sequence = self.sequence if self.client.sequenced else None
        ws.send(full_client_request(payload, sequence), opcode=websocket.ABNF.OPCODE_BINARY)

    def retryable(self):
        return self.sent_corpus

    def restart(self, retry):
        # The same recording on a new connection: a new request, numbered from 1, no hotwords.
        self.corpus = False
        self.sequence = 1
        self.request_id = str(uuid.uuid4())
        self.confirmed = False
        self.doubtful = not retry.refused

    def send_audio(self, ws, chunk):
        self.sequence += 1
        sequence = self.sequence if self.client.sequenced else None
        ws.send(audio_request(chunk, sequence), opcode=websocket.ABNF.OPCODE_BINARY)

    def finish_audio(self, ws, remainder):
        self.sequence += 1
        sequence = -self.sequence if self.client.sequenced else None
        ws.send(audio_request(remainder or SILENCE, sequence, last=True), opcode=websocket.ABNF.OPCODE_BINARY)

    def on_binary(self, ws, data):
        try:
            frame = parse_frame(data)
        except (ValueError, OSError, EOFError, struct.error) as exc:   # (gzip raises OSError/EOFError)
            raise EngineError(f'豆包语音返回的数据无法解析（{exc}）') from None
        if frame.message_type == SERVER_ERROR:
            if frame.code == EMPTY_AUDIO:
                self.final('')   # nothing was heard: an empty result, not a failure
                return
            message = server_error_message(frame.code, frame.payload)
            if self.sent_corpus and not self.confirmed and corpus_refusal(frame.code, frame.payload):
                raise Retry(message)   # perhaps the hotwords: the same recording again without them
            raise EngineError(message)
        if frame.message_type != FULL_SERVER_RESPONSE:
            return
        if not self.confirmed:
            self.confirmed = True
            self.accept()   # the first response acknowledges the request; audio may follow
            if self.retries and not self.corpus:
                # Accepted without hotwords: stop sending them - at once after a refusal, after
                # a close for no stated reason only when it keeps happening (refuse_corpus).
                self.client.refuse_corpus(certain=not self.doubtful)
            elif self.sent_corpus:
                self.client.corpus_worked()   # accepted with them: earlier closes were not about them
        text = response_text(frame.payload)
        if frame.last:
            self.final(text or '')
        elif text:
            self.partial(text)

    def on_text(self, ws, text):
        raise EngineError('豆包语音返回了非预期的文本消息')

    def on_close(self, code, reason):
        if self.sent_corpus and not self.confirmed:
            # Closed before any answer: perhaps the hotwords, so the recording goes again without
            # them - a refusal of them only when the close faults the request (REQUEST_CLOSES).
            raise Retry(self.closed_error(code, reason), refused=code in REQUEST_CLOSES)
        super().on_close(code, reason)

    def handshake_error(self, status, body, headers):
        detail = (body or '').lower()
        resource = self.client.resource()
        if 'not granted' in detail or ('resource' in detail and status == 403):
            return (f'豆包语音未开通资源 {resource}：请在火山引擎控制台开通对应服务，'
                    '并确认小时版/并发版、模型 1.0/2.0 与 Resource ID 一致')
        if 'quota' in detail or 'balance' in detail or 'arrear' in detail or 'overdue' in detail:
            return '豆包语音额度已用完或账户欠费，请到火山引擎控制台查看'
        if 'concurren' in detail or 'limit' in detail or status == 429:
            return '豆包语音并发或调用频率超限，请稍后再试（并发版需要有空闲路数）'
        if status in (401, 403) or 'grant' in detail or 'auth' in detail or 'invalid' in detail:
            if self.client.auth_style() == 'api_key':
                return '豆包语音鉴权失败：API Key 不正确（需新版控制台的 API Key）'
            return '豆包语音鉴权失败：App ID 或 Access Token 不正确'
        if status >= 500:
            return f'豆包语音服务暂时不可用（HTTP {status}），请稍后重试'
        return f'豆包语音拒绝了连接（HTTP {status}）'


class VolcASR:
    """Configuration for Doubao streaming ASR; session() starts one recognition.

    Credentials may be strings or zero-argument callables (e.g. lambda: secrets.load(...)),
    read when a recording starts. auth: 'auto' (API Key if set, else App ID + Access Token),
    'api_key' (new console) or 'app' (old console). hotwords: a list, or a callable returning
    one (most useful first), also read when a recording starts. packet_ms: audio per packet
    (None: the shared 100 ms default; DotVoice passes PACKET_MS)."""
    name = '豆包语音'
    source = 'volc'

    def __init__(self, api_key=None, app_id=None, access_token=None, resource_id=DEFAULT_RESOURCE,
                 mode='async', url=None, auth='auto', punctuation=True, itn=True, ddc=False,
                 two_pass=True, end_window_size=None, hotwords=(), sequenced=True, packet_ms=None,
                 connect_timeout=None, ready_timeout=None, final_timeout=None, ack_grace=None):
        if mode not in ENDPOINTS:
            raise ValueError('豆包语音的识别模式无效')
        if auth not in ('auto', 'api_key', 'app'):
            raise ValueError('豆包语音的鉴权方式无效')
        self.api_key, self.app_id, self.access_token = api_key, app_id, access_token
        self.resource_id = resource_id
        self.mode, self.auth = mode, auth
        self.url = url or ENDPOINTS[mode]
        self.punctuation, self.itn, self.ddc = punctuation, itn, ddc
        self.two_pass, self.end_window_size = two_pass, end_window_size
        self.hotwords = hotwords if callable(hotwords) else list(hotwords or ())
        self.sequenced = sequenced
        self.packet_ms = packet_ms
        self.connect_timeout, self.ready_timeout, self.final_timeout = connect_timeout, ready_timeout, final_timeout
        self.ack_grace = ack_grace

    def __repr__(self):   # never shows credentials
        return f'VolcASR(mode={self.mode!r}, resource={self.resource()!r}, auth={self.auth_style()!r})'

    def resource(self):
        return resolve(self.resource_id) or ''

    def auth_style(self):
        if self.auth != 'auto':
            return self.auth
        return 'api_key' if resolve(self.api_key) else 'app'

    def check(self):
        """Cheap configuration check (no network). ValueError with a Chinese message."""
        check_ws_url(self.url, '豆包语音')
        resource = self.resource()
        if not resource:
            raise ValueError('豆包语音：请填写 Resource ID（例如 volc.seedasr.sauc.duration）')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.\-]{2,127}', resource):
            raise ValueError('豆包语音：Resource ID 格式不正确（例如 volc.seedasr.sauc.duration）')
        if self.auth_style() == 'api_key':
            if not _token(resolve(self.api_key)):
                raise ValueError('豆包语音：请填写新版控制台的 API Key（或改用 App ID + Access Token）')
        else:
            app_id, token = resolve(self.app_id), resolve(self.access_token)
            if not app_id or not token:
                raise ValueError('豆包语音：请填写 API Key（新版控制台），或 App ID 和 Access Token（旧版控制台）')
            if not re.fullmatch(r'\d{4,20}', str(app_id)):
                raise ValueError('豆包语音：App ID 应为纯数字（控制台“APP ID”一栏）')
            if not _token(token):
                raise ValueError('豆包语音：Access Token 格式不正确（不能含空格或中文）')

    def headers(self, request_id):
        headers = {'X-Api-Resource-Id': self.resource(), 'X-Api-Request-Id': request_id,
                   'X-Api-Connect-Id': str(uuid.uuid4()), 'X-Api-Sequence': '-1'}
        if self.auth_style() == 'api_key':
            key = resolve(self.api_key)
            if not _token(key):
                raise EngineError('豆包语音：未配置 API Key')
            headers['X-Api-Key'] = key
        else:
            app_id, token = resolve(self.app_id), resolve(self.access_token)
            if not app_id or not _token(token):
                raise EngineError('豆包语音：未配置 App ID / Access Token')
            headers['X-Api-App-Key'] = str(app_id)
            headers['X-Api-Access-Key'] = token
        return headers

    def packet_bytes(self):
        """Bytes per audio packet (whole samples), or None for the shared default."""
        if not self.packet_ms:
            return None
        return max(2, BYTES_PER_SECOND * int(self.packet_ms) // 1000 // 2 * 2)

    def hotword_source(self):
        """The hotwords now. A source that fails gives none: a dictionary that cannot be read
        never stops a recording."""
        try:
            words = resolve(self.hotwords)
        except Exception:
            return []
        if isinstance(words, str):
            return [words]
        try:
            return list(words or ())
        except TypeError:
            return []

    def corpus_allowed(self):
        return (self.resource(), self.url) not in _corpus_refused

    def refuse_corpus(self, certain=True):
        """This service refused the hotword list: none is sent to it again in this process.
        Not certain - it closed the request before answering, for no stated reason, and took it
        without them: only once that happened CLOSE_STRIKES times in a row (a one-off restart
        or dropped connection must not switch the dictionary off until DotVoice restarts)."""
        key = (self.resource(), self.url)
        if not certain:
            _corpus_doubts[key] = _corpus_doubts.get(key, 0) + 1
            if _corpus_doubts[key] < CLOSE_STRIKES:
                return
        _corpus_doubts.pop(key, None)
        _corpus_refused.add(key)

    def corpus_worked(self):
        """The service took a request with the hotword list: earlier closes were not about it."""
        _corpus_doubts.pop((self.resource(), self.url), None)

    def request_payload(self, corpus=True):
        request = {'model_name': 'bigmodel', 'enable_itn': bool(self.itn), 'enable_punc': bool(self.punctuation),
                   'enable_ddc': bool(self.ddc), 'show_utterances': False, 'result_type': 'full'}
        if self.mode == 'async' and self.two_pass:
            request['enable_nonstream'] = True   # 二遍识别: definite sentences are re-decoded more accurately
        if self.end_window_size:
            request['end_window_size'] = max(200, int(self.end_window_size))
        words = hotword_list(self.hotword_source()) if corpus else []
        if words:
            request['corpus'] = {'context': json.dumps({'hotwords': [{'word': w} for w in words]}, ensure_ascii=False)}
        return {'user': {'uid': 'dotvoice'},
                'audio': {'format': 'pcm', 'codec': 'raw', 'rate': 16000, 'bits': 16, 'channel': 1},
                'request': request}

    def session(self, emit):
        return VolcSession(self, emit)

    def probe(self, timeout=12):
        """User-triggered connection test (sends 0.5 s of silence): (ok, Chinese message)."""
        return probe(self, timeout)


def hotword_list(words, budget=HOTWORD_BUDGET):
    """Hotwords passed straight in the request, in the given order (most useful first); the
    streaming API accepts about 100 tokens, counted here as characters (never more tokens)."""
    result, used = [], 0
    for word in words or ():
        word = str(word).strip()
        if not word or word in result or used + len(word) > budget:
            continue   # (a word that does not fit is skipped; shorter ones after it may still fit)
        result.append(word)
        used += len(word)
    return result


def _token(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and value.isascii() and not any(c.isspace() for c in value)


def _from_env():
    return VolcASR(api_key=os.environ.get('VOLC_API_KEY') or None, app_id=os.environ.get('VOLC_APP_ID') or None,
                   access_token=os.environ.get('VOLC_ACCESS_TOKEN') or None,
                   resource_id=os.environ.get('VOLC_RESOURCE_ID') or DEFAULT_RESOURCE,
                   mode=os.environ.get('VOLC_MODE') or 'async', packet_ms=PACKET_MS)


def main(argv=None):
    from .stream import cli_wav_parser, configure_console, run_wav
    configure_console()
    parser = cli_wav_parser('用真实密钥测试豆包流式语音识别（密钥从环境变量读取，不会打印）')
    parser.add_argument('--mode', choices=sorted(ENDPOINTS), help='async（默认）/ stream / nostream')
    parser.add_argument('--resource', help='Resource ID，默认 ' + DEFAULT_RESOURCE)
    args = parser.parse_args(argv)
    client = _from_env()
    if args.mode:
        client.mode, client.url = args.mode, ENDPOINTS[args.mode]
    if args.resource:
        client.resource_id = args.resource
    try:
        client.check()
    except ValueError as exc:
        print(exc, '\n请设置环境变量 VOLC_API_KEY，或 VOLC_APP_ID 与 VOLC_ACCESS_TOKEN')
        return 2
    print(f'豆包语音：{client.url}，资源 {client.resource()}，鉴权方式 '
          + ('API Key（新版控制台）' if client.auth_style() == 'api_key' else 'App ID + Access Token（旧版控制台）'))
    return run_wav(client, args.wav, realtime=not args.fast)


if __name__ == '__main__':
    raise SystemExit(main())
