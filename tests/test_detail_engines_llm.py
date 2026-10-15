"""Polish details: a 30x is reported and never followed (the key stays with the configured URL),
the speaker's own leading "结果：" survives cleanup, and filler-only speech may come back empty.
Only local test servers are contacted."""
import http.server
import json
import threading
import unittest
from unittest.mock import patch

from bridge.engines import llm
from bridge.engines.llm import PolishError, Polisher, clean_output
from bridge.engines.cloud import CloudBackend
from tests.test_engines import FakeASR, drain

KEY = 'sk-detail-redirect-key-123456'


def chat_reply(text):
    return {'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}


class Server:
    """Answers each request with the next (status, headers, body) and records what arrived."""

    def __init__(self, replies=()):
        owner = self
        self.requests, self.replies = [], list(replies)

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self):
                length = int(self.headers.get('Content-Length') or 0)
                owner.requests.append({'method': self.command, 'path': self.path,
                                       'headers': {k.lower(): v for k, v in self.headers.items()},
                                       'body': self.rfile.read(length) if length else b''})
                status, headers, body = owner.replies.pop(0) if owner.replies else (200, {}, chat_reply('好的。'))
                data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode('utf-8')
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = answer

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


class RedirectTests(unittest.TestCase):
    def server(self, replies=()):
        server = Server(replies)
        self.addCleanup(server.close)
        return server

    def test_redirects_are_reported_not_followed(self):
        elsewhere = self.server()
        target = elsewhere.base('/v1/chat/completions')
        for status in (301, 302, 303, 307, 308):
            configured = self.server([(status, {'Location': target}, b'<html>moved</html>')])
            with self.assertRaises(PolishError) as caught:
                Polisher(configured.base('/v1'), KEY, 'm', style='light').polish('测试一下')
            message = str(caught.exception)
            self.assertIn(f'跳转（HTTP {status}）', message)
            self.assertIn('Base URL', message)
            self.assertIn(target, message, 'the user is told where it wanted to go')
            self.assertNotIn(KEY, message)
            self.assertEqual(configured.requests[0]['method'], 'POST')
            self.assertEqual(len(configured.requests), 1)
        self.assertEqual(elsewhere.requests, [], 'the key never reaches the redirect target')

    def test_redirect_message_wins_over_body_keywords(self):
        self.assertTrue(llm.http_error_message(302, '', 'quota balance').startswith('润色地址发生了跳转（HTTP 302）'))
        self.assertTrue(llm.http_error_message(402, '', '').startswith('润色服务额度不足'))


class CleanOutputTests(unittest.TestCase):
    def test_speakers_own_label_is_kept(self):
        self.assertEqual(clean_output('结果：今年营收增长了12%。', '结果今年营收增长了百分之十二'),
                         '结果：今年营收增长了12%。')
        self.assertEqual(clean_output('输出：明天上线。', '  输出：明天上线'), '输出：明天上线。')

    def test_labels_the_model_adds_are_still_stripped(self):
        self.assertEqual(clean_output('结果：明天开会。', '嗯明天开会'), '明天开会。')
        self.assertEqual(clean_output('润色后的文字：“好的。”', '好的'), '好的。')
        self.assertEqual(clean_output('整理后内容： 今天天气不错。', '那个今天天气不错'), '今天天气不错。')


class EmptyLightResultTests(unittest.TestCase):
    def polisher(self, reply):
        server = Server([(200, {}, chat_reply(reply))])
        self.addCleanup(server.close)
        return Polisher(server.base('/v1'), 'k', 'm', style='light')

    def test_filler_only_speech_may_come_back_empty(self):
        self.assertEqual(self.polisher('').polish('嗯，那个，就是说……'), '')
        self.assertEqual(self.polisher('').polish('呃呃呃，啊，哈哈哈哈'), '')

    def test_empty_result_still_fails_when_content_would_be_lost(self):
        with self.assertRaisesRegex(PolishError, '为空'):
            self.polisher('').polish('嗯那个我们明天下午三点开会吧')

    def test_short_commands_are_not_discarded_as_fillers(self):
        polisher = Polisher('https://example.com/v1', 'k', 'm', style='light')
        with patch.object(polisher, '_complete', return_value=''):
            for text in ('不要发送', '取消', '明天开会', '42', 'yes'):
                with self.subTest(text=text), self.assertRaisesRegex(PolishError, '为空'):
                    polisher.polish(text)

    def test_empty_polish_keeps_short_dictation_and_reports_fallback(self):
        polisher = Polisher('https://example.com/v1', 'k', 'm', style='light')
        typed = []
        backend = CloudBackend(FakeASR(final='不要发送'), polisher, inserter=typed.append)
        self.addCleanup(backend.close)
        with patch.object(polisher, '_complete', return_value=''):
            backend.start()
            backend.stop()
            events = drain(backend)
        self.assertEqual(typed, ['不要发送'])
        self.assertEqual(events[-1]['text'], '不要发送')
        self.assertTrue(any('已改用识别原文' in e.get('message', '') for e in events))

    def test_filler_pattern(self):
        self.assertEqual(llm.FILLERS.sub('', '嗯那个就是说我们开会'), '我们开会')


if __name__ == '__main__':
    unittest.main()
