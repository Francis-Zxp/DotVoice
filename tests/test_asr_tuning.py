"""Recogniser-side tuning (offline): Doubao's 200 ms packets and hotwords (with the retry
without them when the service refuses the request), Bailian's language hint (with the same
fallback), the hotword source (the OpenLess dictionary, read-only) and the core's
"use Doubao" hint flag.

Network tests talk to the minimal WebSocket server in test_engines (127.0.0.1, stdlib
only); no real key, service, microphone or audio upload is involved. Keys in the core tests
go to real DPAPI in a temporary folder, never to DotVoice's own runtime/."""
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
import uuid

from bridge import secrets
from bridge.core import cloud_settings
from bridge.engines import cloud, dashscope, stream, volc
from bridge.fast_insert import OpenLessSettings
from bridge.prefs import DEFAULTS

try:
    from .test_bridge import FakeBackend, wait_for
    from .test_engines import FakeServer, collect, dash_client, sentence, server_error, server_response, volc_client
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, wait_for
    from test_engines import FakeServer, collect, dash_client, sentence, server_error, server_response, volc_client
    from core_fakes import make_core

MS = stream.BYTES_PER_SECOND // 1000          # bytes per millisecond of 16 kHz S16LE mono
CHUNK = 100 * MS                              # the controller hands over ~100 ms at a time


def chunks(count, first=1):
    """`count` distinct 100 ms chunks (each one byte value), so order is checkable."""
    return [bytes([first + i]) * CHUNK for i in range(count)]


def doubao(server, **options):
    options.setdefault('packet_ms', volc.PACKET_MS)
    return volc_client(server, **options)


def corpus_words(request):
    corpus = request.payload['request'].get('corpus')
    return [w['word'] for w in json.loads(corpus['context'])['hotwords']] if corpus else None


def doubao_service(log, refuse=None, refuse_after=0, refuse_without_corpus=False, close_code=None, ack_first=False,
                   final='你好世界。'):
    """A Volcengine stand-in that may refuse a request carrying hotwords: `refuse` = (code,
    message) error frame, or `close_code` closes instead; `refuse_after` audio frames are read
    first (None: until the last one); ack_first answers the request before refusing."""
    log.setdefault('connections', [])
    lock = threading.Lock()

    def handler(conn):
        entry = {'audio': [], 'arrived': []}
        with lock:
            log['connections'].append(entry)
        _, data = conn.recv()
        entry['request'] = volc.parse_frame(data)
        has_corpus = 'corpus' in entry['request'].payload['request']
        if (refuse or close_code) and (has_corpus or refuse_without_corpus):
            if ack_first:
                conn.send_binary(server_response({'result': {'text': ''}}, 1))
            while refuse_after is None or len(entry['audio']) < refuse_after:
                _, data = conn.recv()
                frame = volc.parse_frame(data)
                entry['audio'].append(frame)
                if frame.last:
                    break
            if close_code:
                conn.close(close_code)
            else:
                conn.send_binary(server_error(*refuse))
                conn.close()
            conn.wait_gone(1)
            return
        conn.send_binary(server_response({'audio_info': {'duration': 0}, 'result': {'text': ''}}, 1))
        while True:
            try:
                opcode, data = conn.recv()
            except (OSError, ConnectionError):
                return
            if opcode != 0x2:
                return
            frame = volc.parse_frame(data)
            entry['audio'].append(frame)
            entry['arrived'].append(time.monotonic())
            if frame.last:
                conn.send_binary(server_response({'result': {'text': final}}, frame.sequence or -1, last=True))
                conn.close()
                return
            conn.send_binary(server_response({'result': {'text': '你好'}}, frame.sequence or 2))
    return handler


class Case(unittest.TestCase):
    def setUp(self):
        # The "service refused it" memory lives for the process: each test starts clean.
        for memory in (volc._corpus_refused, volc._corpus_doubts, dashscope._hints_refused):
            saved = memory.copy()
            memory.clear()
            self.addCleanup(lambda m=memory, s=saved: (m.clear(), m.update(s)))

    def serve(self, handler):
        server = FakeServer(handler)
        self.addCleanup(server.close)
        return server


# -- Doubao packets ------------------------------------------------------------------------
class DoubaoPacketTests(Case):
    def test_streams_200_ms_packets_and_sends_the_rest_at_once_on_release(self):
        log = {}
        server = self.serve(doubao_service(log))
        events = queue.Queue()
        session = doubao(server).session(events.put)
        self.assertEqual(session.packet, 200 * MS)
        session.start()
        fed = chunks(5)
        for chunk in fed:   # in real time, as the pen delivers it
            session.feed(chunk)
            time.sleep(0.1)
        released = time.monotonic()
        session.finish()
        self.assertEqual(collect(events)[-1], {'type': 'final', 'text': '你好世界。', 'source': 'volc'})
        frames = log['connections'][0]['audio']
        self.assertEqual([len(f.payload) for f in frames], [200 * MS, 200 * MS, 100 * MS])
        self.assertEqual([f.sequence for f in frames], [2, 3, -4])
        self.assertEqual(b''.join(f.payload for f in frames), b''.join(fed))
        # Release-to-last-packet: the 100 ms remainder never waits for a 200 ms packet to fill.
        self.assertLess(log['connections'][0]['arrived'][-1] - released, 0.25)

    def test_a_backlog_goes_out_as_consecutive_200_ms_packets(self):
        log = {}
        server = self.serve(doubao_service(log, final='好'))
        events = queue.Queue()
        session = doubao(server).session(events.put)
        session.start()
        fed = chunks(9)   # all of it queued before the connection is up
        for chunk in fed:
            session.feed(chunk)
        session.finish()
        self.assertEqual(collect(events)[-1]['text'], '好')
        frames = log['connections'][0]['audio']
        self.assertEqual([len(f.payload) for f in frames], [200 * MS] * 4 + [100 * MS])
        self.assertEqual(b''.join(f.payload for f in frames), b''.join(fed))

    def test_the_engine_builder_picks_the_packet_size_per_engine(self):
        backend = cloud.create_backend({'engine': 'volc'}, secret={cloud.SECRET_VOLC_API_KEY: 'k-1'}.get)
        session = backend.asr.session(lambda event: None)
        self.assertEqual((backend.asr.packet_ms, session.packet), (200, 200 * MS))
        dash = cloud.create_backend({'engine': 'dashscope'}, secret={cloud.SECRET_DASHSCOPE_API_KEY: 'sk'}.get)
        self.assertEqual(dash.asr.session(lambda event: None).packet, 100 * MS, 'Bailian keeps 100 ms')
        self.assertEqual(volc.VolcASR(api_key='k').session(lambda event: None).packet, stream.PACKET)
        self.assertEqual(volc._from_env().packet_ms, volc.PACKET_MS, 'the manual check streams like the app')


# -- Doubao hotwords -----------------------------------------------------------------------
class DoubaoHotwordTests(Case):
    def test_hotwords_are_read_when_a_recording_starts_most_useful_first(self):
        words = ['点声', '气声']
        client = volc.VolcASR(api_key='k', hotwords=lambda: list(words))
        self.assertEqual(json.loads(client.request_payload()['request']['corpus']['context']),
                         {'hotwords': [{'word': '点声'}, {'word': '气声'}]})
        words.insert(0, 'Enscape')
        self.assertEqual(json.loads(client.request_payload()['request']['corpus']['context'])['hotwords'][0],
                         {'word': 'Enscape'}, 'a phrase added since counts from the next recording')
        self.assertNotIn('corpus', client.request_payload(corpus=False)['request'])
        # Within about 100 tokens (counted as characters), in the given order.
        long = volc.VolcASR(api_key='k', hotwords=['甲' * 60, '乙' * 30, '丙' * 20, '丁' * 10])
        self.assertEqual(corpus_words(volc.parse_frame(volc.full_client_request(long.request_payload()))),
                         ['甲' * 60, '乙' * 30, '丁' * 10])

    def test_a_hotword_source_that_fails_never_stops_a_recording(self):
        def broken():
            raise OSError('dictionary locked')
        log = {}
        server = self.serve(doubao_service(log, final='照常。'))
        events = queue.Queue()
        session = doubao(server, hotwords=broken).session(events.put)
        session.start()
        session.feed(bytes(CHUNK))
        session.finish()
        self.assertEqual(collect(events)[-1]['text'], '照常。')
        self.assertIsNone(corpus_words(log['connections'][0]['request']))
        self.assertEqual(volc.VolcASR(api_key='k', hotwords=lambda: None).request_payload()['request'].get('corpus'), None)

    def test_a_refused_hotword_list_is_retried_without_it_and_no_audio_is_lost(self):
        log = {}
        server = self.serve(doubao_service(log, refuse=(volc.INVALID_PARAMETER, '{"error":"invalid corpus context"}'),
                                           refuse_after=2))
        events = queue.Queue()
        client = doubao(server, hotwords=['点声', '气声'], ack_grace=0.05)
        session = client.session(events.put)
        session.start()
        fed = chunks(4)
        for chunk in fed:   # 400 ms streamed before any answer (ack_grace), then refused
            session.feed(chunk)
        wait_for(lambda: len(log['connections']) == 2 and log['connections'][1]['audio'], timeout=4)
        rest = chunks(6, first=20) + [bytes([99]) * (50 * MS)]   # the recording goes on: 650 ms more
        for chunk in rest:
            session.feed(chunk)
        session.finish()
        seen = collect(events)
        self.assertEqual(seen[-1], {'type': 'final', 'text': '你好世界。', 'source': 'volc'})
        self.assertFalse([e for e in seen if e.get('kind') == 'error'], 'the refusal never reaches the user')
        first, second = log['connections']
        self.assertEqual(corpus_words(first['request']), ['点声', '气声'])
        self.assertIsNone(corpus_words(second['request']), 'the second request leaves the hotwords out')
        self.assertEqual(len(first['audio']), 2)
        audio = second['audio']
        self.assertEqual(b''.join(f.payload for f in audio), b''.join(fed + rest), 'every packet, in order')
        self.assertEqual([f.sequence for f in audio], [2, 3, 4, 5, 6, -7], 'numbered afresh')
        self.assertEqual([len(f.payload) for f in audio[:-1]], [200 * MS] * 5)
        self.assertEqual(second['request'].sequence, 1)
        # Remembered for this process: neither this client nor a new one sends them again.
        self.assertFalse(client.corpus_allowed())
        self.assertFalse(client.session(events.put).corpus)
        self.assertFalse(doubao(server, hotwords=['点声']).corpus_allowed())
        self.assertTrue(volc.VolcASR(api_key='k', hotwords=['点声']).corpus_allowed(), 'other services are not affected')

    def test_a_short_recording_refused_after_release_is_sent_again_in_full(self):
        log = {}
        server = self.serve(doubao_service(log, refuse=(volc.INVALID_PARAMETER, 'bad request'), refuse_after=None,
                                           final='短句。'))
        events = queue.Queue()
        session = doubao(server, hotwords=['点声'], ack_grace=0.05).session(events.put)
        session.start()
        fed = chunks(3)
        for chunk in fed:
            session.feed(chunk)
        session.finish()   # everything, the last packet included, goes out before the refusal
        self.assertEqual(collect(events, timeout=5)[-1]['text'], '短句。')
        first, second = log['connections']
        self.assertTrue(first['audio'][-1].last)
        self.assertEqual(b''.join(f.payload for f in first['audio']), b''.join(fed))
        self.assertEqual(b''.join(f.payload for f in second['audio']), b''.join(fed))
        self.assertEqual([(f.sequence, f.last) for f in second['audio']], [(2, False), (-3, True)])

    def test_a_close_before_any_answer_also_falls_back(self):
        log = {}
        server = self.serve(doubao_service(log, close_code=1007, final='关闭后重试。'))
        events = queue.Queue()
        client = doubao(server, hotwords=['点声'])
        session = client.session(events.put)
        session.start()
        session.feed(bytes(CHUNK))
        session.finish()
        self.assertEqual(collect(events, timeout=5)[-1]['text'], '关闭后重试。')
        self.assertEqual(len(log['connections']), 2)
        self.assertFalse(client.corpus_allowed(), '1007 (invalid data) faults the request: remembered at once')

    def record(self, client, final):
        events = queue.Queue()
        session = client.session(events.put)
        session.start()
        session.feed(bytes(CHUNK))
        session.finish()
        self.assertEqual(collect(events, timeout=5)[-1], {'type': 'final', 'text': final, 'source': 'volc'})
        return session

    def test_a_close_that_faults_nothing_in_the_request_does_not_switch_hotwords_off(self):
        # Review 2026-10-14: any close before the first answer (1013 try again later, 1012
        # restart, 1011, a gateway) was taken for a refusal of the hotwords; once the retry
        # without them was accepted they were never sent again in this process - the dictionary
        # silently off until DotVoice restarted. Now such a close only leaves them out of that
        # recording; two in a row, with no request taking them in between, stop them (a
        # service that drops what it dislikes without saying so).
        for code in (1013, 1012, 1011, 1001):
            with self.subTest(code=code):
                volc._corpus_doubts.clear()
                volc._corpus_refused.clear()
                log = {}

                def service(conn, closing=doubao_service(log, close_code=code, final='好。'),
                            healthy=doubao_service(log, final='好。')):
                    (healthy if log.get('healthy') else closing)(conn)
                server = self.serve(service)
                client = doubao(server, hotwords=['点声'])
                first = self.record(client, '好。')
                self.assertEqual(len(log['connections']), 2, 'retried without the hotwords')
                self.assertIsNone(corpus_words(log['connections'][1]['request']))
                self.assertTrue(client.corpus_allowed(), 'one close is no refusal')
                self.assertTrue(client.session(lambda event: None).corpus, 'the next recording sends them')
                self.assertFalse(first.corpus)
                log['healthy'] = True   # taken with the hotwords: that close was not about them
                self.record(client, '好。')
                self.assertEqual(corpus_words(log['connections'][2]['request']), ['点声'])
                log['healthy'] = False
                self.record(client, '好。')
                self.assertTrue(client.corpus_allowed(), 'not two in a row')
                self.record(client, '好。')   # closed again, and again taken without them
                self.assertFalse(client.corpus_allowed(), 'two in a row: this service drops them')
                self.assertEqual(len(log['connections']), 7)

    def test_when_it_is_not_the_hotwords_the_error_is_reported_and_nothing_is_remembered(self):
        log = {}
        server = self.serve(doubao_service(log, refuse=(volc.INVALID_PARAMETER, 'invalid resource'),
                                           refuse_without_corpus=True))
        events = queue.Queue()
        client = doubao(server, hotwords=['点声'])
        session = client.session(events.put)
        session.start()
        session.feed(bytes(CHUNK))
        session.finish()
        error = collect(events, timeout=5)[-1]
        self.assertEqual(error['kind'], 'error')
        self.assertIn('请求参数无效', error['message'])
        self.assertEqual(len(log['connections']), 2, 'one retry only')
        self.assertTrue(client.corpus_allowed(), 'the hotwords were not the problem')

    def test_no_retry_for_other_errors_after_an_answer_or_without_hotwords(self):
        cases = [
            ({'refuse': (volc.BAD_AUDIO_FORMAT, '{"error":"bad format"}')}, ['点声'], '音频格式不正确'),
            ({'refuse': (volc.INVALID_PARAMETER, 'x'), 'ack_first': True}, ['点声'], '请求参数无效'),
            ({'refuse': (volc.INVALID_PARAMETER, 'x'), 'refuse_without_corpus': True}, [], '请求参数无效'),
        ]
        for options, words, expected in cases:
            log = {}
            server = self.serve(doubao_service(log, **options))
            events = queue.Queue()
            session = doubao(server, hotwords=words).session(events.put)
            session.start()
            session.feed(bytes(CHUNK))
            session.finish()
            seen = collect(events, timeout=5)
            self.assertIn(expected, seen[-1]['message'], options)
            time.sleep(0.1)
            self.assertEqual(len(log['connections']), 1, options)
        self.assertTrue(volc.corpus_refusal(45000099, {'error': 'hotwords too long'}))
        self.assertFalse(volc.corpus_refusal(volc.AUDIO_TIMEOUT, {'error': 'context deadline exceeded'}))
        self.assertFalse(volc.corpus_refusal(55000031, 'corpus'))

    def test_a_cancelled_session_never_retries(self):
        log = {}
        gate = threading.Event()

        def handler(conn):
            log.setdefault('n', 0)
            log['n'] += 1
            conn.recv()
            gate.wait(2)
            conn.send_binary(server_error(volc.INVALID_PARAMETER, 'corpus'))
            conn.close()
            conn.wait_gone(1)
        server = self.serve(handler)
        events = queue.Queue()
        session = doubao(server, hotwords=['点声']).session(events.put)
        session.start()
        wait_for(lambda: log.get('n') == 1)
        session.cancel()
        gate.set()
        session.thread.join(2)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(log['n'], 1)
        self.assertTrue(events.empty())


# -- Bailian language hint -------------------------------------------------------------------
def bailian_service(log, refuse_hints=None, fail_code='InvalidParameter', fail_message='language_hints is invalid'):
    """refuse_hints: fail a run-task that carries language_hints, before task-started."""
    log.setdefault('runs', [])
    log.setdefault('audio', bytearray())

    def handler(conn):
        _, data = conn.recv()
        run = json.loads(data)
        log['runs'].append(run)
        task = run['header']['task_id']
        if refuse_hints and 'language_hints' in run['payload']['parameters']:
            conn.send_json({'header': {'task_id': task, 'event': 'task-failed', 'error_code': fail_code,
                                       'error_message': fail_message, 'attributes': {}}, 'payload': {}})
            conn.close()
            conn.wait_gone(1)
            return
        conn.send_json({'header': {'task_id': task, 'event': 'task-started', 'attributes': {}}, 'payload': {}})
        while True:
            opcode, data = conn.recv()
            if opcode == 0x2:
                log['audio'] += data
                continue
            break
        conn.send_json({'header': {'task_id': task, 'event': 'result-generated', 'attributes': {}},
                        'payload': {'output': {'sentence': sentence('今天天气不错。', True)}}})
        conn.send_json({'header': {'task_id': task, 'event': 'task-finished', 'attributes': {}}, 'payload': {'output': {}}})
        conn.wait_gone(1)
    return handler


class BailianHintTests(Case):
    def test_fun_asr_realtime_gets_the_mandarin_hint_and_nothing_else_changes(self):
        self.assertEqual(dashscope.DashScopeASR(api_key='k').parameters(),
                         {'format': 'pcm', 'sample_rate': 16000, 'heartbeat': True, 'language_hints': ['zh']})
        self.assertEqual(dashscope.DashScopeASR(api_key='k', model='fun-asr-realtime-2026-02-28').parameters()
                         ['language_hints'], ['zh'])
        self.assertNotIn('language_hints', dashscope.DashScopeASR(api_key='k', model='paraformer-realtime-v2').parameters())
        self.assertEqual(dashscope.DashScopeASR(api_key='k', language_hints=['en']).parameters()['language_hints'], ['en'])
        log = {}
        server = self.serve(bailian_service(log))
        events = queue.Queue()
        session = dash_client(server).session(events.put)
        session.start()
        session.feed(bytes(CHUNK))
        session.finish()
        self.assertEqual(collect(events)[-1]['text'], '今天天气不错。')
        self.assertEqual(log['runs'][0]['payload']['parameters']['language_hints'], ['zh'])
        self.assertEqual(session.packet, 100 * MS)

    def test_a_refused_hint_is_retried_without_it_and_remembered(self):
        log = {}
        server = self.serve(bailian_service(log, refuse_hints=True))
        events = queue.Queue()
        client = dash_client(server)
        session = client.session(events.put)
        session.start()
        fed = chunks(3)
        for chunk in fed:
            session.feed(chunk)
        session.finish()
        seen = collect(events, timeout=5)
        self.assertEqual(seen[-1], {'type': 'final', 'text': '今天天气不错。', 'source': 'dashscope'})
        first, second = log['runs']
        self.assertEqual(first['payload']['parameters']['language_hints'], ['zh'])
        self.assertNotIn('language_hints', second['payload']['parameters'])
        self.assertNotEqual(first['header']['task_id'], second['header']['task_id'])
        self.assertEqual(bytes(log['audio']), b''.join(fed))
        self.assertNotIn('language_hints', client.parameters(), 'remembered for this model')
        self.assertIn('language_hints', dash_client(server, model='fun-asr-realtime-2026-02-28').parameters())

    def test_other_failures_and_configured_hints_are_never_retried(self):
        for options, client_options in (({'refuse_hints': True, 'fail_code': 'Arrearage', 'fail_message': 'overdue'}, {}),
                                        ({'refuse_hints': True}, {'language_hints': ['zh', 'en']})):
            log = {}
            server = self.serve(bailian_service(log, **options))
            events = queue.Queue()
            session = dash_client(server, **client_options).session(events.put)
            session.start()
            session.feed(bytes(CHUNK))
            self.assertEqual(collect(events, timeout=5)[-1]['kind'], 'error')
            time.sleep(0.1)
            self.assertEqual(len(log['runs']), 1, options)


# -- the hotword source: the OpenLess dictionary (read-only) -----------------------------------
class DictionaryHotwordTests(unittest.TestCase):
    def test_enabled_phrases_most_used_then_newest_first(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = OpenLessSettings(folder)
            self.assertEqual(settings.hotwords(), [], 'no OpenLess: no hotwords')
            entries = [
                {'phrase': 'OpenLess', 'enabled': True, 'hits': 0, 'createdAt': '2026-10-12T11:35:55.0+00:00'},
                {'phrase': '气声', 'enabled': True, 'hits': 0, 'createdAt': '2026-10-08T13:38:54.6+00:00'},
                {'phrase': 'Enscape', 'enabled': True, 'hits': 7, 'createdAt': '2026-10-04T17:26:46.6+00:00'},
                {'phrase': '关掉', 'enabled': False, 'hits': 99},
                {'phrase': '一', 'enabled': True},
                {'phrase': ' 点声 ', 'hits': 3},
                {'phrase': '点声', 'enabled': True},
                {'phrase': 'x' * 40, 'enabled': True},
                {'phrase': '旧词', 'enabled': True, 'hits': True},
                'junk',
            ]
            path = Path(folder, 'dictionary.json')
            path.write_text(json.dumps(entries, ensure_ascii=False), encoding='utf-8')
            self.assertEqual(settings.hotwords(), ['Enscape', '点声', 'OpenLess', '气声', '旧词'])
            stamp = path.stat().st_mtime_ns
            path.write_text('{broken', encoding='utf-8')
            os.utime(path, ns=(stamp + 10**9, stamp + 10**9))   # re-read on the next call
            self.assertEqual(settings.hotwords(), [])


# -- the core: hotwords into Doubao, the engine hint ---------------------------------------------
class FakeOpenLess:
    def __init__(self, words=()):
        self.words = list(words)

    def mode(self):
        return None

    def correct(self, text):
        return text

    def hotwords(self):
        return list(self.words)


class CoreTests(Case):
    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.events = []
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)

    def core(self, engine=None):
        if engine is not None:
            runtime = self.base / 'runtime'
            runtime.mkdir(parents=True, exist_ok=True)
            (runtime / 'prefs.json').write_text(json.dumps({'engine': engine}), encoding='utf-8')
        core = make_core(self.base, FakeBackend(), events=self.events)
        self.addCleanup(core.close)
        return core

    def test_cloud_settings_pass_the_hotword_source_unless_switched_off(self):
        engine = json.loads(json.dumps(DEFAULTS['engine']))
        engine['kind'] = 'volc'
        source = lambda: ['点声']
        self.assertNotIn('hotwords', cloud_settings(engine)['volc'], 'unchanged without a source')
        self.assertIs(cloud_settings(engine, hotwords=source)['volc']['hotwords'], source, 'a missing pref counts as on')
        engine['volc']['hotwords'] = False   # prefs engine.volc.hotwords, once prefs.py has it
        self.assertNotIn('hotwords', cloud_settings(engine, hotwords=source)['volc'])

    def test_doubao_gets_the_dictionary_as_hotwords_and_200_ms_packets(self):
        core = self.core({'kind': 'volc'})
        core.openless = FakeOpenLess(['点声', '气声'])
        asr = core.controller.backend.asr
        self.assertEqual(json.loads(asr.request_payload()['request']['corpus']['context']),
                         {'hotwords': [{'word': '点声'}, {'word': '气声'}]})
        self.assertEqual(asr.packet_bytes(), 200 * MS)
        core.openless.words = []
        self.assertNotIn('corpus', asr.request_payload()['request'])
        core.openless = object()   # an OpenLess stand-in without a dictionary
        self.assertEqual(core.hotwords(), [])

    def test_a_saved_doubao_key_suggests_the_switch_without_reading_it(self):
        core = self.core()
        self.assertNotIn('suggest_volc', core.engine_view())
        raw = 'vk-' + uuid.uuid4().hex
        self.assertNotIn('error', core.secret_set(cloud.SECRET_VOLC_API_KEY, raw))
        self.assertTrue(core.engine_view()['suggest_volc'])
        engine_events = [value for kind, value in self.events if kind == 'engine']
        self.assertTrue(engine_events[-1]['suggest_volc'], 'the shell hears it when the key is saved')
        self.assertNotIn(raw, json.dumps(core.snapshot(), ensure_ascii=False))
        self.assertTrue(core.snapshot()['engine']['suggest_volc'])
        core.secret_delete(cloud.SECRET_VOLC_API_KEY)
        self.assertNotIn('suggest_volc', core.engine_view())
        self.assertNotIn('suggest_volc', [value for kind, value in self.events if kind == 'engine'][-1])

    def test_the_old_console_needs_its_app_id_and_doubao_itself_never_suggests(self):
        core = self.core({'kind': 'openless', 'volc': {'auth': 'legacy', 'app_id': ''}})
        core.secret_set(cloud.SECRET_VOLC_ACCESS_TOKEN, 'token-' + uuid.uuid4().hex)
        self.assertNotIn('suggest_volc', core.engine_view(), 'no App ID yet')
        engine = core.prefs.get('engine')
        engine['volc']['app_id'] = '1234567'
        self.assertTrue(core.volc_configured(engine))
        core.close()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        core = self.core({'kind': 'volc'})
        core.secret_set(cloud.SECRET_VOLC_API_KEY, 'vk-' + uuid.uuid4().hex)
        self.assertEqual(core.engine_view(), {'kind': 'volc', 'label': '豆包语音', 'external': False})


if __name__ == '__main__':
    unittest.main()
