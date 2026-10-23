"""0.11.7 fix: changing the PV60's hold/toggle mode keeps the usage ledger in memory, so
usage not yet written to usage-pv60.json is not replaced by a stale copy.
No HID, microphone or network is touched."""
import json
from pathlib import Path
import tempfile
import unittest

from bridge import secrets

try:
    from .core_fakes import make_core
except ImportError:
    from core_fakes import make_core


class SelectDeviceLedgerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.runtime = self.base / 'runtime'
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)   # each core points it at its runtime/

    def test_pv60_mode_change_keeps_unsaved_usage(self):
        core = make_core(self.base)
        self.addCleanup(core.close)
        self.assertIn('selected', core.select_device('pv60', 'hold'))
        ledger = core.ledger
        core.ledger.completed()          # a finished session, not yet written
        core.ledger.audio({'audio_seconds': 12.0, 'speech_seconds': 9.0})
        self.assertIn('selected', core.select_device('pv60', 'toggle'))
        self.assertIs(core.ledger, ledger)
        self.assertEqual(core.ledger.data['totals']['sessions'], 1)
        self.assertEqual(core.mode, 'toggle')
        core.close()
        saved = json.loads((self.runtime / 'usage-pv60.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['totals']['sessions'], 1)
        self.assertEqual(saved['totals']['audio_seconds'], 12.0)

    def test_switching_device_still_uses_each_device_file(self):
        core = make_core(self.base)
        self.addCleanup(core.close)
        baseus = core.ledger
        baseus.completed()
        self.assertIn('selected', core.select_device('pv60', 'hold'))
        self.assertIsNot(core.ledger, baseus)
        self.assertEqual(core.ledger.path.name, 'usage-pv60.json')
        self.assertEqual(core.ledger.data['totals']['sessions'], 0)
        saved = json.loads((self.runtime / 'usage.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['totals']['sessions'], 1, 'the old device ledger was saved on the switch')
        self.assertIn('selected', core.select_device('baseus'))
        self.assertEqual(core.ledger.path.name, 'usage.json')
        self.assertEqual(core.ledger.data['totals']['sessions'], 1)


if __name__ == '__main__':
    unittest.main()
