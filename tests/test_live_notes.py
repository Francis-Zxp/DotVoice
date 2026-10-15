"""Whole-recording notes, tags, trash and the live preview wiring."""
from pathlib import Path
import os
import tempfile
import time
import unittest

from bridge.notes import NoteStore
from bridge.preview import LivePreview, find_model

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


class FakePreview:
    def __init__(self, emit):
        self.emit, self.calls = emit, []

    def start(self, session):
        self.calls.append('start')
        self.emit('partial', {'session': session, 'text': '', 'final': False})

    def feed(self, pcm):
        self.calls.append('feed')
        self.emit('partial', {'session': 's', 'text': '今天天气', 'final': False})

    def finish(self):
        self.calls.append('finish')

    def cancel(self):
        self.calls.append('cancel')

    def close(self):
        pass


class CoreNoteTests(unittest.TestCase):
    def test_one_note_per_recording_you_end_with_live_preview(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events, sounds = [], []
        backend = FakeBackend()
        previews = []
        core = make_core(Path(folder.name), backend, events=events, sounds=sounds,
                         preview_factory=lambda emit: previews.append(FakePreview(emit)) or previews[-1])
        self.addCleanup(core.close)
        core.prefs_set({'preview': {'show_local_text': True}})  # the local preview runs on request
        control = core.controller
        core.set_mode('notes')
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        self.assertFalse(backend.insert_text)
        control._audio(b'\x01\x00' * 800)
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
        self.assertEqual(previews[0].calls[0], 'start')
        self.assertIn('finish', previews[0].calls)
        self.assertEqual(backend.calls.count('start'), 1, 'one OpenLess session per recording')
        self.assertIn('note_saved', [k for k, _ in events])
        self.assertIn('done_notes', sounds)
        self.assertTrue(core.set_note_tags(notes[0]['id'], ['灵感'])['note']['tags'] == ['灵感'])

    def test_preview_off_and_cancel_releases_preview(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        previews = []
        core = make_core(Path(folder.name), preview_factory=lambda emit: previews.append(FakePreview(emit)) or previews[-1])
        self.addCleanup(core.close)
        core.prefs_set({'preview': {'show_local_text': True}})
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        control.command('cancel')
        wait_for(lambda: 'cancel' in previews[0].calls)
        core.prefs_set({'preview': {'show_local_text': False}})
        self.assertIsNone(control.preview)
        self.assertIsNone(core.preview)


    def test_openless_live_text_replaces_the_local_preview(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        events, previews = [], []
        backend = FakeBackend()
        core = make_core(Path(folder.name), backend, events=events,
                         preview_factory=lambda emit: previews.append(FakePreview(emit)) or previews[-1])
        self.addCleanup(core.close)
        core.prefs_set({'preview': {'show_local_text': True}})
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        backend.incoming.put({'type': 'partial', 'text': '今天天', 'offset': 0, 'final': False})
        backend.incoming.put({'type': 'partial', 'text': '天气很好', 'offset': 2, 'final': False})
        wait_for(lambda: core.live_text == '今天天气很好')
        self.assertIn('cancel', previews[0].calls, 'the local preview stops once OpenLess streams')
        feeds = previews[0].calls.count('feed')
        control._audio(b'\x01\x00' * 800)
        time.sleep(.2)
        self.assertEqual(previews[0].calls.count('feed'), feeds)
        partials = [v for k, v in events if k == 'partial' and v.get('source') == 'remote']
        self.assertEqual(partials[-1]['text'], '今天天气很好')
        backend.incoming.put({'type': 'partial', 'text': None, 'offset': -1})  # malformed: ignored
        control.command('stop')
        wait_for(lambda: control.phase == 'processing')
        self.assertNotIn('finish', previews[0].calls)


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


class LivePreviewTests(unittest.TestCase):
    def test_worker_emits_partials_and_final_with_a_fake_recognizer(self):
        class Stream:
            def __init__(self):
                self.n = 0
            def accept_waveform(self, rate, samples):
                self.n += len(samples)
            def input_finished(self):
                pass

        class Recognizer:
            def create_stream(self):
                return Stream()
            def is_ready(self, stream):
                return False
            def get_result(self, stream):
                return '字' * (stream.n // 800)

        events = []
        preview = LivePreview('unused', lambda k, v: events.append((k, v)), recognizer_factory=Recognizer)
        preview.start('s1')
        for _ in range(3):
            preview.feed(b'\x00\x01' * 800)
            time.sleep(.12)
        preview.finish()
        wait_for(lambda: any(v.get('final') for _, v in events))
        preview.close()
        texts = [v['text'] for k, v in events if k == 'partial']
        self.assertEqual(texts[-1], '字字字')
        self.assertTrue(all(v['session'] == 's1' for _, v in events))

    def test_two_pass_refines_the_sentence_and_commits_at_pauses(self):
        class Stream:
            def __init__(self):
                self.n = 0
                self.end = False
            def accept_waveform(self, rate, samples):
                self.n += len(samples)
            def input_finished(self):
                pass

        class Online:  # a sloppy streaming guess: one wrong char per 800 samples
            def create_stream(self):
                return Stream()
            def is_ready(self, stream):
                return False
            def get_result(self, stream):
                return '错' * (stream.n // 800)
            def is_endpoint(self, stream):
                return stream.end
            def reset(self, stream):
                stream.n, stream.end = 0, False

        class Result:
            def __init__(self, text):
                self.text = text

        class OfflineStream:
            def accept_waveform(self, rate, samples):
                self.result = Result('对' * (len(samples) // 800))

        class Offline:
            def create_stream(self):
                return OfflineStream()
            def decode_stream(self, stream):
                pass

        events, holder = [], {}
        def online():
            holder['online'] = Online()
            return holder['online']
        preview = LivePreview('unused', lambda k, v: events.append((k, v)), recognizer_factory=online,
                              refiner_factory=Offline)
        preview.start('s1')
        for _ in range(21):  # 21 × 800 samples > 1 s: one refinement
            preview.feed(b'\x00\x01' * 800)
        time.sleep(.3)
        preview.feed(b'\x00\x01' * 800)
        time.sleep(.2)
        partials = [v['text'] for k, v in events if k == 'partial']
        self.assertTrue(partials[-1].startswith('对' * 20), partials[-1])
        preview.finish()
        wait_for(lambda: any(v.get('final') for _, v in events))
        preview.close()
        self.assertEqual(events[-1][1]['text'], '对' * 22, 'the final preview is fully refined')

    def test_refiner_reuses_openless_models_and_prefers_dotvoice_own(self):
        from bridge.preview import find_refiner
        with tempfile.TemporaryDirectory() as program, tempfile.TemporaryDirectory() as openless:
            self.assertIsNone(find_refiner(program, openless))
            sense = Path(openless) / 'sherpa-onnx' / 'sense-voice-small-zh'
            sense.mkdir(parents=True)
            for name in ('model.int8.onnx', 'tokens.txt'):
                (sense / name).write_bytes(b'x')
            self.assertEqual(find_refiner(program, openless), (sense, 'sense_voice'))
            own = Path(program) / 'models' / 'paraformer-zh-small'
            own.mkdir(parents=True)
            for name in ('model.int8.onnx', 'tokens.txt'):
                (own / name).write_bytes(b'x')
            self.assertEqual(find_refiner(program, openless), (own, 'paraformer'))

    def test_bundled_model_is_found(self):
        root = Path(__file__).resolve().parents[1]
        self.assertIsNotNone(find_model(root), 'models/sherpa-zh-14M is missing')


if __name__ == '__main__':
    unittest.main()
