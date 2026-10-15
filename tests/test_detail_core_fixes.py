"""Core review fixes (2026-10-14): the fresh-install verdict survives an unfinished wizard,
and the local preview models follow the engine (极速落字 is OpenLess only).
No HID, microphone, model or network is touched."""
import json
from pathlib import Path
import tempfile
import unittest

from bridge import secrets

try:
    from .test_bridge import FakeBackend
    from .core_fakes import make_core
    from .test_live_text import RecordingPreview
except ImportError:
    from test_bridge import FakeBackend
    from core_fakes import make_core
    from test_live_text import RecordingPreview


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.runtime = self.base / 'runtime'
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)   # each core points it at its runtime/


class FreshInstallWizardTests(Case):
    def test_a_wizard_left_unfinished_opens_again_on_the_next_launch(self):
        core = make_core(self.base)
        self.assertFalse(core.snapshot()['prefs']['setup']['done'], 'a fresh install')
        core.close()  # quits before choosing an engine or skipping
        self.assertTrue((self.runtime / 'usage.json').exists(), 'quitting still writes usage.json')
        again = make_core(self.base)
        self.addCleanup(again.close)
        self.assertFalse(again.snapshot()['prefs']['setup']['done'], 'the wizard opens again')

    def test_an_earlier_install_is_not_frozen_to_disk_at_start(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / 'usage.json').write_text('{}', encoding='utf-8')   # used before, prefs never written
        core = make_core(self.base)
        self.addCleanup(core.close)
        self.assertTrue(core.snapshot()['prefs']['setup']['done'])
        self.assertFalse((self.runtime / 'prefs.json').exists())


class NoteErrorSoundTests(Case):
    def test_an_unsaved_note_gets_the_error_cue_not_the_saved_chime(self):
        sounds = []
        core = make_core(self.base, sounds=sounds)
        self.addCleanup(core.close)

        def full_disk(*args, **kwargs):
            raise OSError('disk full')
        core.notes_store.add = full_disk
        core.notify('result', {'text': '明天下午开会', 'destination': 'notes'})
        self.assertIn('note_error', core.result)
        self.assertEqual(sounds[-1:], ['error'])


class PreviewFollowsEngineTests(Case):
    def setUp(self):
        super().setUp()
        self.previews, self.built = [], []

    def factory(self, kind, engine):
        backend = FakeBackend()
        self.built.append((kind, backend))
        return backend

    def core(self):
        core = make_core(self.base, FakeBackend(), engine_factory=self.factory,
                         preview_factory=lambda emit: self.previews.append(RecordingPreview(emit)) or self.previews[-1])
        self.addCleanup(core.close)
        return core

    def test_fast_raw_keeps_the_models_only_while_openless_is_the_engine(self):
        core = self.core()
        core.prefs_set({'voice': {'fast_raw': True}})
        self.assertIs(core.controller.preview, self.previews[0])
        core.prefs_set({'engine': {'kind': 'volc'}})
        self.assertIsNone(core.controller.preview, '极速落字 does nothing for 豆包: no models')
        self.assertEqual(self.previews[0].calls[-1], 'close')
        core.prefs_set({'engine': {'kind': 'openless'}})
        self.assertEqual(len(self.previews), 2, 'loaded again for OpenLess')
        self.assertIs(core.controller.preview, self.previews[1])

    def test_show_local_text_still_runs_them_for_any_engine(self):
        core = self.core()
        core.prefs_set({'preview': {'show_local_text': True}, 'voice': {'fast_raw': True}})
        core.prefs_set({'engine': {'kind': 'volc'}})
        self.assertIs(core.controller.preview, self.previews[0])

    def test_a_saved_fast_raw_with_another_engine_loads_nothing_at_start(self):
        self.runtime.mkdir(parents=True)
        (self.runtime / 'prefs.json').write_text(json.dumps({'engine': {'kind': 'volc'}, 'voice': {'fast_raw': True}}),
                                                 encoding='utf-8')
        core = self.core()
        self.assertEqual(core.engine_kind, 'volc')
        self.assertEqual(self.previews, [])
        self.assertIsNone(core.controller.preview)


if __name__ == '__main__':
    unittest.main()
