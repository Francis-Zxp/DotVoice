"""Theme preferences survive persistence without resetting device settings."""
from pathlib import Path
import tempfile
import unittest
from bridge.prefs import Preferences


class AppearanceTests(unittest.TestCase):
    def test_glass_round_trip_preserves_other_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            prefs = Preferences(path)
            before = prefs.update({'mode': 'notes', 'appearance': {'reduced_motion': True}})
            prefs.update({'appearance': {'theme': 'glass'}})
            restored = Preferences(path).snapshot()
            self.assertEqual(restored['appearance'], {'theme': 'glass', 'reduced_motion': True,
                                                      'live_shortcut': True})
            self.assertEqual(restored['mode'], 'notes')
            for key in before.keys() - {'appearance'}:
                self.assertEqual(restored[key], before[key])
