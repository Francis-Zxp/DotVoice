"""0.11.8 cross-group fixes: the core refuses global shortcuts that would take a key from every
program (as the settings recorder already does), the 续航 page gets the device-name file's
warnings with the usage file's, and a keep-awake stream that failed to open is retried sooner.
Fakes only: no HID, microphone, audio output, network or input injection."""
import json
from pathlib import Path
import tempfile
import unittest

from bridge import secrets
from bridge.prefs import Preferences

try:
    from .core_fakes import make_core
except ImportError:
    from core_fakes import make_core


class Folder(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)


class HotkeyRuleTests(Folder):
    def prefs(self):
        return Preferences(self.base / 'prefs.json')

    def test_bare_keys_tab_and_shift_tab_are_refused(self):
        prefs = self.prefs()
        for value in ('F5', 'f1', 'F12', 'M', 'Tab', 'Shift+Tab', 'shift + tab', 'Ctrl+', '+M'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                prefs.update({'hotkeys': {'toggle_mode': value}})
        self.assertEqual(prefs.get('hotkeys'), {'toggle_mode': '', 'arm_toggle': ''})

    def test_shortcuts_with_a_modifier_and_f13_to_f24_are_kept(self):
        prefs = self.prefs()
        for value in ('Ctrl+Shift+M', 'Alt+3', 'Ctrl+F5', 'Shift+F5', 'Ctrl+Shift+Tab', 'F13', 'F24', ''):
            with self.subTest(value=value):
                self.assertEqual(prefs.update({'hotkeys': {'arm_toggle': value}})['hotkeys']['arm_toggle'], value)
        self.assertEqual(Preferences(self.base / 'prefs.json').get('hotkeys', 'arm_toggle'), '')

    def test_an_old_bare_shortcut_is_dropped_on_load_and_the_rest_kept(self):
        (self.base / 'prefs.json').write_text(json.dumps(
            {'hotkeys': {'toggle_mode': 'F5', 'arm_toggle': 'Ctrl+Alt+V'}, 'sounds': {'volume': 'high'}}),
            encoding='utf-8')
        prefs = self.prefs()
        self.assertEqual(prefs.get('hotkeys'), {'toggle_mode': '', 'arm_toggle': 'Ctrl+Alt+V'})
        self.assertEqual(prefs.get('sounds', 'volume'), 'high')


class CoreCase(Folder):
    def setUp(self):
        super().setUp()
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)
        self.core = make_core(self.base)
        self.addCleanup(self.core.close)


class UsageWarningTests(CoreCase):
    def test_device_name_warnings_reach_the_usage_view(self):
        self.assertEqual(self.core.usage_view()['warning'], '')
        self.core.instances.warning = '设备名称未能保存；重启后可能恢复默认名称。'
        self.assertEqual(self.core.usage_view()['warning'], '设备名称未能保存；重启后可能恢复默认名称。')
        self.core.ledger.warning = '续航记录写入失败，请检查程序目录的写入权限。'
        self.assertEqual(self.core.usage_view()['warning'],
                         '续航记录写入失败，请检查程序目录的写入权限。 设备名称未能保存；重启后可能恢复默认名称。')
        self.assertIn('warning', self.core.snapshot()['usage'])


class KeepAwakeRetryTests(CoreCase):
    def test_a_failed_open_is_retried_after_a_minute(self):
        awake = self.core.keep_awake  # never started here: no audio device is opened
        awake.wanted, awake.handle = False, None
        self.assertEqual(self.core._keep_awake_retry(), 600)
        awake.wanted = True
        self.assertEqual(self.core._keep_awake_retry(), 60, 'wanted but not running')
        awake.handle = object()
        try:
            self.assertEqual(self.core._keep_awake_retry(), 600, 'running: only follows the default device')
        finally:
            awake.wanted, awake.handle = False, None  # close() must not try to close a fake handle


if __name__ == '__main__':
    unittest.main()
