"""Capsule placement schema, per-monitor positions and old-settings migration."""
import json
from pathlib import Path
import tempfile
import unittest

from bridge.prefs import OVERLAY_ANCHORS, Preferences


class OverlayPreferencesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'prefs.json'
        self.prefs = Preferences(self.path)

    def test_defaults_and_legacy_position_are_preserved(self):
        self.path.write_text(json.dumps({'overlay': {'monitor': 'external', 'position': {'x': -1600, 'y': 90},
                                                     'enabled': False}}), encoding='utf-8')
        overlay = Preferences(self.path).get('overlay')
        self.assertFalse(overlay['all_monitors'])
        self.assertEqual(overlay['anchor'], 'top-center')
        self.assertEqual(overlay['positions'], {})
        self.assertEqual(overlay['monitor'], 'external')
        self.assertEqual(overlay['position'], {'x': -1600, 'y': 90})
        self.assertFalse(overlay['enabled'])

    def test_nine_anchors_and_independent_positions_round_trip(self):
        positions = {'external': {'x': -20000, 'y': 0}, 'laptop': {'x': 20000, 'y': -20000}}
        for anchor in OVERLAY_ANCHORS:
            with self.subTest(anchor=anchor):
                self.prefs.update({'overlay': {'all_monitors': True, 'anchor': anchor, 'positions': positions}})
                overlay = Preferences(self.path).get('overlay')
                self.assertTrue(overlay['all_monitors'])
                self.assertEqual(overlay['anchor'], anchor)
                self.assertEqual(overlay['positions'], positions)
        self.prefs.update({'overlay': {'positions': {}}})
        self.assertEqual(Preferences(self.path).get('overlay', 'positions'), {})

    def test_bad_patches_are_atomic_and_do_not_remove_saved_positions(self):
        self.prefs.update({'overlay': {'positions': {'screen': {'x': 3, 'y': 7}}}})
        before = self.path.read_bytes()
        invalid = [
            {'all_monitors': 1}, {'anchor': 'top'}, {'anchor': None},
            {'positions': None}, {'positions': []},
            {'positions': {str(i): {'x': 0, 'y': 0} for i in range(33)}},
            {'positions': {'': {'x': 0, 'y': 0}}},
            {'positions': {None: {'x': 0, 'y': 0}}},
            {'positions': {'screen\n': {'x': 0, 'y': 0}, 'screen': {'x': 1, 'y': 1}}},
            {'positions': {'a\x00b': {'x': 0, 'y': 0}}},
            {'positions': {'x' * 257: {'x': 0, 'y': 0}}},
            {'positions': {'screen': None}},
            {'positions': {'screen': {'x': True, 'y': 0}}},
            {'positions': {'screen': {'x': 0.5, 'y': 0}}},
            {'positions': {'screen': {'x': 20001, 'y': 0}}},
            {'positions': {'screen': {'x': 0}}},
            {'positions': {'screen': {'x': 0, 'y': 0, 'extra': 1}}},
        ]
        for patch in invalid:
            with self.subTest(patch=patch):
                with self.assertRaises(ValueError):
                    self.prefs.update({'overlay': {'enabled': False, **patch}})
                self.assertEqual(self.path.read_bytes(), before)
                self.assertTrue(self.prefs.get('overlay', 'enabled'))

    def test_corrupt_new_fields_do_not_drop_valid_legacy_or_neighbor_fields(self):
        self.path.write_text(json.dumps({'overlay': {'all_monitors': 'yes', 'anchor': 'nowhere',
                                                     'positions': {'bad': None}, 'monitor': 'external',
                                                     'position': {'x': -10, 'y': 20}, 'linger_seconds': 7}}),
                             encoding='utf-8')
        overlay = Preferences(self.path).get('overlay')
        self.assertEqual((overlay['all_monitors'], overlay['anchor'], overlay['positions']),
                         (False, 'top-center', {}))
        self.assertEqual((overlay['monitor'], overlay['position'], overlay['linger_seconds']),
                         ('external', {'x': -10, 'y': 20}, 7))


if __name__ == '__main__':
    unittest.main()
