import json
from pathlib import Path
import tempfile
import unittest

from bridge.prefs import Preferences


class OverlayMonitorPreferencesTests(unittest.TestCase):
    def test_older_settings_keep_manual_position_and_default_to_primary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            path.write_text(json.dumps({'overlay': {'position': {'x': -1800, 'y': 40}}}), encoding='utf-8')
            prefs = Preferences(path)
            self.assertIsNone(prefs.get('overlay', 'monitor'))
            self.assertEqual(prefs.get('overlay', 'position'), {'x': -1800, 'y': 40})

    def test_selection_survives_restart_and_reset_clears_only_manual_position(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            selected = r'\\.\DISPLAY2'
            prefs = Preferences(path)
            prefs.update({'overlay': {'monitor': selected, 'position': {'x': -1600, 'y': 60}}})
            prefs = Preferences(path)
            self.assertEqual(prefs.get('overlay', 'monitor'), selected)
            prefs.update({'overlay': {'position': None}})
            self.assertEqual(Preferences(path).get('overlay', 'monitor'), selected)
            self.assertIsNone(prefs.get('overlay', 'position'))
            prefs.update({'overlay': {'monitor': None}})
            self.assertIsNone(Preferences(path).get('overlay', 'monitor'))

    def test_invalid_monitor_identifiers_do_not_change_saved_preferences(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Preferences(Path(folder) / 'prefs.json')
            for invalid in [False, 42, {}, [], '', '   ', 'bad\x00name', 'a' * 257]:
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    prefs.update({'overlay': {'monitor': invalid}})
                self.assertIsNone(prefs.get('overlay', 'monitor'))


if __name__ == '__main__':
    unittest.main()
