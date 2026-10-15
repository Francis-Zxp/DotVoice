"""Built-in cloud engines: Volcengine (Doubao) and Bailian (DashScope) streaming ASR, the
OpenAI-compatible polisher, CloudBackend and the DPAPI secret store.

Network tests talk to a minimal RFC 6455 server on 127.0.0.1 (stdlib only) and an
http.server fake; no real keys or services are involved."""
import base64
import gzip
import hashlib
import http.server
import io
import json
import queue
import socket
import struct
import tempfile
import threading
import time
import unittest
import uuid
import wave
from pathlib import Path

from bridge import secrets
from bridge.controller import Controller
from bridge.engines import cloud, dashscope, llm, stream, volc
from bridge.engines.cloud import CloudBackend
from bridge.engines.llm import PolishError, Polisher

try:
    from .test_bridge import FakeCapture, wait_for
except ImportError:
    from test_bridge import FakeCapture, wait_for

GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


# -- a minimal WebSocket server ----------------------------------------------------------
class FakeConnection:
    def __init__(self, sock):
        self.sock = sock
        self.buffer = b''
        self.path = None
        self.headers = {}
        self.closed = False

    def _fill(self, predicate):
        while not predicate():
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError('client went away')
            self.buffer += chunk

    def _read(self, n):
        self._fill(lambda: len(self.buffer) >= n)
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def handshake(self, reject=None):
        self.sock.settimeout(5)
        self._fill(lambda: b'\r\n\r\n' in self.buffer)
        head, self.buffer = self.buffer.split(b'\r\n\r\n', 1)
        lines = head.decode('latin-1').split('\r\n')
        self.path = lines[0].split(' ')[1]
        for line in lines[1:]:
            name, _, value = line.partition(':')
            self.headers[name.strip().lower()] = value.strip()
        if reject:
            status, body = reject
            body = body.encode('utf-8') if isinstance(body, str) else body
            self.sock.sendall((f'HTTP/1.1 {status} Rejected\r\nContent-Type: application/json\r\n'
                               f'Content-Length: {len(body)}\r\nX-Tt-Logid: reject-logid\r\n'
                               'Connection: close\r\n\r\n').encode() + body)
            return False
        accept = base64.b64encode(hashlib.sha1((self.headers['sec-websocket-key'] + GUID).encode()).digest())
        self.sock.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Accept: {accept.decode()}\r\nX-Tt-Logid: test-logid\r\n\r\n').encode())
        return True

    def recv(self, timeout=5):
        """(opcode, payload) of the next client message; pings are answered."""
        self.sock.settimeout(timeout)
        while True:
            if not self.buffer:
                self._fill(lambda: len(self.buffer) >= 1)   # a timeout here loses nothing
            b0, b1 = self._read(2)
            opcode, length = b0 & 0x0F, b1 & 0x7F
            if length == 126:
                (length,) = struct.unpack('>H', self._read(2))
            elif length == 127:
                (length,) = struct.unpack('>Q', self._read(8))
            mask = self._read(4) if b1 & 0x80 else b''
            payload = self._read(length)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._send(0xA, payload)
                continue
            if opcode == 0x8 and not self.closed:
                self.closed = True
                try:
                    self._send(0x8, payload[:2])
                except OSError:
                    pass
            return opcode, payload

    def _send(self, opcode, payload):
        n = len(payload)
        if n < 126:
            head = struct.pack('>BB', 0x80 | opcode, n)
        elif n < 65536:
            head = struct.pack('>BBH', 0x80 | opcode, 126, n)
        else:
            head = struct.pack('>BBQ', 0x80 | opcode, 127, n)
        self.sock.sendall(head + payload)

    def send_binary(self, data):
        self._send(0x2, data)

    def send_json(self, value):
        self._send(0x1, json.dumps(value, ensure_ascii=False).encode('utf-8'))

    def wait_gone(self, timeout=3):
        """Read until the client closes (or the timeout passes); returns the opcodes seen."""
        seen = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                opcode, _ = self.recv(timeout=max(0.05, deadline - time.monotonic()))
            except (OSError, ConnectionError):
                break
            seen.append(opcode)
            if opcode == 0x8:
                break
        return seen

    def close(self, code=1000):
        if not self.closed:
            self.closed = True
            try:
                self._send(0x8, struct.pack('>H', code))
            except OSError:
                pass


class FakeServer:
    def __init__(self, handler, reject=None):
        self.listener = socket.create_server(('127.0.0.1', 0))
        self.port = self.listener.getsockname()[1]
        self.handler, self.reject = handler, reject
        self.connections, self.errors = [], []
        self.done = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def url(self, path='/ws'):
        return f'ws://127.0.0.1:{self.port}{path}'

    def _serve(self):
        while True:
            try:
                sock, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(sock,), daemon=True).start()

    def _client(self, sock):
        conn = FakeConnection(sock)
        try:
            accepted = conn.handshake(self.reject)
            self.connections.append(conn)
            if accepted:
                self.handler(conn)
        except Exception as exc:   # surfaced by tests that care
            self.errors.append(exc)
        finally:
            time.sleep(0.05)
            try:
                sock.close()
            except OSError:
                pass
            self.done.set()

    def close(self):
        self.listener.close()


def collect(events, timeout=3):
    """Events from a session until its terminal event (final / error)."""
    seen = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.05)
        except queue.Empty:
            continue
        seen.append(event)
        if event.get('type') == 'final' or event.get('kind') == 'error':
            return seen
    raise AssertionError(f'no final/error event: {seen}')


# -- Volcengine frames built independently from the documented layout ------------------
def server_response(payload, sequence, last=False):
    body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    flags = 0b0011 if last else 0b0001
    return bytes([0x11, (0b1001 << 4) | flags, 0x11, 0x00]) + struct.pack('>iI', sequence, len(body)) + body


def server_error(code, message):
    body = message.encode('utf-8')
    return bytes([0x11, 0xF0, 0x10, 0x00]) + struct.pack('>II', code, len(body)) + body


def volc_script(log, partials=('你好',), final='你好世界。', error=None, ack=True, final_reply=True,
                close_code=None, send_ack=True):
    log.setdefault('audio', [])

    def handler(conn):
        log['headers'], log['path'] = dict(conn.headers), conn.path
        _, data = conn.recv()
        log['request_raw'] = data
        log['request'] = volc.parse_frame(data)
        if close_code:
            conn.close(close_code)
            conn.wait_gone()
            return
        if error is not None:
            conn.send_binary(server_error(*error))
            conn.close()
            return
        if not ack:
            conn.wait_gone()
            return
        if send_ack:
            conn.send_binary(server_response({'audio_info': {'duration': 0}, 'result': {'text': ''}}, 1))
        count = 0
        while True:
            try:
                opcode, data = conn.recv()
            except (OSError, ConnectionError):
                return
            if opcode != 0x2:
                return
            frame = volc.parse_frame(data)
            log['audio'].append(frame)
            sequence = frame.sequence if frame.sequence is not None else (-1 if frame.last else count + 2)
            if frame.last:
                if final_reply:
                    conn.send_binary(server_response({'audio_info': {'duration': 300}, 'result': {'text': final}},
                                                     sequence, last=True))
                    conn.close()
                else:
                    conn.wait_gone()
                return
            conn.send_binary(server_response({'result': {'text': partials[min(count, len(partials) - 1)]}}, sequence))
            count += 1
    return handler


def volc_client(server, **options):
    options.setdefault('api_key', 'test-api-key-0001')
    options.setdefault('ready_timeout', 2)
    options.setdefault('final_timeout', 2)
    return volc.VolcASR(url=server.url('/api/v3/sauc/bigmodel_async'), **options)


class VolcFrameTests(unittest.TestCase):
    def test_client_frames_match_the_documented_layout_byte_for_byte(self):
        payload = {'audio': {'format': 'pcm'}, 'request': {'model_name': 'bigmodel'}}
        body = gzip.compress(json.dumps(payload, ensure_ascii=False).encode('utf-8'), compresslevel=6, mtime=0)
        # Full client request: v1/4-byte header, type 0b0001 + flags 0b0001 (sequence 1), JSON + gzip.
        frame = volc.full_client_request(payload, sequence=1)
        self.assertEqual(frame[:12], b'\x11\x11\x11\x00' + b'\x00\x00\x00\x01' + struct.pack('>I', len(body)))
        self.assertEqual(frame[12:], body)
        # The documented unnumbered variant: flags 0b0000, payload size right after the header.
        self.assertEqual(volc.full_client_request(payload, sequence=None),
                         b'\x11\x10\x11\x00' + struct.pack('>I', len(body)) + body)
        pcm = bytes(range(256)) * 10
        audio = gzip.compress(pcm, compresslevel=6, mtime=0)
        # Audio only request: type 0b0010, no serialization, gzip; positive sequence 2.
        self.assertEqual(volc.audio_request(pcm, 2),
                         b'\x11\x21\x01\x00' + b'\x00\x00\x00\x02' + struct.pack('>I', len(audio)) + audio)
        # Last packet: flags 0b0011 and the sequence negated (-3 = ff ff ff fd).
        self.assertEqual(volc.audio_request(pcm, 3, last=True),
                         b'\x11\x23\x01\x00' + b'\xff\xff\xff\xfd' + struct.pack('>I', len(audio)) + audio)
        # Documented unnumbered last packet: flags 0b0010 ("最后一包音频请求").
        self.assertEqual(volc.audio_request(pcm, None, last=True),
                         b'\x11\x22\x01\x00' + struct.pack('>I', len(audio)) + audio)
        self.assertEqual(volc.audio_request(pcm, None), b'\x11\x20\x01\x00' + struct.pack('>I', len(audio)) + audio)
        with self.assertRaises(ValueError):
            volc.audio_request(pcm, 0)

    def test_round_trips_and_server_frames(self):
        payload = {'user': {'uid': 'dotvoice'}, 'request': {'model_name': 'bigmodel', 'enable_punc': True}}
        frame = volc.parse_frame(volc.full_client_request(payload, 1))
        self.assertEqual((frame.message_type, frame.flags, frame.sequence, frame.last), (0b0001, 0b0001, 1, False))
        self.assertEqual(frame.payload, payload)
        last = volc.parse_frame(volc.audio_request(b'\x01\x02' * 100, 7, last=True))
        self.assertEqual((last.message_type, last.sequence, last.last, last.payload),
                         (0b0010, -7, True, b'\x01\x02' * 100))
        # Full server response (doc example: flags b0001, sequence 0x00000001, JSON + gzip).
        response = volc.parse_frame(server_response({'result': {'text': '你好'}, 'audio_info': {'duration': 10}}, 1))
        self.assertEqual((response.message_type, response.sequence, response.last), (0b1001, 1, False))
        self.assertEqual(volc.response_text(response.payload), '你好')
        final = volc.parse_frame(server_response({'result': {'text': '你好。'}}, -5, last=True))
        self.assertEqual((final.flags, final.sequence, final.last), (0b0011, -5, True))
        # Error message from server: type b1111, flags b0000, JSON, no compression, code + size + message.
        error = volc.parse_frame(server_error(45000151, '{"error":"invalid audio format"}'))
        self.assertEqual((error.message_type, error.code), (0b1111, 45000151))
        self.assertEqual(error.payload, {'error': 'invalid audio format'})
        self.assertIn('音频格式不正确', volc.server_error_message(error.code, error.payload))
        self.assertIn('服务内部错误', volc.server_error_message(55000123, 'x'))
        plain = volc.parse_frame(server_error(45000001, 'missing field'))
        self.assertEqual(plain.payload, 'missing field')
        for bad in (b'', b'\x11\x91', b'\x11\x91\x11\x00\x00\x00\x00\x01\x00\x00\x00\x09{}'):
            with self.assertRaises(ValueError):
                volc.parse_frame(bad)
        self.assertEqual(volc.response_text({'result': [{'text': '旧格式'}]}), '旧格式')
        self.assertEqual(volc.response_text({'result': {'utterances': [{'text': '甲'}, {'text': '乙'}]}}), '甲乙')
        self.assertIsNone(volc.response_text({'audio_info': {}}))

    def test_request_payload_and_configuration_checks(self):
        client = volc.VolcASR(api_key='k-1', hotwords=['点声', '豆包', '点声', ''])
        request = client.request_payload()
        self.assertEqual(request['audio'], {'format': 'pcm', 'codec': 'raw', 'rate': 16000, 'bits': 16, 'channel': 1})
        self.assertEqual(request['request']['model_name'], 'bigmodel')
        self.assertTrue(request['request']['enable_nonstream'])   # two-pass on the async endpoint
        self.assertEqual(json.loads(request['request']['corpus']['context']),
                         {'hotwords': [{'word': '点声'}, {'word': '豆包'}]})
        self.assertNotIn('enable_nonstream', volc.VolcASR(api_key='k', mode='stream').request_payload()['request'])
        self.assertEqual(client.url, 'wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async')
        client.check()
        with self.assertRaisesRegex(ValueError, 'API Key'):
            volc.VolcASR().check()
        with self.assertRaisesRegex(ValueError, '纯数字'):
            volc.VolcASR(app_id='abc', access_token='tok').check()
        with self.assertRaisesRegex(ValueError, 'Resource ID'):
            volc.VolcASR(api_key='k', resource_id='').check()
        with self.assertRaisesRegex(ValueError, 'wss://'):
            volc.VolcASR(api_key='k', url='ws://example.com/x').check()
        volc.VolcASR(app_id='1234567890', access_token=lambda: 'token-abc').check()   # lazy credential
        self.assertNotIn('k-1', repr(client))
        self.assertEqual(volc.hotword_list(['一' * 60, '二' * 50, '三']), ['一' * 60, '三'])


class VolcSessionTests(unittest.TestCase):
    def serve(self, handler, reject=None):
        server = FakeServer(handler, reject)
        self.addCleanup(server.close)
        return server

    def test_streams_numbered_packets_maps_partials_and_sends_the_last_packet_on_finish(self):
        log = {}
        server = self.serve(volc_script(log, partials=('你好',), final='你好世界。'))
        events = queue.Queue()
        session = volc_client(server).session(events.put)
        session.start()
        chunks = [bytes([i]) * 1600 for i in (1, 2, 3)]   # 3 x 50 ms
        for chunk in chunks:
            session.feed(chunk)
        session.finish()
        seen = collect(events)
        self.assertEqual(seen, [
            {'type': 'partial', 'text': '你好', 'offset': 0, 'final': False, 'source': 'volc'},
            {'type': 'final', 'text': '你好世界。', 'source': 'volc'}])
        request = log['request']
        self.assertEqual((request.message_type, request.sequence), (volc.FULL_CLIENT_REQUEST, 1))
        self.assertEqual(request.payload['request']['model_name'], 'bigmodel')
        self.assertEqual(log['path'], '/api/v3/sauc/bigmodel_async')
        frames = log['audio']
        self.assertEqual([f.sequence for f in frames], [2, -3])   # 100 ms packet, then the rest as last
        self.assertEqual([f.last for f in frames], [False, True])
        self.assertEqual(b''.join(f.payload for f in frames), b''.join(chunks))
        session.thread.join(2)
        self.assertEqual(session.logid, 'test-logid')

    def test_auth_headers_new_console(self):
        log = {}
        server = self.serve(volc_script(log, final=''))
        events = queue.Queue()
        session = volc_client(server, api_key='new-console-key').session(events.put)
        session.start()
        session.finish()   # no audio at all: the last packet carries 20 ms of silence
        self.assertEqual(collect(events), [{'type': 'final', 'text': '', 'source': 'volc'}])
        headers = log['headers']
        self.assertEqual(headers['x-api-key'], 'new-console-key')
        self.assertEqual(headers['x-api-resource-id'], 'volc.seedasr.sauc.duration')
        self.assertEqual(headers['x-api-sequence'], '-1')
        uuid.UUID(headers['x-api-request-id'])
        uuid.UUID(headers['x-api-connect-id'])
        self.assertNotIn('x-api-app-key', headers)
        self.assertNotIn('x-api-access-key', headers)
        self.assertNotIn('origin', headers)
        self.assertEqual([(f.sequence, f.last, f.payload) for f in log['audio']], [(-2, True, bytes(640))])

    def test_auth_headers_old_console(self):
        log = {}
        server = self.serve(volc_script(log, final='好'))
        events = queue.Queue()
        session = volc_client(server, api_key=None, app_id='1234567890', access_token='old-token',
                              resource_id='volc.bigasr.sauc.duration').session(events.put)
        session.start()
        session.feed(bytes(3200))
        session.finish()
        self.assertEqual(collect(events)[-1]['text'], '好')
        headers = log['headers']
        self.assertEqual((headers['x-api-app-key'], headers['x-api-access-key']), ('1234567890', 'old-token'))
        self.assertEqual(headers['x-api-resource-id'], 'volc.bigasr.sauc.duration')
        self.assertNotIn('x-api-key', headers)

    def test_unnumbered_variant(self):
        log = {}
        server = self.serve(volc_script(log, final='嗯'))
        events = queue.Queue()
        session = volc_client(server, sequenced=False).session(events.put)
        session.start()
        session.feed(bytes(4000))
        session.finish()
        self.assertEqual(collect(events)[-1]['type'], 'final')
        self.assertEqual(log['request'].flags, volc.NO_SEQUENCE)
        self.assertEqual([(f.flags, f.sequence) for f in log['audio']],
                         [(volc.NO_SEQUENCE, None), (volc.NEG_SEQUENCE, None)])

    def test_error_frame_becomes_a_chinese_error_event(self):
        server = self.serve(volc_script({}, error=(45000151, '{"error":"bad format"}')))
        events = queue.Queue()
        session = volc_client(server).session(events.put)
        session.start()
        session.feed(bytes(3200))
        seen = collect(events)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]['kind'], 'error')
        self.assertIn('音频格式不正确', seen[0]['message'])
        self.assertEqual(seen[0]['logid'], 'test-logid')

    def test_empty_audio_error_is_an_empty_result(self):
        server = self.serve(volc_script({}, error=(volc.EMPTY_AUDIO, 'empty audio')))
        events = queue.Queue()
        session = volc_client(server).session(events.put)
        session.start()
        session.finish()
        self.assertEqual(collect(events), [{'type': 'final', 'text': '', 'source': 'volc'}])

    def test_handshake_rejections_are_explained(self):
        cases = [
            (403, '{"error":"[resource_id=volc.seedasr.sauc.duration] requested resource not granted"}', '未开通资源'),
            (401, '{"error":"load grant: requested grant not found in SaaS storage"}', 'API Key 不正确'),
            (429, '{"error":"too many"}', '并发'),
            (503, '', '暂时不可用'),
        ]
        for status, body, expected in cases:
            server = self.serve(lambda conn: None, reject=(status, body))
            events = queue.Queue()
            session = volc_client(server).session(events.put)
            session.start()
            seen = collect(events)
            self.assertEqual(seen[0]['kind'], 'error')
            self.assertIn(expected, seen[0]['message'], (status, seen))
            self.assertNotIn('test-api-key-0001', seen[0]['message'])
            self.assertEqual(seen[0]['logid'], 'reject-logid')

    def test_cancel_never_sends_the_last_packet_and_emits_nothing(self):
        log = {}
        server = self.serve(volc_script(log))
        events = queue.Queue()
        session = volc_client(server).session(events.put)
        session.start()
        session.feed(bytes(6400))
        wait_for(lambda: len(log['audio']) >= 1)
        session.cancel()
        session.feed(bytes(3200))
        session.finish()
        self.assertTrue(server.done.wait(3))
        self.assertFalse(any(f.last for f in log['audio']))
        time.sleep(0.1)
        self.assertTrue(all(e.get('type') == 'partial' for e in list(events.queue)))
        session.thread.join(2)
        self.assertFalse(session.thread.is_alive())

    def test_no_acknowledgement_or_no_final_times_out(self):
        server = self.serve(volc_script({}, ack=False))
        events = queue.Queue()
        session = volc_client(server, ready_timeout=0.3).session(events.put)
        session.start()
        self.assertIn('没有响应', collect(events)[-1]['message'])
        server = self.serve(volc_script({}, final_reply=False))
        events = queue.Queue()
        session = volc_client(server, final_timeout=0.3).session(events.put)
        session.start()
        session.feed(bytes(3200))
        session.finish()
        self.assertIn('没有返回最终结果', collect(events)[-1]['message'])

    def test_a_missing_acknowledgement_does_not_stall_the_session(self):
        log = {}
        server = self.serve(volc_script(log, final='照常完成。', send_ack=False))
        events = queue.Queue()
        session = volc_client(server, ack_grace=0.2).session(events.put)
        session.start()
        session.feed(bytes(3200))
        session.finish()
        self.assertEqual(collect(events)[-1], {'type': 'final', 'text': '照常完成。', 'source': 'volc'})
        self.assertEqual([f.sequence for f in log['audio']], [2, -3])

    def test_server_closing_early_is_an_error(self):
        server = self.serve(volc_script({}, close_code=4003))
        events = queue.Queue()
        session = volc_client(server).session(events.put)
        session.start()
        message = collect(events)[-1]['message']
        self.assertIn('提前关闭了连接', message)
        self.assertIn('4003', message)

    def test_backlog_overflow_cancels_instead_of_growing_forever(self):
        server = self.serve(volc_script({}, ack=False))
        original = stream.MAX_BACKLOG
        stream.MAX_BACKLOG = 6400
        self.addCleanup(setattr, stream, 'MAX_BACKLOG', original)
        events = queue.Queue()
        session = volc_client(server, ready_timeout=5).session(events.put)
        session.start()
        for _ in range(4):
            session.feed(bytes(3200))
        self.assertIn('积压', collect(events)[-1]['message'])

    def test_connection_refused_is_reported_without_blocking_the_caller(self):
        listener = socket.create_server(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.close()
        events = queue.Queue()
        client = volc.VolcASR(api_key='k', url=f'ws://127.0.0.1:{port}/x')
        session = client.session(events.put)
        began = time.monotonic()
        session.start()
        session.feed(bytes(3200))
        session.finish()
        self.assertLess(time.monotonic() - began, 0.1, 'start/feed/finish never wait on the network')
        self.assertIn('无法连接豆包语音', collect(events, timeout=8)[-1]['message'])

    def test_manual_check_entry_point_streams_a_wav(self):
        log = {}
        server = self.serve(volc_script(log, partials=('测试',), final='测试完成。'))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sample.wav'
            with wave.open(str(path), 'wb') as wav:   # 8 kHz stereo: converted to 16 kHz mono
                wav.setnchannels(2)
                wav.setsampwidth(2)
                wav.setframerate(8000)
                wav.writeframes(b'\x10\x00\x20\x00' * 4000)   # 0.5 s
            out = io.StringIO()
            code = stream.run_wav(volc_client(server), path, realtime=False, out=out)
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn('测试完成。', out.getvalue())
        sent = sum(len(f.payload) for f in log['audio'])
        self.assertLessEqual(abs(sent - 16000), 8, sent)   # 0.5 s at 16 kHz mono (ratecv may drop a sample)
        self.assertNotIn('test-api-key-0001', out.getvalue())


# -- DashScope ---------------------------------------------------------------------------
def sentence(text, end=False, sentence_id=1, heartbeat=False):
    return {'begin_time': 100, 'end_time': None if not end else 900, 'text': text, 'words': [],
            'sentence_end': end, 'heartbeat': heartbeat, 'sentence_id': sentence_id}


def dash_script(log, script=(), finals=(), fail=None, started_delay=0.2):
    log.setdefault('audio', bytearray())

    def result(task, value):
        return {'header': {'task_id': task, 'event': 'result-generated', 'attributes': {}},
                'payload': {'output': {'sentence': value}, 'usage': None}}

    def handler(conn):
        log['headers'], log['path'] = dict(conn.headers), conn.path
        _, data = conn.recv()
        run = json.loads(data)
        log['run'] = run
        task = run['header']['task_id']
        try:   # audio must wait for task-started
            conn.recv(timeout=started_delay)
            log['early'] = True
        except (socket.timeout, TimeoutError):
            log['early'] = False
        conn.send_json({'header': {'task_id': task, 'event': 'task-started', 'attributes': {}}, 'payload': {}})
        pending = list(script)
        if fail:
            conn.send_json({'header': {'task_id': task, 'event': 'task-failed', 'error_code': fail[0],
                                       'error_message': fail[1], 'attributes': {}}, 'payload': {}})
            conn.close()
            return
        while True:
            opcode, data = conn.recv()
            if opcode == 0x2:
                log['audio'] += data
                if pending:
                    conn.send_json(result(task, pending.pop(0)))
                continue
            if opcode == 0x1:
                log['finish'] = json.loads(data)
                break
            return
        for value in pending + list(finals):
            conn.send_json(result(task, value))
        conn.send_json({'header': {'task_id': task, 'event': 'task-finished', 'attributes': {}},
                        'payload': {'output': {}, 'usage': None}})
        conn.wait_gone(1)
    return handler


def dash_client(server, **options):
    options.setdefault('api_key', 'sk-dash-test-0001')
    options.setdefault('ready_timeout', 2)
    options.setdefault('final_timeout', 2)
    return dashscope.DashScopeASR(url=server.url('/api-ws/v1/inference'), **options)


class DashScopeTests(unittest.TestCase):
    def serve(self, handler, reject=None):
        server = FakeServer(handler, reject)
        self.addCleanup(server.close)
        return server

    def test_task_flow_sentence_assembly_and_heartbeats(self):
        log = {}
        script = [sentence('', sentence_id=1), sentence('今天'), sentence('今天天气'), sentence('今天天气不错。', True),
                  sentence('', heartbeat=True, sentence_id=0), sentence('我们', sentence_id=2)]
        server = self.serve(dash_script(log, script, finals=[sentence('我们去打球吧。', True, 2)]))
        events = queue.Queue()
        session = dash_client(server).session(events.put)
        session.start()
        fed = b''.join(bytes([i]) * 3200 for i in range(6))
        for i in range(6):
            session.feed(fed[i * 3200:(i + 1) * 3200])
        session.finish()
        seen = collect(events)
        self.assertEqual([e['text'] for e in seen if e['type'] == 'partial'],
                         ['今天', '今天天气', '今天天气不错。', '今天天气不错。我们', '今天天气不错。我们去打球吧。'])
        self.assertEqual(seen[-1], {'type': 'final', 'text': '今天天气不错。我们去打球吧。', 'source': 'dashscope'})
        self.assertFalse(log['early'], 'no audio before task-started')
        self.assertEqual(bytes(log['audio']), fed)
        run = log['run']
        self.assertEqual(run['header']['action'], 'run-task')
        self.assertEqual(run['header']['streaming'], 'duplex')
        self.assertEqual(run['payload']['task_group'], 'audio')
        self.assertEqual(run['payload']['task'], 'asr')
        self.assertEqual(run['payload']['function'], 'recognition')
        self.assertEqual(run['payload']['model'], 'fun-asr-realtime')
        self.assertEqual(run['payload']['input'], {})
        self.assertEqual(run['payload']['parameters']['format'], 'pcm')
        self.assertEqual(run['payload']['parameters']['sample_rate'], 16000)
        self.assertNotIn('disfluency_removal_enabled', run['payload']['parameters'])
        self.assertEqual(log['finish'], {'header': {'action': 'finish-task', 'task_id': run['header']['task_id'],
                                                    'streaming': 'duplex'}, 'payload': {'input': {}}})
        self.assertEqual(log['headers']['authorization'], 'Bearer sk-dash-test-0001')

    def test_failures_and_empty_audio(self):
        server = self.serve(dash_script({}, fail=('Arrearage', 'Access denied, please make sure your account is in good standing.')))
        events = queue.Queue()
        session = dash_client(server).session(events.put)
        session.start()
        session.feed(bytes(3200))
        error = collect(events)[-1]
        self.assertEqual(error['kind'], 'error')
        self.assertIn('欠费', error['message'])
        server = self.serve(dash_script({}, fail=('NO_VALID_AUDIO_ERROR', 'no valid audio')))
        events = queue.Queue()
        session = dash_client(server).session(events.put)
        session.start()
        session.feed(bytes(3200))
        self.assertEqual(collect(events)[-1], {'type': 'final', 'text': '', 'source': 'dashscope'})
        server = self.serve(lambda conn: None,
                            reject=(401, '{"code":"InvalidApiKey","message":"Invalid API-key provided."}'))
        events = queue.Queue()
        dash_client(server).session(events.put).start()
        message = collect(events)[-1]['message']
        self.assertIn('API Key 无效', message)
        self.assertNotIn('sk-dash-test-0001', message)

    def test_sentences_join_latin_with_spaces_and_survive_a_missing_end(self):
        parts = dashscope.Sentences()
        parts.update(sentence('Hello world.', True, 1))
        parts.update(sentence('How are', False, 2))
        self.assertEqual(parts.text(), 'Hello world. How are')
        parts.update(sentence('新的一句', False, 3))   # sentence 2 never ended: keep what we had
        self.assertEqual(parts.text(), 'Hello world. How are新的一句')
        self.assertFalse(parts.update({'heartbeat': True, 'text': ''}))
        self.assertEqual(stream.join_text('版本 2.0', '发布'), '版本 2.0发布')
        self.assertEqual(stream.join_text('abc', 'def'), 'abc def')

    def test_configuration(self):
        client = dashscope.DashScopeASR(api_key='sk-1')
        client.check()
        self.assertEqual(client.endpoint(), 'wss://dashscope.aliyuncs.com/api-ws/v1/inference')
        self.assertEqual(dashscope.DashScopeASR(api_key='k', workspace='llm-abc123').endpoint(),
                         'wss://llm-abc123.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference')
        self.assertEqual(dashscope.DashScopeASR(api_key='k', region='intl').endpoint(),
                         'wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference')
        with self.assertRaisesRegex(ValueError, 'API Key'):
            dashscope.DashScopeASR().check()
        for model in ('qwen3-asr-flash-realtime', 'fun-asr', 'fun-asr-flash-8k-realtime'):
            with self.assertRaisesRegex(ValueError, '不支持模型'):
                dashscope.DashScopeASR(api_key='k', model=model).check()
        with self.assertRaisesRegex(ValueError, 'Workspace'):
            dashscope.DashScopeASR(api_key='k', workspace='a/b').check()
        paraformer = dashscope.DashScopeASR(api_key='k', model='paraformer-realtime-v2', disfluency_removal=True,
                                            language_hints=['zh', 'en'], max_sentence_silence=50)
        paraformer.check()
        self.assertEqual(paraformer.parameters(), {'format': 'pcm', 'sample_rate': 16000, 'heartbeat': True,
                                                   'language_hints': ['zh', 'en'], 'max_sentence_silence': 200,
                                                   'disfluency_removal_enabled': True})
        self.assertNotIn('sk-1', repr(client))


# -- LLM polish ----------------------------------------------------------------------------
class LLMServer:
    def __init__(self, replies=(), delay=0.0):
        owner = self
        self.requests, self.replies, self.delay = [], list(replies), delay

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                owner.requests.append({'path': self.path, 'headers': {k.lower(): v for k, v in self.headers.items()},
                                       'body': json.loads(body)})
                if owner.delay:
                    time.sleep(owner.delay)
                status, reply = owner.replies.pop(0) if owner.replies else (200, chat_reply('好的。'))
                data = reply if isinstance(reply, bytes) else json.dumps(reply, ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    def base(self, path=''):
        return f'http://127.0.0.1:{self.httpd.server_port}{path}'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def chat_reply(text):
    return {'id': 'x', 'object': 'chat.completion',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}


class LLMTests(unittest.TestCase):
    def server(self, replies=(), delay=0.0):
        server = LLMServer(replies, delay)
        self.addCleanup(server.close)
        return server

    def test_request_shape_light_style(self):
        server = self.server([(200, chat_reply('今天天气不错。'))])
        polisher = Polisher(server.base('/v1'), 'sk-llm-test-key', 'my-model', style='light')
        self.assertEqual(polisher.polish('嗯，今天那个天气不错'), '今天天气不错。')
        request = server.requests[0]
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['headers']['authorization'], 'Bearer sk-llm-test-key')
        self.assertEqual(request['headers']['content-type'], 'application/json')
        body = request['body']
        self.assertEqual(body['model'], 'my-model')
        self.assertFalse(body['stream'])
        self.assertEqual([m['role'] for m in body['messages']], ['system', 'user'])
        self.assertEqual(body['messages'][0]['content'], llm.LIGHT_PROMPT)
        self.assertIn('轻度润色', body['messages'][0]['content'])
        self.assertEqual(body['messages'][1]['content'], '嗯，今天那个天气不错')

    def test_raw_style_never_sends_anything(self):
        server = self.server()
        polisher = Polisher(server.base('/v1'), 'sk-x', 'm', style='raw')
        polisher.check()
        Polisher(style='raw').check()   # needs no URL or key at all
        self.assertEqual(polisher.polish('嗯今天天气'), '嗯今天天气')
        self.assertEqual(polisher.probe(), (True, '原文模式不调用大模型'))
        backend = CloudBackend(FakeASR(final='嗯今天天气'), polisher, inserter=lambda text: None)
        backend.connect()
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        events = drain(backend)
        self.assertNotIn('polishing', [e.get('kind') for e in events])
        self.assertEqual(events[-1]['text'], '嗯今天天气')
        self.assertEqual(server.requests, [])

    def test_custom_prompt_and_output_cleanup(self):
        server = self.server([(200, chat_reply('<think>想一想</think>\n“- 买牛奶\n- 取快递”'))])
        polisher = Polisher(server.base(), 'k', 'm', style='custom', prompt='整理成待办清单')
        self.assertEqual(polisher.polish('我要买牛奶还要取快递'), '- 买牛奶\n- 取快递')
        self.assertEqual(server.requests[0]['path'], '/v1/chat/completions')   # bare host gets /v1
        system = server.requests[0]['body']['messages'][0]['content']
        self.assertTrue(system.startswith('整理成待办清单'))
        self.assertIn(llm.GUARD, system)
        with self.assertRaisesRegex(ValueError, '提示词'):
            Polisher(server.base(), 'k', 'm', style='custom').check()

    def test_chat_url_normalisation(self):
        cases = {
            'https://api.example.com': 'https://api.example.com/v1/chat/completions',
            'https://api.example.com/': 'https://api.example.com/v1/chat/completions',
            'https://api.example.com/v1': 'https://api.example.com/v1/chat/completions',
            'https://api.example.com/v1/': 'https://api.example.com/v1/chat/completions',
            'https://ark.cn-beijing.volces.com/api/v3': 'https://ark.cn-beijing.volces.com/api/v3/chat/completions',
            'https://dashscope.aliyuncs.com/compatible-mode/v1':
                'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
            'https://x.example.com/v1beta/openai/': 'https://x.example.com/v1beta/openai/chat/completions',
            'https://relay.example.com/v1/chat/completions': 'https://relay.example.com/v1/chat/completions',
            'http://192.168.1.10:3000/v1': 'http://192.168.1.10:3000/v1/chat/completions',
        }
        for base, expected in cases.items():
            self.assertEqual(llm.chat_url(base), expected, base)
        for bad in ('', 'api.example.com', 'ftp://x.com', 'http://api.example.com/v1'):
            with self.assertRaises(ValueError):
                llm.chat_url(bad)

    def test_errors_are_chinese_and_never_contain_the_key(self):
        key = 'sk-very-secret-key-123456'
        server = self.server([
            (401, {'error': {'message': f'Incorrect API key provided: {key}', 'code': 'invalid_api_key'}}),
            (404, {'error': {'message': 'The model `nope` does not exist', 'code': 'model_not_found'}}),
            (429, {'error': {'message': 'You exceeded your current quota', 'code': 'insufficient_quota'}}),
            (400, {'error': {'message': f'bad request from {key}'}}),
            (500, b'<html>oops</html>'),
            (200, b'not json'),
            (200, {'error': {'message': '余额不足'}}),
        ])
        polisher = Polisher(server.base('/v1'), key, 'm', style='light')
        expected = ['API Key 无效', '模型不存在', '额度不足', '参数错误', '暂时不可用', '不是 JSON', '额度不足']
        for fragment in expected:
            with self.assertRaises(PolishError) as caught:
                polisher.polish('测试一下')
            self.assertIn(fragment, str(caught.exception))
            self.assertNotIn(key, str(caught.exception))

    def test_timeout_and_unreachable(self):
        server = self.server([(200, chat_reply('迟到'))], delay=1.0)
        with self.assertRaisesRegex(PolishError, '超时'):
            Polisher(server.base('/v1'), 'k', 'm', style='light', timeout=0.3).polish('测试')
        listener = socket.create_server(('127.0.0.1', 0))
        port = listener.getsockname()[1]
        listener.close()
        with self.assertRaisesRegex(PolishError, '润色服务'):
            Polisher(f'http://127.0.0.1:{port}/v1', 'k', 'm', style='light', timeout=2).polish('测试')

    def test_temperature_is_dropped_when_the_model_refuses_it(self):
        server = self.server([
            (400, {'error': {'message': "Unsupported value: 'temperature' does not support 0.3 with this model.",
                             'code': 'unsupported_value'}}),
            (200, chat_reply('好的。')),
        ])
        self.assertEqual(Polisher(server.base('/v1'), 'k', 'm', style='light').polish('好的'), '好的。')
        self.assertIn('temperature', server.requests[0]['body'])
        self.assertNotIn('temperature', server.requests[1]['body'])

    def test_answers_instead_of_polish_are_rejected(self):
        server = self.server([(200, chat_reply('好的！下面是一首关于春天的诗：春风拂面百花开，' * 3))])
        with self.assertRaisesRegex(PolishError, '异常'):
            Polisher(server.base('/v1'), 'k', 'm', style='light').polish('帮我写一首诗')

    def test_configuration_checks(self):
        with self.assertRaisesRegex(ValueError, 'API Key'):
            Polisher('https://api.example.com/v1', '', 'm', style='light').check()
        with self.assertRaisesRegex(ValueError, '模型'):
            Polisher('https://api.example.com/v1', 'k', '', style='light').check()
        with self.assertRaises(ValueError):
            Polisher(style='fancy')
        self.assertNotIn('sk-secret', repr(Polisher('https://a.example.com', 'sk-secret', 'm', style='light')))


# -- CloudBackend ------------------------------------------------------------------------------
class FakeASR:
    name = '测试识别'
    source = 'fake'

    def __init__(self, partials=('今天',), final='嗯今天天气不错', error=None, hold=False, check_error=None,
                 finalize=True):
        self.partials, self.final_text, self.error = partials, final, error
        self.check_error, self.finalize = check_error, finalize
        self.hold = threading.Event() if hold else None
        self.sessions = []

    def check(self):
        if self.check_error:
            raise ValueError(self.check_error)

    def session(self, emit):
        session = FakeASRSession(self, emit)
        self.sessions.append(session)
        return session


class FakeASRSession:
    def __init__(self, asr, emit):
        self.asr, self.emit = asr, emit
        self.audio, self.started, self.finished, self.cancelled = [], False, False, False

    def start(self):
        self.started = True

    def feed(self, pcm):
        self.audio.append(pcm)
        if len(self.audio) == 1:
            for text in self.asr.partials:
                self.emit({'type': 'partial', 'text': text, 'offset': 0, 'final': False, 'source': 'fake'})

    def finish(self):
        self.finished = True

        def later():
            if self.asr.hold is not None:
                self.asr.hold.wait(3)
            if self.cancelled or not self.asr.finalize:
                return
            if self.asr.error:
                self.emit({'kind': 'error', 'message': self.asr.error})
            else:
                self.emit({'type': 'final', 'text': self.asr.final_text})
        threading.Thread(target=later, daemon=True).start()

    def cancel(self):
        self.cancelled = True


class FakePolisher:
    enabled = True

    def __init__(self, log, result='今天天气不错。', error=None, gate=None):
        self.log, self.result, self.error, self.gate = log, result, error, gate

    def check(self):
        pass

    def polish(self, text):
        self.log.append(('polish', text))
        if self.gate is not None:
            self.gate.wait(3)
        if self.error:
            raise PolishError(self.error)
        return self.result


def drain(backend, timeout=3):
    events = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        event = backend.poll()
        if event is None:
            time.sleep(0.01)
            continue
        events.append(event)
        if event.get('type') == 'result' or event.get('kind') == 'error':
            return events
    raise AssertionError(f'no result/error: {events}')


class CloudBackendTests(unittest.TestCase):
    def make(self, asr=None, polish=None, log=None):
        log = [] if log is None else log
        backend = CloudBackend(asr or FakeASR(), polish, inserter=lambda text: log.append(('insert', text)))
        push = backend._push

        def logged_push(job, event, terminal=False):
            log.append(('push', event.get('type') or event.get('kind')))
            push(job, event, terminal)
        backend._push = logged_push
        backend.connect()
        return backend, log

    def test_inserts_once_before_the_result_with_polishing_in_order(self):
        log = []
        backend, log = self.make(FakeASR(partials=('今天', '今天天气')), FakePolisher(log), log)
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        events = drain(backend)
        self.assertEqual([e.get('type') or e.get('kind') for e in events], ['partial', 'partial', 'polishing', 'result'])
        tag = {'source': 'cloud', 'engine': 'fake'}
        self.assertEqual(events[0], {'type': 'partial', 'text': '今天天气', 'offset': 0, 'final': False, **tag},
                         'queued live texts collapse to the newest')
        self.assertEqual(events[1], {'type': 'partial', 'text': '嗯今天天气不错', 'offset': 0, 'final': True, **tag})
        self.assertEqual(events[-1], {'type': 'result', 'text': '今天天气不错。', 'raw': '嗯今天天气不错', **tag})
        self.assertTrue(all(e['source'] == 'cloud' for e in events))
        self.assertEqual(CloudBackend.partial_source, 'cloud')
        inserts = [entry for entry in log if entry[0] == 'insert']
        self.assertEqual(inserts, [('insert', '今天天气不错。')])
        self.assertLess(log.index(('polish', '嗯今天天气不错')), log.index(('insert', '今天天气不错。')))
        self.assertLess(log.index(('insert', '今天天气不错。')), log.index(('push', 'result')))
        self.assertIsNone(backend.poll())
        with self.assertRaises(RuntimeError):
            backend.feed(bytes(320))   # the session is over

    def test_cancel_before_the_transcript_inserts_nothing(self):
        asr = FakeASR(hold=True)
        backend, log = self.make(asr)
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        backend.cancel()
        asr.hold.set()
        time.sleep(0.2)
        self.assertIsNone(backend.poll())
        self.assertNotIn('insert', [entry[0] for entry in log])

    def test_cancel_while_polishing_inserts_nothing(self):
        log, gate = [], threading.Event()
        backend, log = self.make(FakeASR(), FakePolisher(log, gate=gate), log)
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        wait_for(lambda: any(entry[0] == 'polish' for entry in log))
        backend.cancel()
        gate.set()
        time.sleep(0.2)
        self.assertNotIn('insert', [entry[0] for entry in log])
        self.assertIsNone(backend.poll())
        backend.start()   # the next recording works normally
        backend.feed(bytes(3200))
        backend.stop()
        self.assertEqual(drain(backend)[-1]['type'], 'result')
        self.assertEqual([e for e in log if e[0] == 'insert'], [('insert', '今天天气不错。')])

    def test_notes_are_not_typed(self):
        backend, log = self.make(FakeASR(final='记一下'))
        backend.insert_text = False
        backend.start()
        backend.insert_text = True   # captured when the recording started
        backend.feed(bytes(3200))
        backend.stop()
        self.assertEqual(drain(backend)[-1], {'type': 'result', 'text': '记一下', 'source': 'cloud', 'engine': 'fake'})
        self.assertNotIn('insert', [entry[0] for entry in log])

    def test_polish_failure_types_the_transcript_and_says_so(self):
        log = []
        backend, log = self.make(FakeASR(final='嗯测试'), FakePolisher(log, error='润色服务响应超时（超过 20 秒）'), log)
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        events = drain(backend)
        notices = [e for e in events if e.get('kind') == 'notice']
        self.assertEqual(len(notices), 1)
        self.assertIn('已改用识别原文', notices[0]['message'])
        self.assertEqual(events[-1]['text'], '嗯测试')
        self.assertEqual([e for e in log if e[0] == 'insert'], [('insert', '嗯测试')])

    def test_typing_refused_still_reports_the_text(self):
        def refuse(text):
            raise OSError('SendInput 被系统拒绝')
        backend = CloudBackend(FakeASR(final='你好'), inserter=refuse)
        backend.connect()
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        events = drain(backend)
        self.assertIn('管理员权限窗口', [e for e in events if e.get('kind') == 'notice'][0]['message'])
        self.assertEqual(events[-1]['text'], '你好')

    def test_empty_transcript_is_neither_polished_nor_typed(self):
        log = []
        backend, log = self.make(FakeASR(partials=(), final=''), FakePolisher(log), log)
        backend.start()
        backend.stop()
        events = drain(backend)
        self.assertEqual(events[-1], {'type': 'result', 'text': '', 'source': 'cloud', 'engine': 'fake'})
        self.assertNotIn('polish', [entry[0] for entry in log])
        self.assertNotIn('insert', [entry[0] for entry in log])

    def test_recogniser_errors_and_configuration(self):
        backend, _ = self.make(FakeASR(error='豆包语音鉴权失败：API Key 不正确'))
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        self.assertEqual(drain(backend)[-1], {'kind': 'error', 'message': '豆包语音鉴权失败：API Key 不正确',
                                              'source': 'cloud', 'engine': 'fake'})
        with self.assertRaisesRegex(RuntimeError, '请填写'):
            CloudBackend(FakeASR(check_error='豆包语音：请填写 API Key')).connect()
        # 轻度润色 picked before its relay is filled in: the voice still works, typed as recognised,
        # and each recording says why once (review 2026-10-14: it used to refuse to start).
        typed = []
        half = CloudBackend(FakeASR(final='你好'), Polisher('https://api.example.com/v1', '', 'm', style='light'),
                            inserter=typed.append)
        half.connect()
        for _ in range(2):
            half.start()
            half.feed(bytes(3200))
            half.stop()
            events = drain(half)
            notices = [e['message'] for e in events if e.get('kind') == 'notice']
            self.assertEqual(len(notices), 1)
            self.assertTrue(notices[0].startswith(cloud.POLISH_INCOMPLETE))
            self.assertIn('请填写 API Key', notices[0])
            self.assertNotIn('polishing', [e.get('kind') for e in events], 'nothing sent anywhere')
            self.assertEqual(events[-1]['text'], '你好')
        self.assertEqual(typed, ['你好', '你好'])
        with self.assertRaisesRegex(RuntimeError, '没有已确认的录音会话'):
            CloudBackend(FakeASR()).feed(b'\x00\x00')

    def test_watchdog_ends_a_session_that_never_returns(self):
        backend = CloudBackend(FakeASR(finalize=False), inserter=lambda text: None, asr_watchdog=0.2)
        backend.connect()
        backend.start()
        backend.stop()
        time.sleep(0.3)
        event = drain(backend)[-1]
        self.assertEqual(event['kind'], 'error')
        self.assertIn('没有返回结果', event['message'])

    def test_plain_callable_polish_and_corrector(self):
        typed = []
        backend = CloudBackend(FakeASR(final='abc'), lambda text: text.upper(), inserter=typed.append,
                               corrector=lambda text: text + '!')
        backend.connect()
        backend.start()
        backend.stop()
        self.assertEqual(drain(backend)[-1]['text'], 'ABC!')
        self.assertEqual(typed, ['ABC!'])

    def test_end_to_end_with_volcengine_protocol(self):
        log, typed = {}, []
        server = FakeServer(volc_script(log, partials=('今天天气',), final='今天天气不错。'))
        self.addCleanup(server.close)
        backend = CloudBackend(volc_client(server), inserter=typed.append)
        backend.connect()
        backend.start()
        for _ in range(3):
            backend.feed(bytes(1600))
        backend.stop()
        events = drain(backend)
        self.assertEqual(events[-1], {'type': 'result', 'text': '今天天气不错。', 'source': 'cloud', 'engine': 'volc'})
        self.assertEqual(typed, ['今天天气不错。'])
        self.assertTrue(log['audio'][-1].last)

    def test_controller_drives_the_cloud_backend_unchanged(self):
        typed, notes = [], []
        backend = CloudBackend(FakeASR(partials=('今天',), final='今天天气不错'), inserter=typed.append)
        control = Controller(lambda *value: notes.append(value), backend, FakeCapture)
        self.addCleanup(control.close)
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for _ in range(3):
            control._audio(b'\x10\x00' * 1600)   # 0.3 s
        control.command('stop')
        wait_for(lambda: control.phase == 'idle' and any(kind == 'result' for kind, _ in notes), timeout=5)
        result = [value for kind, value in notes if kind == 'result'][-1]
        self.assertEqual(result['text'], '今天天气不错')
        self.assertEqual(typed, ['今天天气不错'])
        partials = [value for kind, value in notes if kind == 'partial']
        self.assertTrue(partials[-1]['final'])
        self.assertEqual(partials[-1]['text'], '今天天气不错')
        self.assertEqual({p['source'] for p in partials}, {'cloud'}, 'never mistaken for OpenLess live text')

    def test_create_backend_reads_keys_lazily_from_secrets(self):
        stored = {}
        settings = {'engine': 'volc', 'volc': {'resource_id': 'volc.bigasr.sauc.duration'},
                    'polish': {'style': 'light', 'base_url': 'https://api.example.com/v1', 'model': 'm'}}
        backend = cloud.create_backend(settings, secret=stored.get, inserter=lambda text: None)
        with self.assertRaisesRegex(RuntimeError, '豆包语音'):
            backend.connect()
        stored[cloud.SECRET_VOLC_API_KEY] = 'key-now-set'
        backend.connect()   # the polish key missing never stops the voice (typed as recognised)
        stored[cloud.SECRET_LLM_API_KEY] = 'llm-key'
        backend.polish.check()
        self.assertEqual(backend.asr.headers('r')['X-Api-Key'], 'key-now-set')
        self.assertEqual(backend.label, '豆包语音')
        dash = cloud.create_backend({'engine': 'dashscope'}, secret={cloud.SECRET_DASHSCOPE_API_KEY: 'sk-d'}.get)
        dash.connect()
        self.assertIsNone(dash.polish)
        self.assertEqual(dash.asr.model_name(), 'fun-asr-realtime')
        with self.assertRaises(ValueError):
            cloud.create_backend({'engine': 'openless'}, secret=stored.get)


# -- secrets (real DPAPI) ------------------------------------------------------------------
class SecretTests(unittest.TestCase):
    def test_round_trip_is_encrypted_at_rest_and_masked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'runtime' / 'secrets.json'
            store = secrets.SecretStore(path)
            value = 'sk-abcdefgh12345678wxyz'
            store.save('volc.api_key', '  ' + value + '\n')
            self.assertEqual(store.load('volc.api_key'), value)
            self.assertEqual(secrets.SecretStore(path).load('volc.api_key'), value, 'persists across instances')
            raw = path.read_text(encoding='utf-8')
            self.assertNotIn(value, raw)
            self.assertNotIn(base64.b64encode(value.encode()).decode(), raw)
            self.assertEqual(store.masked('volc.api_key'), '••••wxyz')
            self.assertEqual(store.names(), ['volc.api_key'])
            # A blob only decrypts under its own name.
            data = json.loads(raw)
            data['items']['other.key'] = data['items']['volc.api_key']
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertIsNone(store.load('other.key'))
            data['items']['volc.api_key'] = base64.b64encode(b'garbage').decode()
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertIsNone(store.load('volc.api_key'))
            store.save('volc.api_key', value)
            store.delete('volc.api_key')
            self.assertIsNone(store.load('volc.api_key'))
            self.assertEqual(store.masked('volc.api_key'), '')
            store.save('llm.api_key', 'x' * 20)
            store.save('llm.api_key', '')   # empty deletes
            self.assertFalse(store.has('llm.api_key'))
            for bad in ('', '../x', 'a b', None, 'x' * 80):
                with self.assertRaises(ValueError):
                    store.save(bad, 'v')
            with self.assertRaises(ValueError):
                store.save('ok.name', 'line\nbreak')
            self.assertNotIn(value, repr(store))

    def test_mask_rules_and_module_api(self):
        self.assertEqual(secrets.mask(''), '')
        self.assertEqual(secrets.mask('abcd'), '••••')
        self.assertEqual(secrets.mask('abcdefgh'), '••••gh')
        self.assertEqual(secrets.mask('0123456789abcdef'), '••••cdef')
        self.assertEqual(secrets.unprotect(secrets.protect(b'\x00\x01binary', b'e'), b'e'), b'\x00\x01binary')
        with self.assertRaises(OSError):
            secrets.unprotect(secrets.protect(b'data', b'one'), b'two')
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)
        with tempfile.TemporaryDirectory() as folder:
            secrets.configure(folder)
            secrets.save('dashscope.api_key', 'sk-module-api-123456')
            self.assertEqual(secrets.load('dashscope.api_key'), 'sk-module-api-123456')
            self.assertEqual(secrets.masked('dashscope.api_key'), '••••3456')
            self.assertTrue((Path(folder) / 'secrets.json').exists())
            secrets.delete('dashscope.api_key')
            self.assertIsNone(secrets.load('dashscope.api_key'))


if __name__ == '__main__':
    unittest.main()
