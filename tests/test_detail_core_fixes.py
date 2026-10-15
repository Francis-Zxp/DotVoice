"""Core review fixes: unfinished setup survives restarts and failed notes get an error cue.
No HID, microphone, model or network is touched."""
from pathlib import Path
import tempfile
import unittest

from bridge import secrets

try:
    from .core_fakes import make_core
except ImportError:
    from core_fakes import make_core


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




if __name__ == '__main__':
    unittest.main()
