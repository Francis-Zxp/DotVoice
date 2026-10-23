"""Review 0.11.8 (group 1, OpenLess and kept text): a dropped OpenLess connection, a handshake
that times out and an unreadable reply are reported in Chinese instead of raw exception text;
after partial typing the notice no longer says to paste the full text as it is.

Everything runs on fakes: no socket is opened, nothing is typed."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import websocket

from bridge import backend as openless
from bridge.backend import OpenLessBackend
from bridge.elevation import SETTING, kept_message

SESSION = '00000000-0000-0000-0000-000000000001'


class FakeSocket:
    """error: what recv() and ping() raise, and send() too unless send_error says otherwise."""

    def __init__(self, error=None, reply=None, send_error='same', close_error=None):
        self.error, self.reply = error, reply
        self.send_error = error if send_error == 'same' else send_error
        self.close_error = close_error
        self.closed = False

    def recv(self):
        if self.error:
            raise self.error
        return self.reply

    def send(self, data, **options):
        if self.send_error:
            raise self.send_error

    def ping(self):
        if self.error:
            raise self.error

    def settimeout(self, timeout):
        pass

    def close(self, timeout=None):
        self.closed = True
        if self.close_error:
            raise self.close_error


def dropped():
    return (websocket.WebSocketConnectionClosedException('Connection to remote host was lost.'),
            ConnectionResetError(10054, 'An existing connection was forcibly closed by the remote host'),
            BrokenPipeError(32, 'Broken pipe'))


class DroppedConnectionTests(unittest.TestCase):
    def backend(self, sock):
        backend = OpenLessBackend()
        backend.ws, backend.session_id = sock, SESSION
        backend.last_ping = time.monotonic()   # no keep-alive ping due
        return backend

    def test_a_connection_lost_mid_session_reads_in_chinese(self):
        for error in dropped():
            backend = self.backend(FakeSocket(error))
            for name, call in (('poll', backend.poll), ('feed', lambda: backend.feed(b'\0\0')),
                               ('stop', backend.stop)):
                with self.subTest(error=type(error).__name__, call=name):
                    with self.assertRaises(RuntimeError) as caught:
                        call()
                    self.assertEqual(str(caught.exception), openless.LOST)

    def test_a_failed_ping_is_the_same_message(self):
        backend = self.backend(FakeSocket(dropped()[0]))
        backend.last_ping = 0
        with self.assertRaisesRegex(RuntimeError, 'OpenLess 连接中断'):
            backend.poll()

    def test_a_clean_close_and_a_quiet_socket_are_unchanged(self):
        self.assertIsNone(self.backend(FakeSocket(websocket.WebSocketTimeoutException('timed out'))).poll())
        with self.assertRaisesRegex(RuntimeError, 'OpenLess 连接中断'):
            self.backend(FakeSocket(reply='')).poll()

    def test_unreadable_replies(self):
        for reply in ('HTTP/1.1 400 Bad Request', '[1, 2]', '"result"', 'null'):
            with self.subTest(reply=reply), self.assertRaises(RuntimeError) as caught:
                self.backend(FakeSocket(reply=reply)).poll()
            self.assertEqual(str(caught.exception), openless.UNKNOWN_REPLY)
        event = self.backend(FakeSocket(reply='{"type":"result","text":"你好"}'))
        self.assertEqual(event.poll(), {'type': 'result', 'text': '你好'})
        self.assertIsNone(event.session_id)

    def test_cancel_on_a_dead_socket_still_ends_the_session(self):
        backend = self.backend(FakeSocket(dropped()[1]))
        with self.assertRaisesRegex(RuntimeError, 'OpenLess 连接中断'):
            backend.cancel()
        self.assertIsNone(backend.session_id)


class HandshakeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        pin = Path(folder.name) / 'pin.txt'
        pin.write_text('123456', encoding='utf-8')
        self.backend = OpenLessBackend(pin_file=pin, preferences_file=Path(folder.name) / 'none.json')

    def connect(self, sock):
        with patch('bridge.backend.websocket.create_connection', return_value=sock):
            with self.assertRaises(RuntimeError) as caught:
                self.backend.connect()
        self.assertIsNone(self.backend.ws)
        self.assertTrue(sock.closed)
        return str(caught.exception)

    def test_no_answer_to_the_pairing_request(self):
        message = self.connect(FakeSocket(websocket.WebSocketTimeoutException('Connection timed out'), send_error=None))
        self.assertIn('没有回应配对请求', message)

    def test_a_reply_that_is_not_json_is_a_changed_handshake(self):
        self.assertIn('握手格式已变化', self.connect(FakeSocket(reply='HTTP/1.1 400 Bad Request')))

    def test_a_connection_dropped_during_the_handshake(self):
        for error in dropped():
            with self.subTest(error=type(error).__name__):
                self.assertIn('暂不可达', self.connect(FakeSocket(error)))

    def test_a_failing_close_does_not_hide_the_reason(self):
        sock = FakeSocket(websocket.WebSocketTimeoutException('Connection timed out'), send_error=None,
                          close_error=OSError('already closed'))
        self.assertIn('没有回应配对请求', self.connect(sock))

    def test_a_good_handshake_still_connects(self):
        sock = FakeSocket(reply='{"type":"auth","ok":true}')
        with patch('bridge.backend.websocket.create_connection', return_value=sock):
            self.backend.connect()
        self.assertIs(self.backend.ws, sock)


class PartialTypingNoticeTests(unittest.TestCase):
    def test_partial_typing_says_to_remove_what_went_in_first(self):
        message = kept_message(True, reason='partial')
        self.assertTrue(message.startswith('部分文字没能输入'), message)
        self.assertIn('全文已复制到剪贴板', message)
        self.assertIn('请先删掉已输入的部分', message)
        self.assertIn('按 Ctrl+V 粘贴', message)
        self.assertIn('锁屏或 UAC', message)
        self.assertNotIn('管理员', message)
        self.assertNotIn(SETTING, message)
        self.assertIn('全文保留在点声主窗口的本次结果里', kept_message(False, reason='partial'))

    def test_a_full_refusal_still_says_paste(self):
        message = kept_message(True, reason='refused')
        self.assertIn('文字已复制到剪贴板，按 Ctrl+V 粘贴', message)
        self.assertNotIn('删掉', message)


if __name__ == '__main__':
    unittest.main()
