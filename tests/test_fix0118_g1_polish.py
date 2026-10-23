"""Review 0.11.8 (group 1, polish): a reply cut off by the service's output limit or filter is
never typed as the whole text, and one polish has an overall deadline (bytes trickling in no
longer keep it alive; a long text may wait longer for its reply; the cloud engine sets the
deadline from the release so the controller's two-minute limit is never reached).

Only a local fake relay on 127.0.0.1 is contacted; nothing is typed."""
import http.server
import json
import threading
import time
import unittest
from unittest.mock import patch

from bridge.engines import cloud, llm
from bridge.engines.cloud import CloudBackend
from bridge.engines.llm import PolishError, Polisher

try:
    from .test_engines import FakeASR, drain
except ImportError:
    from test_engines import FakeASR, drain

SOURCE = '我们明天下午三点在三楼会议室开会，讨论下季度的预算，请大家提前准备好各自部门的数据，并且在会后把纪要发给我。'
KEY = 'test-relay-key-0001'


def reply_with(text, finish_reason='stop'):
    choice = {'index': 0, 'message': {'role': 'assistant', 'content': text}}
    if finish_reason is not None:
        choice['finish_reason'] = finish_reason
    return {'choices': [choice]}


class Relay:
    """Answers each request with the next (status, body). delay: seconds before the headers;
    drip: newline keep-alive bytes sent 0.2 s apart before the body (no Content-Length);
    stall: seconds of silence after them."""

    def __init__(self, replies=(), delay=0.0, drip=0, stall=0.0):
        owner = self
        self.requests, self.replies, self.delay, self.drip = [], list(replies), delay, drip
        self.stall = stall

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                owner.requests.append(json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0))))
                status, reply = owner.replies.pop(0) if owner.replies else (200, reply_with('好的。'))
                data = json.dumps(reply, ensure_ascii=False).encode('utf-8')
                try:
                    time.sleep(owner.delay)
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    if not owner.drip:
                        self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    for _ in range(owner.drip):
                        self.wfile.write(b'\n')
                        self.wfile.flush()
                        time.sleep(0.2)
                    time.sleep(owner.stall)
                    self.wfile.write(data)
                except OSError:
                    pass   # the client gave up (the deadline): expected

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True).start()

    def base(self):
        return f'http://127.0.0.1:{self.httpd.server_port}/v1'

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RelayTestCase(unittest.TestCase):
    def relay(self, replies=(), **options):
        relay = Relay(replies, **options)
        self.addCleanup(relay.close)
        return relay


class CutOffReplyTests(RelayTestCase):
    def test_a_reply_cut_off_by_the_output_limit_or_a_filter_is_an_error(self):
        for reason, fragment in (('length', '不完整'), ('max_tokens', '不完整'), ('content_filter', '内容审核')):
            relay = self.relay([(200, reply_with(SOURCE[:20], reason))] * 2)
            for style in ('light', 'custom'):
                with self.subTest(reason=reason, style=style):
                    polisher = Polisher(relay.base(), KEY, 'm', style=style, prompt='整理文字')
                    with self.assertRaisesRegex(PolishError, fragment):
                        polisher.polish(SOURCE)

    def test_complete_replies_still_pass(self):
        relay = self.relay([(200, reply_with('今天天气不错。', 'stop')), (200, reply_with('今天天气不错。', None))])
        polisher = Polisher(relay.base(), KEY, 'm', style='light')
        for _ in range(2):   # some relays leave finish_reason out
            self.assertEqual(polisher.polish('嗯，今天那个天气不错'), '今天天气不错。')

    def test_cloud_types_and_saves_the_whole_transcript_instead(self):
        relay = self.relay([(200, reply_with(SOURCE[:20], 'length'))] * 2)
        polisher = Polisher(relay.base(), KEY, 'm', style='light')
        typed = []
        backend = CloudBackend(FakeASR(final=SOURCE), polisher, inserter=typed.append)
        self.addCleanup(backend.close)
        backend.connect()
        for insert in (True, False):   # dictation, then a note
            backend.insert_text = insert
            backend.start()
            backend.feed(bytes(3200))
            backend.stop()
            events = drain(backend, timeout=5)
            self.assertEqual(events[-1]['text'], SOURCE)
            notices = [e['message'] for e in events if e.get('kind') == 'notice']
            self.assertEqual(len(notices), 1)
            self.assertIn('不完整', notices[0])
            self.assertIn('已改用识别原文', notices[0])
        self.assertEqual(typed, [SOURCE], 'typed once, in full; the note is not typed')


class PolishDeadlineTests(RelayTestCase):
    def test_bytes_trickling_in_cannot_pass_the_deadline(self):
        relay = self.relay(drip=25)   # 5 s of keep-alive newlines, each well inside the 20 s per-read timeout
        polisher = Polisher(relay.base(), KEY, 'm', style='light')
        began = time.monotonic()
        with self.assertRaisesRegex(PolishError, '超时'):
            polisher.polish('嗯那个我们明天开会吧', deadline=time.monotonic() + 1.0)
        self.assertLess(time.monotonic() - began, 2.5)

    def test_silence_after_trickling_bytes_ends_at_the_deadline_too(self):
        relay = self.relay(drip=10, stall=5)   # 2 s of newlines, then 5 s of nothing
        polisher = Polisher(relay.base(), KEY, 'm', style='light')   # the socket opens with a 2.6 s timeout
        began = time.monotonic()
        with self.assertRaisesRegex(PolishError, '超时'):
            polisher.polish('嗯那个我们明天开会吧', deadline=time.monotonic() + 2.6)
        self.assertLess(time.monotonic() - began, 3.4, 'the read after the last byte waits only what is left')

    def test_trickling_bytes_before_the_reply_are_still_fine_within_the_deadline(self):
        relay = self.relay([(200, reply_with('我们明天开会吧。'))], drip=2)
        polisher = Polisher(relay.base(), KEY, 'm', style='light')
        self.assertEqual(polisher.polish('嗯那个我们明天开会吧', deadline=time.monotonic() + 5), '我们明天开会吧。')

    def test_a_long_text_may_wait_longer_for_its_reply(self):
        relay = self.relay([(200, reply_with('好的。')), (200, reply_with('整理后的一段长文字。'))], delay=0.8)
        polisher = Polisher(relay.base(), KEY, 'm', style='light', timeout=0.25)
        with self.assertRaisesRegex(PolishError, '超时'):
            polisher.polish('好的')
        long_text = '我们明天下午三点开会' * 8   # 80 characters: up to 2 s per read
        self.assertEqual(polisher.polish(long_text), '整理后的一段长文字。')

    def test_nothing_is_sent_once_the_deadline_has_passed(self):
        relay = self.relay()
        polisher = Polisher(relay.base(), KEY, 'm', style='light')
        with self.assertRaisesRegex(PolishError, '来不及润色'):
            polisher.polish('我们明天开会', deadline=time.monotonic() - 1)
        self.assertEqual(relay.requests, [])

    def test_the_temperature_retry_is_skipped_near_the_deadline(self):
        refusal = {'error': {'message': "Unsupported value: 'temperature' does not support 0.3 with this model."}}
        relay = self.relay([(400, refusal), (200, reply_with('好的。'))])
        polisher = Polisher(relay.base(), KEY, 'm', style='light')
        with self.assertRaisesRegex(PolishError, '来不及润色'):
            polisher.polish('好的', deadline=time.monotonic() + 2)
        self.assertEqual(len(relay.requests), 1)

    def test_probe_and_plain_calls_get_a_default_deadline(self):
        polisher = Polisher('https://example.com/v1', KEY, 'm', style='light')
        seen = []

        def complete(text, temperature, deadline):
            seen.append(deadline - time.monotonic())
            return '好的。'
        with patch.object(polisher, '_complete', side_effect=complete):
            polisher.polish('好的')
        self.assertAlmostEqual(seen[0], llm.POLISH_BUDGET, delta=1)

    def test_cloud_sets_the_deadline_from_the_release(self):
        polisher = Polisher('https://example.com/v1', KEY, 'm', style='light')
        seen = []

        def complete(text, temperature, deadline):
            seen.append(deadline)
            return '今天天气不错。'
        backend = CloudBackend(FakeASR(final='嗯今天天气不错'), polisher, inserter=lambda text: None)
        self.addCleanup(backend.close)
        with patch.object(polisher, '_complete', side_effect=complete):
            backend.connect()
            backend.start()
            backend.feed(bytes(3200))
            backend.stop()
            stopped = backend.job.stopped_at
            self.assertEqual(drain(backend)[-1]['text'], '今天天气不错。')
        self.assertAlmostEqual(seen[0], stopped + cloud.POLISH_BY, places=6)
        self.assertLess(cloud.ASR_CAP, cloud.POLISH_BY)
        self.assertLessEqual(cloud.POLISH_BY, 120 - 15, 'room to type inside the two-minute limit')

    def test_plain_callables_and_other_polishers_are_called_as_before(self):
        typed = []
        backend = CloudBackend(FakeASR(final='abc'), lambda text: text.upper(), inserter=typed.append)
        self.addCleanup(backend.close)
        backend.connect()
        backend.start()
        backend.stop()
        self.assertEqual(drain(backend)[-1]['text'], 'ABC')
        self.assertEqual(typed, ['ABC'])


if __name__ == '__main__':
    unittest.main()
