"""极速落字: OpenLess raw mode + local preview text typed at once, with OpenLess fallback."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import controller as controller_module  # noqa: E402
from bridge.controller import Controller  # noqa: E402
from bridge.fast_insert import KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, OpenLessSettings, PartlyTyped, key_events, type_text  # noqa: E402
from tests.test_bridge import FakeBackend, FakeCapture, event, wait_for  # noqa: E402


class SettingsTests(unittest.TestCase):
    def test_mode_and_rules_follow_the_files(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = OpenLessSettings(folder)
            self.assertIsNone(settings.mode())
            self.assertEqual(settings.correct('U15 渲染'), 'U15 渲染')
            prefs, rules = Path(folder) / 'preferences.json', Path(folder) / 'correction-rules.json'
            prefs.write_text(json.dumps({'defaultMode': 'raw', 'other': 1}), encoding='utf-8')
            rules.write_text(json.dumps([
                {'pattern': 'U15', 'replacement': 'UE5', 'enabled': True},
                {'pattern': '旧', 'replacement': '新', 'enabled': False},
                {'pattern': '', 'replacement': 'x'}, 'junk']), encoding='utf-8')
            self.assertEqual(settings.mode(), 'raw')
            self.assertEqual(settings.correct('U15 旧'), 'UE5 旧')
            prefs.write_text(json.dumps({'defaultMode': 'light'}), encoding='utf-8')
            stamp = prefs.stat().st_mtime_ns + 10_000_000
            os.utime(prefs, ns=(stamp, stamp))
            self.assertEqual(settings.mode(), 'light')
            prefs.write_text('{broken', encoding='utf-8')
            os.utime(prefs, ns=(stamp + 10_000_000, stamp + 10_000_000))
            self.assertIsNone(settings.mode())


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


class FakePreview:
    def __init__(self):
        self.calls = []
        self.ready = True
    def start(self, session): self.calls.append(('start', session))
    def feed(self, pcm): self.calls.append('feed')
    def finish(self): self.calls.append('finish')
    def cancel(self): self.calls.append('cancel')


class FastSessionTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.notifications = []
        self.typed = []
        self.control = Controller(lambda *value: self.notifications.append(value), self.backend, FakeCapture)
        self.preview = FakePreview()
        self.control.preview = self.preview
        self.control.fast = lambda: True
        self.control.typer = self.typed.append
        self.control.corrector = lambda text: text.replace('U15', 'UE5')
        self.control.command('arm')
        wait_for(lambda: self.control.phase == 'idle')

    def tearDown(self):
        controller_module.FAST_WAIT = 3.0
        self.control.close()

    def record(self, chunks=3):
        self.control.on_hid(event(0x76))
        wait_for(lambda: self.control.phase == 'recording')
        for _ in range(chunks):
            self.control._audio(b'\x04\x00' * 800)
        self.control.on_hid(event(0x77))
        wait_for(lambda: self.control.phase == 'processing')

    def results(self):
        return [n[1] for n in self.notifications if n[0] == 'result']

    def test_local_text_is_typed_without_an_openless_session(self):
        self.record()
        self.assertNotIn('start', self.backend.calls)
        self.assertFalse(any(isinstance(call, tuple) for call in self.backend.calls))
        self.assertIn('finish', self.preview.calls)
        self.control.local_final(self.control.session_id, ' 打开 U15 ')
        wait_for(lambda: self.control.phase == 'idle')
        self.assertEqual(self.typed, ['打开 UE5'])
        self.assertTrue(self.results()[-1]['local'])
        self.assertEqual(self.results()[-1]['text'], '打开 UE5')

    def test_stale_session_text_is_ignored(self):
        self.record()
        self.control.local_final('another-session', '旧的')
        time.sleep(.1)
        self.assertEqual(self.typed, [])
        self.assertEqual(self.control.phase, 'processing')

    def test_no_local_text_in_time_hands_the_audio_to_openless(self):
        controller_module.FAST_WAIT = .1
        self.record(chunks=3)
        wait_for(lambda: 'stop' in self.backend.calls)
        frames = [call for call in self.backend.calls if isinstance(call, tuple)]
        self.assertEqual(self.backend.calls.count('start'), 1)
        self.assertEqual(len(frames), 3)
        self.assertLess(self.backend.calls.index('start'), self.backend.calls.index(frames[0]))
        self.backend.incoming.put({'type': 'result', 'text': '云端'})
        wait_for(lambda: self.control.phase == 'idle')
        self.assertEqual(self.typed, [])
        self.assertFalse(self.results()[-1]['local'])

    def test_empty_local_text_or_refused_typing_falls_back(self):
        self.record()
        self.control.local_final(self.control.session_id, '   ')
        wait_for(lambda: 'stop' in self.backend.calls)

        self.backend.incoming.put({'type': 'result', 'text': '云端'})
        wait_for(lambda: self.control.phase == 'idle')
        self.backend.calls.clear()

        def refuse(text):
            raise OSError('admin window')
        self.control.typer = refuse
        self.record()
        self.control.local_final(self.control.session_id, '你好')
        wait_for(lambda: 'stop' in self.backend.calls)

    def test_partial_first_batch_never_replays_the_recording(self):
        self.record()
        self.control.typer = type_text
        with patch('bridge.fast_insert.c.windll.user32.SendInput', return_value=2):
            self.control.local_final(self.control.session_id, '前缀后缀')
            wait_for(lambda: self.control.phase == 'idle')
        self.assertNotIn('start', self.backend.calls)
        self.assertEqual(self.results()[-1]['text'], '前缀后缀')
        self.assertTrue(any(kind == 'notice' and '部分文字' in value for kind, value in self.notifications))

    def test_notes_and_unready_preview_use_openless(self):
        self.preview.ready = False
        self.record()
        self.assertIn('start', self.backend.calls)
        self.backend.incoming.put({'type': 'result', 'text': '云端'})
        wait_for(lambda: self.control.phase == 'idle')
        self.preview.ready = True
        self.control.fast = lambda: False
        self.backend.calls.clear()
        self.record()
        self.assertIn('start', self.backend.calls)



class PhraseTests(unittest.TestCase):
    def test_dictionary_phrases_come_back_by_sound_and_case(self):
        from bridge.fast_insert import restore_phrases
        text = restore_phrases('讨论点生的功能，松开笔剑，用enscape渲染，银行很远，点声不变',
                               ['点声', '笔键', 'Enscape', '行人'])
        self.assertEqual(text, '讨论点声的功能，松开笔键，用Enscape渲染，银行很远，点声不变')
        self.assertEqual(restore_phrases('myenscape', ['Enscape']), 'myenscape')  # inside a word: untouched

    def test_settings_apply_dictionary_then_rules(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, 'dictionary.json').write_text(json.dumps([
                {'phrase': '点声', 'enabled': True}, {'phrase': '关掉', 'enabled': False}, {'phrase': '一'}]), encoding='utf-8')
            Path(folder, 'correction-rules.json').write_text(json.dumps([{'pattern': '点声', 'replacement': '点声DotVoice'}]), encoding='utf-8')
            self.assertEqual(OpenLessSettings(folder).correct('打开点生'), '打开点声DotVoice')

if __name__ == '__main__':
    unittest.main()
