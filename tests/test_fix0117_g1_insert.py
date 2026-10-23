"""Review 0.11.7 (group 1): typing refusals are marked on the result, an empty custom polish
falls back to the transcript, and a busy secrets.json is never overwritten.

Everything runs on fakes: nothing is typed, no model is called, DPAPI is replaced."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import secrets
from bridge.elevation import SETTING, kept_message
from bridge.engines.cloud import CloudBackend
from bridge.engines.llm import PolishError, Polisher
from bridge.fast_insert import PartlyTyped

try:
    from .core_fakes import make_core
    from .test_bridge import FakeBackend
    from .test_elevation import Desk
    from .test_engines import FakeASR, drain
except ImportError:
    from core_fakes import make_core
    from test_bridge import FakeBackend
    from test_elevation import Desk
    from test_engines import FakeASR, drain


def dictate(backend):
    backend.connect()
    backend.start()
    backend.feed(bytes(3200))
    backend.stop()
    return drain(backend)


class TypingRefusedTests(unittest.TestCase):
    """DotVoice elevated (the user's setup): guard_typer never raises ElevatedTarget, so a lock
    screen or UAC prompt surfaces as a plain OSError / PartlyTyped. It used to look like success."""

    def refused(self, error):
        def typer(text):
            raise error
        backend = CloudBackend(FakeASR(final='你好世界'), inserter=typer)
        self.addCleanup(backend.close)
        return dictate(backend)

    def test_plain_refusal_is_marked_not_typed_without_a_separate_notice(self):
        events = self.refused(OSError('SendInput refused'))
        self.assertEqual(events[-1]['not_typed'], 'refused')
        self.assertEqual(events[-1]['text'], '你好世界', 'the text is kept')
        self.assertEqual([e for e in events if e.get('kind') == 'notice'], [])

    def test_partly_typed_is_marked_partial(self):
        events = self.refused(PartlyTyped('refused after 40 strokes'))
        self.assertEqual(events[-1]['not_typed'], 'partial')
        self.assertEqual([e for e in events if e.get('kind') == 'notice'], [])

    def test_a_typer_bug_is_marked_refused(self):
        self.assertEqual(self.refused(RuntimeError('bug'))[-1]['not_typed'], 'refused')

    def test_elevated_dotvoice_behind_guard_typer(self):
        def lock_screen(text):
            raise OSError('lock screen')
        guarded = Desk().watch(own_elevated=True).guard_typer(lock_screen)
        backend = CloudBackend(FakeASR(final='你好'), inserter=guarded)
        self.addCleanup(backend.close)
        self.assertEqual(dictate(backend)[-1]['not_typed'], 'refused')

    def test_typed_text_carries_no_flag(self):
        typed = []
        backend = CloudBackend(FakeASR(final='你好'), inserter=typed.append)
        self.addCleanup(backend.close)
        self.assertNotIn('not_typed', dictate(backend)[-1])
        self.assertEqual(typed, ['你好'])

    def test_core_keeps_refused_text_on_the_clipboard_without_the_success_chime(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events, sounds = [], []
        core = make_core(Path(folder.name), FakeBackend(), events=events, sounds=sounds)
        self.addCleanup(core.close)
        for reason in ('refused', 'partial'):
            core.notify('result', {'text': '你好世界', 'destination': 'input', 'not_typed': reason})
            self.assertEqual(core.copied[-1], '你好世界')
            self.assertEqual(sounds[-1], 'error')
            self.assertTrue([value for kind, value in events if kind == 'result'][-1]['copied'])


class KeptMessageReasonTests(unittest.TestCase):
    def test_refusals_do_not_blame_an_administrator_window(self):
        for reason, lead in (('refused', '文字没能输入'), ('partial', '部分文字没能输入')):
            message = kept_message(True, reason=reason)
            self.assertTrue(message.startswith(lead), message)
            self.assertIn('锁屏或 UAC', message)
            self.assertIn('按 Ctrl+V 粘贴', message)
            self.assertNotIn('管理员', message)
            self.assertNotIn(SETTING, message)
        self.assertIn('本次结果里', kept_message(False, reason='refused'))

    def test_elevated_wording_is_unchanged(self):
        self.assertEqual(kept_message(True), kept_message(True, reason='elevated'))
        self.assertIn(SETTING, kept_message(True, reason='elevated'))


class EmptyCustomPolishTests(unittest.TestCase):
    def polisher(self):
        return Polisher('https://example.com/v1', 'k', 'm', style='custom', prompt='整理成要点')

    def test_empty_or_think_only_reply_fails_instead_of_dropping_the_text(self):
        polisher = self.polisher()
        for reply in ('', '<think>先想一想……', '```\n\n```'):
            with self.subTest(reply=reply), patch.object(polisher, '_complete', return_value=reply):
                with self.assertRaisesRegex(PolishError, '为空'):
                    polisher.polish('我们明天下午三点开会')

    def test_filler_only_speech_may_still_come_back_empty(self):
        polisher = self.polisher()
        with patch.object(polisher, '_complete', return_value=''):
            self.assertEqual(polisher.polish('嗯，那个……'), '')

    def test_custom_results_may_still_be_longer_than_the_source(self):
        polisher = self.polisher()
        longer = '- 明天下午三点开会\n- 地点：三楼会议室\n- 参会：产品、研发、测试全体成员'
        with patch.object(polisher, '_complete', return_value=longer):
            self.assertEqual(polisher.polish('明天三点三楼开会大家都来'), longer)

    def test_cloud_types_the_transcript_and_says_so(self):
        polisher = self.polisher()
        typed = []
        backend = CloudBackend(FakeASR(final='我们明天下午三点开会'), polisher, inserter=typed.append)
        self.addCleanup(backend.close)
        with patch.object(polisher, '_complete', return_value='<think>先想一想'):
            events = dictate(backend)
        self.assertEqual(typed, ['我们明天下午三点开会'])
        self.assertEqual(events[-1]['text'], '我们明天下午三点开会')
        self.assertTrue(any('已改用识别原文' in e.get('message', '') for e in events))


def fake_protect(data, entropy=b''):
    return b'P' + bytes(data)


def fake_unprotect(blob, entropy=b''):
    if not blob.startswith(b'P'):
        raise OSError('not ours')
    return bytes(blob[1:])


class BusySecretsFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for name, value in (('protect', fake_protect), ('unprotect', fake_unprotect), ('READ_RETRY', 0)):
            patcher = patch.object(secrets, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.store = secrets.SecretStore(Path(folder.name) / 'secrets.json')
        self.store.save('volc.api_key', 'volc-key-123456')
        self.store.save('llm.api_key', 'llm-key-123456')
        self.failures = 0
        original = Path.read_text
        store_path = self.store.path

        def flaky(path, *args, **kwargs):
            if path == store_path and self.failures:
                self.failures -= 1
                raise PermissionError(32, 'The process cannot access the file because it is being used by another process')
            return original(path, *args, **kwargs)
        patcher = patch.object(Path, 'read_text', flaky)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_brief_lock_is_retried_and_no_key_is_lost(self):
        self.failures = 1
        self.store.save('dashscope.api_key', 'dash-key-123456')
        self.assertEqual(self.store.names(), ['dashscope.api_key', 'llm.api_key', 'volc.api_key'])
        self.assertEqual(self.store.load('volc.api_key'), 'volc-key-123456')
        self.failures = 1
        self.store.delete('llm.api_key')
        self.assertEqual(self.store.names(), ['dashscope.api_key', 'volc.api_key'])

    def test_a_lasting_lock_refuses_to_write_and_leaves_the_file_alone(self):
        before = self.store.path.read_bytes()
        self.failures = 100
        with self.assertRaises(OSError):
            self.store.save('dashscope.api_key', 'dash-key-123456')
        with self.assertRaises(OSError):
            self.store.delete('volc.api_key')
        self.assertIsNone(self.store.load('volc.api_key'), 'lookups stay lenient')
        self.assertFalse(self.store.has('volc.api_key'))
        self.failures = 0
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.load('llm.api_key'), 'llm-key-123456')

    def test_a_damaged_file_may_still_start_over(self):
        self.store.path.write_text('{not json', encoding='utf-8')
        self.store.save('dashscope.api_key', 'dash-key-123456')
        self.assertEqual(self.store.names(), ['dashscope.api_key'])
        self.assertEqual(json.loads(self.store.path.read_text(encoding='utf-8'))['version'], 1)


if __name__ == '__main__':
    unittest.main()
