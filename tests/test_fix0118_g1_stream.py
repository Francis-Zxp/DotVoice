"""Review 0.11.8 (group 1, streaming): the cloud watchdog no longer cancels a connected session
that is still uploading a backlog on a slow link; cancelling stops uploading at once; audio
taken from the queue but not yet sent still counts toward the 2-minute backlog limit; the WAV
check stops at an early error.

Everything runs on fakes: no network, no microphone, nothing is typed."""
import io
import queue
import tempfile
import threading
import time
import types
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from bridge.engines import stream
from bridge.engines.cloud import CloudBackend

try:
    from .test_engines import drain
except ImportError:
    from test_engines import drain


# -- CloudBackend watchdog -------------------------------------------------------------------
class SlowLinkASR:
    """A recogniser whose sessions connect at start(); after finish() the final arrives
    `final_after` seconds later (None: never), with live text every 50 ms meanwhile."""
    name = '测试识别'
    source = 'fake'

    def __init__(self, final_after=None, connects=True):
        self.final_after, self.connects = final_after, connects
        self.sessions = []

    def check(self):
        pass

    def session(self, emit):
        session = SlowLinkSession(self, emit)
        self.sessions.append(session)
        return session


class SlowLinkSession:
    def __init__(self, asr, emit):
        self.asr, self.emit = asr, emit
        self.stats = {'connected': None}
        self.cancelled = False

    def start(self):
        if self.asr.connects:
            self.stats['connected'] = time.monotonic()

    def feed(self, pcm):
        pass

    def finish(self):
        def run():
            began = time.monotonic()
            count = 0
            while not self.cancelled:
                if self.asr.final_after is not None and time.monotonic() - began >= self.asr.final_after:
                    self.emit({'type': 'final', 'text': '完整的一段话'})
                    return
                count += 1
                self.emit({'type': 'partial', 'text': '字' * count})
                time.sleep(0.05)
        threading.Thread(target=run, daemon=True).start()

    def cancel(self):
        self.cancelled = True


class WatchdogTests(unittest.TestCase):
    def dictate(self, asr, **options):
        typed = []
        backend = CloudBackend(asr, inserter=typed.append, **options)
        self.addCleanup(backend.close)
        backend.connect()
        backend.start()
        backend.feed(bytes(3200))
        backend.stop()
        began = time.monotonic()
        events = drain(backend, timeout=5)
        return events, typed, time.monotonic() - began

    def test_a_connected_session_still_uploading_is_not_cancelled(self):
        asr = SlowLinkASR(final_after=0.5)
        events, typed, _ = self.dictate(asr, asr_watchdog=0.1, asr_cap=5)
        self.assertEqual(events[-1]['type'], 'result', events[-1])
        self.assertEqual(typed, ['完整的一段话'])
        self.assertFalse(asr.sessions[0].cancelled)

    def test_a_connected_session_is_still_capped(self):
        asr = SlowLinkASR(final_after=None)
        events, typed, waited = self.dictate(asr, asr_watchdog=0.1, asr_cap=0.5)
        self.assertEqual(events[-1]['kind'], 'error')
        self.assertIn('网络太慢', events[-1]['message'])
        self.assertGreaterEqual(waited, 0.4)
        self.assertTrue(asr.sessions[0].cancelled)
        self.assertEqual(typed, [])

    def test_a_session_that_never_connected_still_ends_at_the_watchdog(self):
        asr = SlowLinkASR(final_after=None, connects=False)
        events, _, waited = self.dictate(asr, asr_watchdog=0.2, asr_cap=5)
        self.assertEqual(events[-1]['kind'], 'error')
        self.assertIn('没有返回结果', events[-1]['message'])
        self.assertLess(waited, 2)
        self.assertTrue(asr.sessions[0].cancelled)


# -- StreamSession backlog -------------------------------------------------------------------
class FakeSocket:
    def __init__(self):
        self.sock = types.SimpleNamespace(pending=lambda: False)

    def getheaders(self):
        return {}

    def settimeout(self, timeout):
        pass

    def close(self, timeout=None):
        pass

    def shutdown(self):
        pass


class FakeSession(stream.StreamSession):
    name = '测试识别'
    source = 'fake'
    ack_grace = 0.0

    def __init__(self, emit, send_delay=0.0):
        super().__init__(emit)
        self.send_delay = send_delay
        self.sent = []                     # (time, bytes) per audio message
        self.gate = None                   # set: the first send waits for it (a stuck uplink)
        self.in_send = threading.Event()
        self.refuse_once = False           # the server refuses the request once (Retry)

    def endpoint(self):
        return 'ws://127.0.0.1:9/fake', {}

    def open(self, ws):
        pass

    def retryable(self):
        return self.refuse_once

    def send_audio(self, ws, chunk):
        self.in_send.set()
        if self.gate is not None:
            self.gate.wait(5)
            self.gate = None
        time.sleep(self.send_delay)
        self.sent.append((time.monotonic(), len(chunk)))

    def finish_audio(self, ws, remainder):
        if remainder:
            self.sent.append((time.monotonic(), len(remainder)))
        if not self.refuse_once or self.retries:
            self.final('好的')

    def _readable(self, ws, timeout):
        if self.refuse_once and not self.retries and self.finish_sent:
            return True
        time.sleep(timeout)
        return False

    def _receive(self, ws):
        raise stream.Retry('测试识别拒绝了请求')


class StreamTestCase(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(stream.websocket, 'create_connection', lambda url, **options: FakeSocket())
        patcher.start()
        self.addCleanup(patcher.stop)

    def session(self, **options):
        events = queue.Queue()
        session = FakeSession(events.put, **options)
        self.addCleanup(session.cancel)
        return session, events


class CancelBacklogTests(StreamTestCase):
    def test_cancel_stops_uploading_the_backlog(self):
        session, _ = self.session(send_delay=0.05)   # an uplink at half the audio rate
        for _ in range(50):                           # 5 s queued while connecting
            session.feed(bytes(stream.PACKET))
        session.start()
        deadline = time.monotonic() + 2
        while len(session.sent) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        session.cancel()
        cancelled_at = time.monotonic()
        session.thread.join(2)
        self.assertFalse(session.thread.is_alive(), 'the worker lets go of the socket')
        after = [at for at, _ in session.sent if at > cancelled_at]
        self.assertLessEqual(len(after), 1, 'at most the send already under way')


class BacklogAccountingTests(StreamTestCase):
    def test_audio_taken_but_unsent_counts_toward_the_limit(self):
        limit = 10 * stream.PACKET
        patcher = patch.object(stream, 'MAX_BACKLOG', limit)
        patcher.start()
        self.addCleanup(patcher.stop)
        session, events = self.session()
        session.gate = threading.Event()
        for _ in range(4):
            session.feed(bytes(stream.PACKET))
        session.start()
        self.assertTrue(session.in_send.wait(2), 'the worker took the 4 packets and is stuck sending')
        accepted = 4
        for _ in range(40):
            session.feed(bytes(stream.PACKET))
            if session.overflow:
                break
            accepted += 1
        self.assertTrue(session.overflow)
        self.assertEqual(accepted * stream.PACKET, limit, 'the 4 packets in the worker still count')
        session.gate.set()
        event = events.get(timeout=3)
        self.assertEqual(event.get('kind'), 'error')
        self.assertIn('积压', event['message'])
        self.assertEqual(len(session.sent), 1, 'no more of the doomed backlog is uploaded')

    def test_the_count_returns_to_zero_after_a_retry_replays_the_audio(self):
        session, events = self.session()
        session.refuse_once = True
        for _ in range(6):
            session.feed(bytes(stream.PACKET))
        session.finish()
        session.start()
        event = events.get(timeout=3)
        self.assertEqual(event, {'type': 'final', 'text': '好的', 'source': 'fake'})
        self.assertEqual(session.retries, 1)
        self.assertEqual(sum(size for _, size in session.sent), 2 * 6 * stream.PACKET, 'sent, then replayed')
        self.assertEqual(session.backlog, 0)


# -- manual WAV check --------------------------------------------------------------------------
class ScriptedSession:
    final_timeout = 1
    stats, started_at, logid = {}, None, ''

    def __init__(self, emit, first):
        self.emit, self.first = emit, first
        self.fed, self.finished = 0, False

    def start(self):
        self.emit(self.first)   # e.g. a refused key: answered before any audio

    def feed(self, pcm):
        self.fed += 1

    def finish(self):
        self.finished = True

    def cancel(self):
        pass


class ScriptedClient:
    def __init__(self, first):
        self.first, self.sessions = first, []

    def session(self, emit):
        session = ScriptedSession(emit, self.first)
        self.sessions.append(session)
        return session


class RunWavTests(unittest.TestCase):
    def wav(self, seconds=2):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'sample.wav'
        with wave.open(str(path), 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(bytes(stream.BYTES_PER_SECOND * seconds))
        return path

    def test_an_early_error_ends_the_check_at_once_with_exit_code_2(self):
        client = ScriptedClient({'kind': 'error', 'message': '测试识别鉴权失败，请检查 API Key'})
        out = io.StringIO()
        began = time.monotonic()
        code = stream.run_wav(client, self.wav(), realtime=False, out=out)
        self.assertEqual(code, 2, out.getvalue())
        self.assertLess(time.monotonic() - began, 1, 'no wait for a second terminal event')
        self.assertIn('鉴权失败', out.getvalue())
        self.assertNotIn('等待结果超时', out.getvalue())
        session = client.sessions[0]
        self.assertEqual(session.fed, 1, 'the rest of the WAV is not fed')
        self.assertFalse(session.finished)

    def test_an_early_final_is_reported(self):
        client = ScriptedClient({'type': 'final', 'text': '提前结束。'})
        out = io.StringIO()
        self.assertEqual(stream.run_wav(client, self.wav(), realtime=False, out=out), 0)
        self.assertIn('最终结果：提前结束。', out.getvalue())


if __name__ == '__main__':
    unittest.main()
