"""Shared text insertion: Unicode, no Enter submission, partial-send safety."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge.fast_insert import KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, PartlyTyped, key_events, type_text  # noqa: E402


class TypingTests(unittest.TestCase):
    def test_unicode_strokes_surrogates_without_submitting(self):
        events = key_events('中a\n😀')
        self.assertEqual(events[0], (0, ord('中'), KEYEVENTF_UNICODE))
        self.assertEqual(events[1], (0, ord('中'), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
        self.assertNotIn((0x0D, 0, 0), events)  # no VK_RETURN
        self.assertEqual(events[4], (0, ord(' '), KEYEVENTF_UNICODE))
        self.assertEqual(len(events), 2 + 2 + 2 + 4)  # emoji = two UTF-16 units

    def test_line_endings_are_inline_spaces_never_enter_keys(self):
        events = key_events('第一条\r\n第二条\r第三条\n第四条')
        self.assertTrue(all(vk == 0 for vk, _, _ in events))
        typed = ''.join(chr(scan) for _, scan, flags in events if not flags & KEYEVENTF_KEYUP)
        self.assertEqual(typed, '第一条 第二条 第三条 第四条')

    def test_partial_first_send_is_not_reported_as_zero_input(self):
        with patch('bridge.fast_insert.c.windll.user32.SendInput', return_value=2):
            with self.assertRaises(PartlyTyped):
                type_text('前缀后缀')

    def test_zero_first_send_still_allows_fallback(self):
        with patch('bridge.fast_insert.c.windll.user32.SendInput', return_value=0):
            with self.assertRaises(OSError) as caught:
                type_text('测试')
        self.assertNotIsInstance(caught.exception, PartlyTyped)

    def test_long_text_goes_in_batches(self):
        batches, pauses = [], []
        type_text('字' * 300, sender=batches.append, pause=pauses.append)
        self.assertEqual(sum(len(b) for b in batches), 600)
        self.assertEqual(len(batches), 3)
        self.assertEqual(len(pauses), 2)

    def test_cloud_partial_insertion_keeps_result_without_retyping_or_replaying(self):
        from bridge.engines.cloud import CloudBackend
        from tests.test_engines import FakeASR, drain
        asr = FakeASR(final='前缀后缀')
        backend = CloudBackend(asr, inserter=type_text)
        self.addCleanup(backend.close)
        with patch('bridge.fast_insert.c.windll.user32.SendInput', return_value=2) as send:
            backend.connect()
            backend.start()
            backend.feed(bytes(3200))
            backend.stop()
            events = drain(backend)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(len(asr.sessions), 1)
        self.assertEqual([event['text'] for event in events if event.get('type') == 'result'], ['前缀后缀'])
        self.assertTrue(any('部分文字' in event.get('message', '') for event in events))




if __name__ == '__main__':
    unittest.main()
