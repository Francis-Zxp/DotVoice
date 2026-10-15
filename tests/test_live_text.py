"""Live text policy: every partial says where it came from ('local' preview, 'remote'
OpenLess, 'cloud' built-in engine) so the UI shows words from accurate sources only, and
the local preview models run only while something uses them (preview.show_local_text or
voice.fast_raw). No model, microphone or hook is touched."""
import json
from pathlib import Path
import tempfile
import unittest

from bridge.controller import Controller
from bridge.prefs import Preferences

try:
    from .test_bridge import FakeBackend, FakeCapture, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, wait_for
    from core_fakes import make_core


class RecordingPreview:
    """Stands in for LivePreview: records construction, warm-up and use."""
    def __init__(self, emit):
        self.emit, self.calls, self.session = emit, [], None

    def warm(self):
        self.calls.append('warm')

    def start(self, session):
        self.calls.append('start')
        self.session = session

    def feed(self, pcm):
        self.calls.append('feed')
        self.emit('partial', {'session': self.session, 'text': '本机猜的字', 'final': False})

    def finish(self):
        self.calls.append('finish')

    def cancel(self):
        self.calls.append('cancel')

    def close(self):
        self.calls.append('close')


class CoreLiveTextCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.events, self.previews = [], []

    def core(self, backend=None):
        core = make_core(self.base, backend or FakeBackend(), events=self.events,
                         preview_factory=lambda emit: self.previews.append(RecordingPreview(emit)) or self.previews[-1])
        self.addCleanup(core.close)
        return core

    def partials(self):
        return [value for kind, value in self.events if kind == 'partial']

    def record(self, core):
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        return control


class PreviewLifecycleTests(CoreLiveTextCase):
    def test_local_models_do_not_run_while_neither_switch_wants_them(self):
        core = self.core()  # defaults: show_local_text off, fast_raw off
        self.assertEqual(self.previews, [], 'not created, so nothing is loaded or warmed')
        self.assertIsNone(core.controller.preview)
        snapshot = core.snapshot()
        self.assertTrue(snapshot['preview_available'], 'the switch can still be turned on')
        self.assertFalse(snapshot['prefs']['preview']['show_local_text'])
        control = self.record(core)
        control._audio(b'\x01\x00' * 800)
        control.command('stop')
        wait_for(lambda: control.phase == 'processing')
        self.assertEqual(self.partials(), [], 'no local words were even made')

    def test_either_switch_starts_and_warms_them_and_both_off_stops_them(self):
        core = self.core()
        core.prefs_set({'preview': {'show_local_text': True}})
        self.assertEqual(len(self.previews), 1)
        self.assertEqual(self.previews[0].calls, ['warm'], 'loaded ahead of the first recording')
        self.assertIs(core.controller.preview, self.previews[0])
        core.prefs_set({'voice': {'fast_raw': True}})
        self.assertEqual(len(self.previews), 1, 'already running')
        core.prefs_set({'preview': {'show_local_text': False}})
        self.assertIs(core.controller.preview, self.previews[0], '极速落字 still needs the models')
        core.prefs_set({'voice': {'fast_raw': False}})
        self.assertIsNone(core.controller.preview)
        self.assertIsNone(core.preview)
        self.assertEqual(self.previews[0].calls[-1], 'close', 'its worker ends and frees the models')
        core.prefs_set({'voice': {'fast_raw': True}})
        self.assertEqual(len(self.previews), 2, 'started again on request')
        self.assertEqual(self.previews[1].calls, ['warm'])
        self.assertIs(core.controller.preview, self.previews[1])

    def test_a_saved_switch_starts_them_with_the_core(self):
        runtime = self.base / 'runtime'
        runtime.mkdir(parents=True)
        (runtime / 'prefs.json').write_text(json.dumps({'voice': {'whisper': 'auto', 'fast_raw': True}}),
                                            encoding='utf-8')
        core = self.core()
        self.assertEqual(len(self.previews), 1)
        self.assertEqual(self.previews[0].calls, ['warm'])
        self.assertIs(core.controller.preview, self.previews[0])


class PartialSourceTests(CoreLiveTextCase):
    def test_local_preview_words_are_tagged_local(self):
        core = self.core()
        core.prefs_set({'preview': {'show_local_text': True}})
        control = self.record(core)
        control._audio(b'\x01\x00' * 800)
        wait_for(lambda: any(p['text'] == '本机猜的字' for p in self.partials()))
        self.assertEqual({p['source'] for p in self.partials()}, {'local'})
        self.assertEqual((core.snapshot()['live_text'], core.snapshot()['live_source']), ('本机猜的字', 'local'))

    def test_openless_words_are_remote_and_late_local_guesses_never_replace_them(self):
        backend = FakeBackend()
        core = self.core(backend)
        core.prefs_set({'preview': {'show_local_text': True}})
        control = self.record(core)
        backend.incoming.put({'type': 'partial', 'text': '在线的字', 'offset': 0, 'final': False})
        wait_for(lambda: any(p['text'] == '在线的字' for p in self.partials()))  # emitted after the state is set
        self.assertEqual(core.live_source, 'remote')
        self.assertEqual(self.partials()[-1]['source'], 'remote')
        self.assertEqual(core.upstream.state.get('status'), 'supported', 'OpenLess streams: no reminder needed')
        count = len(self.partials())
        # A local guess the preview worker had queued before it was cancelled.
        core._preview_event('partial', {'session': control.session_id, 'text': '迟到的本机字', 'final': False})
        self.assertEqual((core.live_text, core.live_source), ('在线的字', 'remote'))
        self.assertEqual(len(self.partials()), count)

    def test_a_new_recording_clears_the_source(self):
        core = self.core()
        core.prefs_set({'preview': {'show_local_text': True}})
        control = self.record(core)
        control._audio(b'\x01\x00' * 800)
        wait_for(lambda: core.live_source == 'local')
        control.command('cancel')  # stays armed: back to idle on its own
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        self.assertEqual((core.live_text, core.live_source), ('', None))


class CloudBackend(FakeBackend):
    partial_source = 'cloud'  # how a built-in engine (bridge.engines) can mark its live text


class ControllerSourceTests(unittest.TestCase):
    def sources(self, backend, *events):
        notified = []
        control = Controller(lambda *value: notified.append(value), backend, FakeCapture)
        self.addCleanup(control.close)
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        for item in events:
            backend.incoming.put(item)
        wait_for(lambda: sum(1 for kind, _ in notified if kind == 'partial') == len(events))
        return [value['source'] for kind, value in notified if kind == 'partial']

    def test_backend_partials_are_remote_unless_a_cloud_engine_says_cloud(self):
        partial = {'type': 'partial', 'text': '字', 'offset': 0}
        self.assertEqual(self.sources(FakeBackend(), partial), ['remote'])
        self.assertEqual(self.sources(CloudBackend(), partial), ['cloud'])
        self.assertEqual(self.sources(FakeBackend(), {**partial, 'source': 'cloud'},
                                      {**partial, 'source': 'local'}, {**partial, 'source': 7}),
                         ['cloud', 'remote', 'remote'], 'a backend cannot pass its words off as local')

    def test_cloud_words_do_not_count_as_openless_live_text(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        backend, events = CloudBackend(), []
        core = make_core(Path(folder.name), backend, events=events)
        self.addCleanup(core.close)
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        backend.incoming.put({'type': 'partial', 'text': '云端的字', 'offset': 0})
        wait_for(lambda: ('partial', {'session': control.session_id, 'text': '云端的字', 'final': False,
                                      'source': 'cloud'}) in events)
        self.assertEqual((core.live_text, core.live_source), ('云端的字', 'cloud'))
        self.assertNotEqual(core.upstream.state.get('status'), 'supported')


class LiveTextPrefsTests(unittest.TestCase):
    def test_show_local_text_is_off_by_default_and_0_10s_preview_switch_is_skipped(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            path.write_text(json.dumps({'version': 1, 'preview': {'enabled': True},
                                        'sounds': {'bogus': 1, 'enabled': False, 'volume': 'deafening'}}),
                            encoding='utf-8')
            prefs = Preferences(path)
            self.assertEqual(prefs.warning, '')
            self.assertEqual(prefs.get('preview'), {'show_local_text': False}, 'not migrated: the old default was on')
            self.assertFalse(prefs.get('sounds', 'enabled'), 'a neighbour of an unknown key survives')
            self.assertEqual(prefs.get('sounds', 'volume'), 'medium', 'an invalid value falls back alone')
            prefs.update({'preview': {'show_local_text': True}})
            self.assertTrue(Preferences(path).get('preview', 'show_local_text'))
            with self.assertRaises(ValueError):
                prefs.update({'preview': {'show_local_text': 'yes'}})

    def test_a_settings_file_that_is_not_an_object_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            path.write_text('[]', encoding='utf-8')
            prefs = Preferences(path)
            self.assertTrue(prefs.warning)
            self.assertEqual(prefs.path.name, 'prefs-recovered.json', 'the original file is kept')
            self.assertFalse(prefs.get('preview', 'show_local_text'))


if __name__ == '__main__':
    unittest.main()
