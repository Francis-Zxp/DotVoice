"""Online transcript policy and migration away from local ASR. No real model or device."""
import builtins
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge.controller import Controller
from bridge.prefs import Preferences

try:
    from .test_bridge import FakeBackend, FakeCapture, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, wait_for
    from core_fakes import make_core


class CoreLiveTextCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.events = []

    def core(self, backend=None):
        core = make_core(self.base, backend or FakeBackend(), events=self.events)
        self.addCleanup(core.close)
        return core

    def record(self, core):
        control = core.controller
        control.command('arm')
        wait_for(lambda: control.phase == 'idle')
        control.command('start')
        wait_for(lambda: control.phase == 'recording')
        return control


class LocalAsrRemovalTests(CoreLiveTextCase):
    def test_saved_local_switches_do_not_load_models_or_bypass_openless_raw(self):
        runtime = self.base / 'runtime'
        runtime.mkdir()
        path = runtime / 'prefs.json'
        path.write_text(json.dumps({'preview': {'enabled': True, 'show_local_text': True},
                                    'voice': {'fast_raw': True, 'whisper': 'off'},
                                    'appearance': {'theme': 'glass'},
                                    'overlay': {'monitor': 'panel', 'position': {'x': -120, 'y': 45}}}),
                        encoding='utf-8')
        # A real old OpenLess raw-mode preference must no longer select a local branch.
        appdata = self.base / 'appdata'
        (appdata / 'OpenLess').mkdir(parents=True)
        (appdata / 'OpenLess' / 'preferences.json').write_text('{"defaultMode":"raw"}', encoding='utf-8')
        original_import, local_imports = builtins.__import__, []

        def without_local(name, *args, **kwargs):
            if name.startswith(('sherpa_onnx', 'pypinyin')) or name in ('bridge.preview', 'preview'):
                local_imports.append(name)
                raise ImportError('local ASR is not installed')
            return original_import(name, *args, **kwargs)

        backend = FakeBackend()
        with patch.dict(os.environ, {'APPDATA': str(appdata)}), patch('builtins.__import__', side_effect=without_local):
            core = self.core(backend)
            control = self.record(core)
            pcm = b'\x04\x00' * 800
            control._audio(pcm)
            wait_for(lambda: ('pcm', pcm) in backend.calls)
            self.assertEqual(backend.calls.count('start'), 1)
            self.assertTrue(backend.insert_text, 'OpenLess remains the only insertion owner')
            self.assertEqual(core.live_text, '', 'without backend partials only the waveform is shown')
            control.command('stop')
            wait_for(lambda: control.phase == 'processing')
            self.assertEqual(backend.calls.count('stop'), 1)
            backend.incoming.put({'type': 'result', 'text': 'OpenLess 的原文'})
            wait_for(lambda: control.phase == 'idle')
            self.assertEqual(core.result['text'], 'OpenLess 的原文')
            core.close()
        self.assertEqual(local_imports, [])
        prefs = core.prefs.snapshot()
        self.assertNotIn('preview', prefs)
        self.assertEqual(prefs['voice'], {'whisper': 'off'})
        self.assertEqual(prefs['appearance']['theme'], 'glass')
        self.assertEqual(prefs['overlay']['position'], {'x': -120, 'y': 45})
        self.assertEqual(prefs['overlay']['monitor'], 'panel')
        core.prefs.save()
        saved = Preferences(path).snapshot()
        self.assertNotIn('preview', saved)
        self.assertNotIn('fast_raw', saved['voice'])
        self.assertEqual(saved['voice']['whisper'], 'off')

    def test_retired_switches_are_not_accepted_as_new_settings(self):
        core = self.core()
        for patch_value in ({'voice': {'fast_raw': True}}, {'preview': {'show_local_text': True}}):
            self.assertIn('error', core.prefs_set(patch_value))
        self.assertEqual(core.prefs.get('voice'), {'whisper': 'auto'})


class PartialSourceTests(CoreLiveTextCase):
    def test_openless_words_keep_source_and_capability_detection(self):
        backend = FakeBackend()
        core = self.core(backend)
        self.record(core)
        backend.incoming.put({'type': 'partial', 'text': '在线的字', 'offset': 0, 'final': False})
        wait_for(lambda: core.live_text == '在线的字')
        self.assertEqual(core.live_source, 'remote')
        self.assertEqual(core.upstream.state.get('status'), 'supported')

    def test_a_new_recording_clears_the_source(self):
        backend = FakeBackend()
        core = self.core(backend)
        control = self.record(core)
        backend.incoming.put({'type': 'partial', 'text': '上一段', 'offset': 0})
        wait_for(lambda: core.live_source == 'remote')
        control.command('cancel')
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
    def test_retired_keys_and_invalid_values_leave_other_settings_intact(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            path.write_text(json.dumps({'version': 1, 'preview': {'enabled': True, 'show_local_text': True},
                                        'voice': {'fast_raw': True, 'whisper': 'on'},
                                        'sounds': {'bogus': 1, 'enabled': False, 'volume': 'deafening'}}),
                            encoding='utf-8')
            prefs = Preferences(path)
            self.assertEqual(prefs.warning, '')
            self.assertNotIn('preview', prefs.snapshot())
            self.assertEqual(prefs.get('voice'), {'whisper': 'on'})
            self.assertFalse(prefs.get('sounds', 'enabled'))
            self.assertEqual(prefs.get('sounds', 'volume'), 'medium')

    def test_a_settings_file_that_is_not_an_object_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            path.write_text('[]', encoding='utf-8')
            prefs = Preferences(path)
            self.assertTrue(prefs.warning)
            self.assertEqual(prefs.path.name, 'prefs-recovered.json')
            self.assertEqual(prefs.get('voice'), {'whisper': 'auto'})
            self.assertNotIn('preview', prefs.snapshot())


if __name__ == '__main__':
    unittest.main()
