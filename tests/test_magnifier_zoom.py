"""Magnifier zoom: prefs range, the pen's volume keys zoom while the lens is held."""
from pathlib import Path
import sys
import tempfile
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge import core as core_module  # noqa: E402
from bridge.airmouse import AirMouse  # noqa: E402
from bridge.prefs import Preferences as Prefs  # noqa: E402


class ZoomPrefsTests(unittest.TestCase):
    def test_default_and_range(self):
        with tempfile.TemporaryDirectory() as folder:
            prefs = Prefs(Path(folder) / 'prefs.json')
            self.assertEqual(prefs.get('present', 'zoom'), 200)
            prefs.update({'present': {'zoom': 300}})
            self.assertEqual(prefs.get('present', 'zoom'), 300)
            for bad in (100, 500, 'x'):
                with self.assertRaises(ValueError):
                    prefs.update({'present': {'zoom': bad}})


class StepZoomTests(unittest.TestCase):
    def fake(self, zoom, pointer_down=True, tool='magnifier'):
        events, saved = [], []
        with tempfile.TemporaryDirectory() as folder:
            prefs = Prefs(Path(folder) / 'prefs.json')
        prefs.update({'present': {'zoom': zoom}}, persist=False)
        core = types.SimpleNamespace(prefs=prefs, emit=lambda kind, data: events.append((kind, data)),
                                     _save_prefs=lambda: saved.append(1), pointer_down=pointer_down, tool=tool,
                                     submitted=[])
        core._zoom = lambda: core_module.Core._zoom(core)
        core._submit = lambda fn, *args: core.submitted.append((fn, args))
        core._step_zoom = lambda step: core_module.Core._step_zoom(core, step)
        return core, events, saved

    def test_steps_through_levels_and_stops_at_ends(self):
        core, events, _ = self.fake(200)
        core_module.Core._step_zoom(core, 1)
        self.assertEqual(core.prefs.get('present', 'zoom'), 250)
        self.assertIn(('magnifier', {'zoom': 250}), events)
        core_module.Core._step_zoom(core, -1)
        core_module.Core._step_zoom(core, -1)
        self.assertEqual(core.prefs.get('present', 'zoom'), 150)
        core, events, saved = self.fake(400)
        core_module.Core._step_zoom(core, 1)
        self.assertEqual((events, saved), ([], []))  # already at the top

    def test_volume_keys_zoom_only_while_the_lens_is_held(self):
        core, _, _ = self.fake(200)
        self.assertTrue(core_module.Core._magnifier_wheel(core, 'wheel_up'))
        self.assertEqual(core.submitted[0][1], (1,))
        for down, tool in ((False, 'magnifier'), (True, 'laser')):
            core, _, _ = self.fake(200, pointer_down=down, tool=tool)
            self.assertFalse(core_module.Core._magnifier_wheel(core, 'wheel_down'))


class WheelHookTests(unittest.TestCase):
    def mouse(self):
        sent = []
        air = AirMouse(lambda: {'enabled': True, 'skip_slideshow': True},
                       lambda: {}, sender=lambda action, down, ctrl: sent.append(action))
        return air, sent

    def test_hook_takes_the_scroll_or_falls_back(self):
        air, sent = self.mouse()
        air._wheel('wheel_up')
        self.assertEqual(sent, ['wheel_up'])
        air.on_wheel = lambda role: True
        air._wheel('wheel_down')
        self.assertEqual(sent, ['wheel_up'])
        air.on_wheel = lambda role: (_ for _ in ()).throw(RuntimeError('boom'))
        air._wheel('wheel_down')
        self.assertEqual(sent, ['wheel_up', 'wheel_down'])


if __name__ == '__main__':
    unittest.main()
