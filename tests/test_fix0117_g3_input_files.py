"""Input-side files and enumeration: a broken or locked usage.json never loses records or
stops the core, a code list of the wrong type in bindings.json skips only that binding, a
failed Raw Input enumeration never reads as "every device unplugged".

No hook is installed and no HID device is opened: user32 is a fake where Windows is
involved, and the files are temp files.
"""
import ctypes as c
from ctypes import wintypes as w
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import airmouse, rawinput, usage
from bridge.keymap import Keymap
from bridge.usage import UsageLedger


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class UsageRecoveredTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.path = self.folder / 'usage.json'
        self.recovered = self.folder / 'usage-recovered.json'
        self.now = 1000
        patcher = patch.object(usage, 'READ_RETRY', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ledger(self, path=None):
        return UsageLedger(path or self.path, lambda: self.now, lambda: self.now)

    def test_records_kept_while_usage_json_is_broken_survive_a_restart(self):
        self.path.write_text('broken', encoding='utf-8')
        first = self.ledger()
        self.assertIn('本次从头记录', first.warning)
        first.full()
        first.completed()
        first.battery(80)
        self.assertEqual(first.path, self.recovered)

        again = self.ledger()
        self.assertIn('之前另存的记录', again.warning)
        self.assertIn('usage-recovered.json', again.warning)
        self.assertEqual(again.data['totals']['sessions'], 1)
        self.assertEqual(again.data['battery']['percent'], 80)
        self.assertIsNotNone(again.data['active'])
        again.completed()
        again.tick('disconnected')
        again.save()  # what core.close does: it must never wipe the earlier records
        saved = json.loads(self.recovered.read_text(encoding='utf-8'))
        self.assertEqual(saved['totals']['sessions'], 2)
        self.assertEqual(saved['battery']['percent'], 80)
        self.assertEqual(self.path.read_text(encoding='utf-8'), 'broken', 'the original file is kept')

    def test_the_pv60_ledger_has_its_own_recovered_file(self):
        self.recovered.write_text(json.dumps({'version': 1, 'active': None, 'cycles': [], 'totals': {
            'observed': 0, 'armed': 0, 'recording': 0, 'sessions': 5}}), encoding='utf-8')
        pv60 = self.folder / 'usage-pv60.json'
        pv60.write_text('broken', encoding='utf-8')
        ledger = self.ledger(pv60)
        self.assertEqual(ledger.path, self.folder / 'usage-pv60-recovered.json')
        self.assertEqual(ledger.data['totals']['sessions'], 0, "never the Baseus pen's records")
        self.assertIn('usage-pv60.json', ledger.warning)

    def test_an_unreadable_recovered_file_starts_afresh(self):
        self.path.write_text('broken', encoding='utf-8')
        self.recovered.write_text('{"version": 2}', encoding='utf-8')
        ledger = self.ledger()
        self.assertIn('本次从头记录', ledger.warning)
        self.assertEqual(ledger.data['totals']['sessions'], 0)
        self.assertEqual(ledger.path, self.recovered)

    def test_a_briefly_busy_file_still_loads(self):
        first = self.ledger()
        first.completed()
        first.save()
        real, calls = Path.read_text, []

        def busy_once(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(32, 'sharing violation')
            return real(path, *args, **kwargs)

        with patch.object(Path, 'read_text', busy_once):
            ledger = self.ledger()
        self.assertEqual(ledger.warning, '')
        self.assertEqual(ledger.path, self.path)
        self.assertEqual(ledger.data['totals']['sessions'], 1)

    def test_a_file_held_open_for_good_never_stops_the_core(self):
        first = self.ledger()
        first.completed()
        first.save()
        before = self.path.read_text(encoding='utf-8')
        with patch.object(Path, 'read_text', side_effect=PermissionError(32, 'sharing violation')):
            ledger = self.ledger()
        self.assertIn('占用', ledger.warning)
        self.assertEqual(ledger.path, self.recovered)
        ledger.completed()
        ledger.save()
        self.assertEqual(self.path.read_text(encoding='utf-8'), before, 'the original file is kept')


class KeymapCodeListShapeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'bindings.json'

    def test_a_null_or_numeric_code_list_skips_only_that_binding(self):
        self.path.write_text(json.dumps({'version': 3, 'devices': {'baseus': {
            'cancel': {'source': 'vendor', 'fire': None},
            'arm_toggle': {'source': 'vendor', 'fire': 5},
            'toggle_mode': {'source': 'vendor', 'fire': ['6e'], 'ignore': None},
            'quick_note': {'source': 'vendor', 'fire': ['70']}}}}), encoding='utf-8')
        keymap = Keymap(self.path, clock=Clock())
        self.assertEqual(keymap.warning, '')
        self.assertEqual(keymap.path, self.path)
        bindings = keymap.bindings('baseus')
        self.assertNotIn('cancel', bindings)
        self.assertNotIn('arm_toggle', bindings)
        self.assertTrue(bindings['toggle_mode']['default'], 'back to the default')
        self.assertEqual(keymap.vendor('baseus', 0x7c), 'toggle_mode')
        self.assertEqual(keymap.vendor('baseus', 0x70), 'quick_note')

    def test_set_says_so_in_chinese(self):
        keymap = Keymap(self.path, clock=Clock())
        for binding in ({'source': 'vendor', 'fire': None}, {'source': 'vendor', 'fire': ['6a'], 'ignore': 5}):
            with self.subTest(binding), self.assertRaisesRegex(ValueError, '按键码无效'):
                keymap.set('baseus', 'cancel', binding)


RECEIVER = r'\\?\HID#VID_ABC8&PID_CA08&MI_0%d#7&1&000%d'


class FakeRawUser32:
    """GetRawInputDeviceList / GetRawInputDeviceInfoW over a list of device paths
    (handle = index + 1). `arrive`: paths added just before the next filled call."""

    def __init__(self, names, arrive=(), error=None):
        self.names, self.arrive, self.error = list(names), list(arrive), error
        self.filled = 0

    def GetRawInputDeviceList(self, buf, count, size):
        if buf is None:
            count._obj.value = len(self.names)
            return 0
        self.filled += 1
        if self.error is not None:
            c.set_last_error(self.error)
            return 0xFFFFFFFF
        if self.arrive:
            self.names.append(self.arrive.pop(0))
        if count._obj.value < len(self.names):
            count._obj.value = len(self.names)
            c.set_last_error(rawinput.ERROR_INSUFFICIENT_BUFFER)
            return 0xFFFFFFFF  # the buffer is left untouched
        for index in range(len(self.names)):
            buf[index].handle, buf[index].kind = index + 1, 2
        return len(self.names)

    def GetRawInputDeviceInfoW(self, handle, command, buf, size):
        if command == 0x20000007:
            name = self.names[handle - 1]
            if buf is None:
                size._obj.value = len(name) + 1
                return 0
            buf.value = name
            return len(name)
        buf._obj.data[3] = 0xFF03
        return c.sizeof(buf._obj)


class RawEnumerationTests(unittest.TestCase):
    def test_failures_are_seen_as_uint_minus_one(self):
        for name in ('GetRawInputDeviceList', 'GetRawInputDeviceInfoW', 'GetRawInputData'):
            self.assertIs(getattr(rawinput.user32, name).restype, w.UINT, name)

    def test_a_device_arriving_between_the_two_calls_is_counted_again(self):
        fake = FakeRawUser32([RECEIVER % (i, i) for i in range(4)], arrive=[RECEIVER % (4, 4)])
        with patch.object(rawinput, 'user32', fake):
            found = rawinput.devices(all_supported=True)
        self.assertEqual(len(found), 5)
        self.assertEqual(found[-1]['path'], (RECEIVER % (4, 4)).upper())
        self.assertEqual(fake.filled, 2)

    def test_a_failed_enumeration_raises_instead_of_reporting_nothing(self):
        for error, tries in ((5, 1), (rawinput.ERROR_INSUFFICIENT_BUFFER, 3)):
            fake = FakeRawUser32([RECEIVER % (0, 0)], error=error)
            with self.subTest(error=error), patch.object(rawinput, 'user32', fake):
                with self.assertRaises(OSError):
                    rawinput.devices(all_supported=True)
                self.assertEqual(fake.filled, tries)

    def test_the_monitor_keeps_its_devices_when_enumeration_fails(self):
        seen = []
        monitor = rawinput.RawMonitor(lambda event: None, devices_callback=seen.append)
        monitor.paths = monitor.known = {1: (RECEIVER % (0, 0)).upper()}
        with patch.object(rawinput, 'user32', FakeRawUser32([RECEIVER % (0, 0)], error=5)):
            with self.assertRaises(OSError):
                monitor._register()  # the window procedure passes it to error_callback
        self.assertEqual(monitor.paths, {1: (RECEIVER % (0, 0)).upper()})
        self.assertEqual(seen, [], 'never reported as "receiver unplugged"')


class GuardOffForeignShiftTests(unittest.TestCase):
    """typed_by_pen (③'s F5, guard switched off) relies on other keyboards' Shift being
    reported, which the core does while the keyboard hook runs for the air mouse alone."""

    def guard(self, clock):
        guard = airmouse.ShortcutGuard(clock=clock, slideshow=lambda: False, presentation_app=lambda: False,
                                       wait=0.0, pressed=lambda name: True)
        guard.enabled = False
        guard.pen_activity(0x71)  # a ⑤ tap a moment ago
        clock.now += .2
        return guard

    def test_a_reported_real_shift_f5_is_never_the_pens(self):
        clock = Clock()
        guard = self.guard(clock)
        guard.raw_key(0xA0, True, pen=False)  # the laptop keyboard's Shift, via on_foreign_key
        guard.modifier('shift', 0xA0, True)
        self.assertFalse(guard.typed_by_pen())

    def test_without_the_report_recent_pen_activity_decides(self):
        clock = Clock()
        guard = self.guard(clock)
        guard.modifier('shift', 0xA0, True)
        self.assertTrue(guard.typed_by_pen(), 'why on_foreign_key must be set whenever the hook runs')


if __name__ == '__main__':
    unittest.main()
