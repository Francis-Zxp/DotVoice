"""Whole-recording notes, tags, trash and backend live transcript wiring."""
from pathlib import Path
import os
import tempfile
import time
import unittest

from bridge.notes import NoteStore

try:
    from .test_bridge import FakeBackend, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, wait_for
    from core_fakes import make_core


class NoteStoreTests(unittest.TestCase):
    def test_tags_legacy_category_and_trash_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            store = NoteStore(folder)
            note = store.add('嗯，第一句。第二句。', [], 12.34)
            self.assertEqual(note['tags'], [])
            self.assertEqual(note['duration'], 12.3)
            self.assertEqual(note['text'], '第一句。\n第二句。')
            store.set_tags(note['id'], ['论文', ' 论文 ', '实验'])
            self.assertEqual(store.list()['items'][0]['tags'], ['论文', '实验'])
            self.assertEqual(store.list()['tags'], ['实验', '论文'])
            legacy = Path(folder) / ('b' * 32 + '.json')
            legacy.write_text('{"id":"' + 'b' * 32 + '","created":1,"category":"paper","title":"t","source":"s","text":"s"}',
                              encoding='utf-8')
            self.assertEqual(next(n for n in store.list()['items'] if n['id'] == 'b' * 32)['tags'], ['论文'])
            store.delete(note['id'])
            self.assertNotIn(note['id'], [n['id'] for n in store.list()['items']])
            self.assertEqual(store.restore(note['id'])['id'], note['id'])
            store.delete(note['id'])
            trashed = Path(folder) / '.trash' / (note['id'] + '.json')
            old = time.time() - 40 * 86400
            os.utime(trashed, (old, old))
            self.assertEqual(store.purge_trash(), 1)
            for bad in (None, 5, '../x'):
                with self.assertRaises(ValueError):
                    store.update(bad, 'x')
                with self.assertRaises(ValueError):
                    store.delete(bad)
            with self.assertRaises(ValueError):
                store.set_tags('b' * 32, [str(i) for i in range(9)])


class CoreNoteTests(unittest.TestCase):
    def test_one_note_per_recording_you_end_with_backend_live_text(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events, sounds = [], []
        backend = FakeBackend()
        core = make_core(Path(folder.name), backend, events=events, sounds=sounds)
        self.addCleanup(core.close)
        control = core.controller
        core.set_mode('notes')
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        self.assertFalse(backend.insert_text)
        control._audio(b'\x01\x00' * 800)
        backend.incoming.put({'type': 'partial', 'text': '今天天气', 'offset': 0})
        wait_for(lambda: core.live_text == '今天天气')
        time.sleep(.3)  # a pause no longer ends the note
        self.assertEqual(backend.calls.count('stop'), 0)
        control.command('stop')
        wait_for(lambda: control.phase == 'processing')
        backend.incoming.put({'type': 'result', 'text': '今天天气不错。我想去打球。'})
        wait_for(lambda: control.phase == 'idle')
        notes = core.notes()['items']
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['source'], '今天天气不错。我想去打球。')
        self.assertEqual(notes[0]['tags'], [])
        self.assertEqual(backend.calls.count('start'), 1, 'one OpenLess session per recording')
        self.assertIn('note_saved', [k for k, _ in events])
        self.assertIn('done_notes', sounds)
        self.assertTrue(core.set_note_tags(notes[0]['id'], ['灵感'])['note']['tags'] == ['灵感'])

    def test_openless_live_text_applies_offsets_and_ignores_malformed_updates(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events = []
        backend = FakeBackend()
        core = make_core(Path(folder.name), backend, events=events)
        self.addCleanup(core.close)
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        backend.incoming.put({'type': 'partial', 'text': '今天天', 'offset': 0, 'final': False})
        backend.incoming.put({'type': 'partial', 'text': '天气很好', 'offset': 2, 'final': False})
        wait_for(lambda: core.live_text == '今天天气很好')
        control._audio(b'\x01\x00' * 800)
        time.sleep(.2)
        partials = [v for k, v in events if k == 'partial' and v.get('source') == 'remote']
        self.assertEqual(partials[-1]['text'], '今天天气很好')
        backend.incoming.put({'type': 'partial', 'text': None, 'offset': -1})  # malformed: ignored
        control.command('stop')
        wait_for(lambda: control.phase == 'processing')
        self.assertEqual(core.live_text, '今天天气很好')
        self.assertEqual(backend.calls.count('stop'), 1)


    def test_silence_is_cancelled_instead_of_sent_and_empty_replies_are_not_saved(self):
        from bridge.core import _empty_transcript
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events = []
        backend = FakeBackend()
        core = make_core(Path(folder.name), backend, events=events)
        self.addCleanup(core.close)
        control = core.controller
        core.set_mode('notes')
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for _ in range(10):
            control._audio(b'\x00\x00' * 1600)  # 1 s of silence
        time.sleep(.2)
        control.command('stop')
        wait_for(lambda: any(k == 'empty' for k, _ in events))
        wait_for(lambda: control.phase == 'idle')
        self.assertNotIn('stop', backend.calls, 'nothing is sent to OpenLess')
        self.assertIn('cancel', backend.calls)
        self.assertEqual(core.notes()['items'], [])

        reply = '收到。\n音频转写内容为空（仅包含句号），无实质内容可整理。\n如果需要处理语音转写，请提供实际的音频转写文本。'
        self.assertTrue(_empty_transcript({'text': reply, 'speech_seconds': 0.3}))
        self.assertTrue(_empty_transcript({'text': '。'}))
        self.assertFalse(_empty_transcript({'text': '明天讨论转写内容为空时的处理方案', 'speech_seconds': 4.0}))
        self.assertFalse(_empty_transcript({'text': '今天天气不错。', 'speech_seconds': 0.2}))


if __name__ == '__main__':
    unittest.main()
