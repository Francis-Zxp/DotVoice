"""0.11.8 input-side fixes: a restored default takes its key back, a key with its own release
code records only its press, a cleared wheel slot of a version-2 file stays cleared; changes
saved to a *-recovered.json file are taken (or set aside) once the original reads again;
instances.json is never overwritten after a failed read; usage.json of the wrong shape never
stops the core; write-failure warnings end; keep-awake retries; ⑧ leaves the Win key alone;
Tidepool messages; restoring Tidepool keeps its marker on failure; Raw Input looks for a
receiver often while none is registered.

No hook is installed, no HID device or audio device is opened: Windows calls are fakes and
the files are temp files.
"""
import json
import os
from pathlib import Path
import re
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

from bridge import airmouse, instances as instances_module, keymap as keymap_module, rawinput, usage
from bridge.airmouse import MODIFIERS, WM_KEYDOWN, WM_KEYUP
from bridge.instances import InstanceRegistry, instance_id
from bridge.keymap import Keymap
from bridge.sounds import KeepAwake
from bridge.tidepool import TidepoolAdapter
from bridge.usage import UsageLedger

try:
    from .core_fakes import FakeTree
except ImportError:
    from core_fakes import FakeTree


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def busy(*args, **kwargs):
    raise PermissionError(32, 'sharing violation')


def age(path, seconds):
    """Make a file look last written `seconds` ago."""
    then = time.time() - seconds
    os.utime(path, (then, then))


def kept_copies(folder, stem):
    return sorted(p.name for p in Path(folder).glob(stem + '-*.json'))


class RestoreDefaultTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.keymap = Keymap(Path(folder.name) / 'bindings.json', clock=Clock())

    def test_restoring_a_default_takes_its_key_back_from_another_slot(self):
        result = self.keymap.set('baseus', 'right_click', {'source': 'vendor', 'fire': ['7c']})
        self.assertEqual(result['released'], ['toggle_mode'])
        self.keymap.reset('baseus', 'toggle_mode')  # the settings page's 恢复默认
        bindings = self.keymap.bindings('baseus')
        self.assertEqual(bindings['toggle_mode']['fire'], ['7c'])
        self.assertNotIn('right_click', bindings, 'one key, one action: never two slots on 7c')
        self.assertEqual(self.keymap.vendor('baseus', 0x7c), 'toggle_mode')
        self.assertIn('right_click', self.keymap.snapshot()['defaults']['baseus'], 'it can be restored in turn')
        self.keymap.reset('baseus', 'right_click')
        bindings = self.keymap.bindings('baseus')
        self.assertEqual((bindings['right_click']['fire'], bindings['toggle_mode']['fire']), (['6d'], ['7c']))

    def test_a_restored_press_is_taken_off_another_slots_release_list(self):
        self.keymap.set('baseus', 'airmouse_ctrl', None)
        self.keymap.set('baseus', 'annotate', {'source': 'vendor', 'fire': ['66'], 'ignore': ['68']})
        self.keymap.reset('baseus', 'airmouse_ctrl')
        bindings = self.keymap.bindings('baseus')
        self.assertEqual(bindings['airmouse_ctrl']['fire'], ['68'])
        self.assertEqual(bindings['annotate']['ignore'], [])
        self.assertEqual(self.keymap.hold_slot('baseus', 0x68), ('airmouse_ctrl', True))


class LearnPressReleaseKeyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.clock = Clock()
        self.keymap = Keymap(Path(folder.name) / 'bindings.json', clock=self.clock)

    def record(self, slot, codes):
        self.keymap.learn_start('baseus', slot)
        for code, delay in codes:
            self.clock.now += delay
            self.assertEqual(self.keymap.vendor('baseus', code), 'learning')
        self.assertIsNone(self.keymap.learning, 'saved')
        return self.keymap.bindings('baseus')[slot]

    def test_a_key_with_its_own_release_code_fires_once_per_press(self):
        # ⑧ as the wizard asks: a short press (68, 69), then held for a second (68 ... 69).
        binding = self.record('toggle_mode', [('68', .1), ('69', .15), ('68', .5), ('69', 1.2)])
        self.assertEqual((binding['fire'], binding['ignore']), (['68'], ['69']))
        self.assertEqual(self.keymap.vendor('baseus', '68'), 'toggle_mode')
        self.assertEqual(self.keymap.vendor('baseus', '69'), 'ignored', 'the release never toggles back')

    def test_the_translate_keys_latch_codes_are_still_all_learned(self):
        binding = self.record('cancel', [('7a', .1), ('7b', .5), ('7c', .5), ('7d', 1.0)])
        self.assertEqual((binding['fire'], binding['ignore']), (['7a', '7b', '7c'], ['7d']))


class Version2WheelTests(unittest.TestCase):
    def test_a_cleared_wheel_slot_of_a_version_2_file_stays_cleared(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bindings.json'
            path.write_text(json.dumps({'version': 2, 'devices': {'baseus': {
                'airmouse_wheel_up': None,
                'cancel': {'source': 'vendor', 'fire': ['78'], 'gesture': 'single'}}}}), encoding='utf-8')
            keymap = Keymap(path, clock=Clock())
            self.assertNotIn('airmouse_wheel_up', keymap.bindings('baseus'))
            self.assertEqual(keymap.vendor('baseus', 0x78), 'cancel')


class KeymapRecoveredTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.path = Path(folder.name) / 'bindings.json'
        self.recovered = self.path.with_name('bindings-recovered.json')
        patcher = patch.object(keymap_module, 'READ_RETRY', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path.write_text(json.dumps({'version': 3, 'devices': {'baseus': {
            'quick_note': {'source': 'vendor', 'fire': ['70']},
            'cancel': {'source': 'vendor', 'fire': ['6c']}}}}), encoding='utf-8')

    def busy_session(self):
        """bindings.json held by another program for the whole start: the session starts from
        the defaults and its changes go to the copy."""
        with patch.object(Path, 'read_text', busy):
            keymap = Keymap(self.path, clock=Clock())
        self.assertEqual(keymap.path, self.recovered)
        keymap.set('baseus', 'arm_toggle', {'source': 'vendor', 'fire': ['6a']})
        keymap.set('baseus', 'toggle_mode', {'source': 'vendor', 'fire': ['6c']})
        return keymap

    def test_changes_saved_while_bindings_json_was_busy_are_merged_at_the_next_start(self):
        self.busy_session()
        age(self.path, 3600)
        keymap = Keymap(self.path, clock=Clock())
        self.assertIn('已并入', keymap.warning)
        self.assertEqual(keymap.path, self.path)
        self.assertEqual(keymap.vendor('baseus', 0x6a), 'arm_toggle')
        self.assertEqual(keymap.vendor('baseus', 0x6c), 'toggle_mode', 'the later change wins')
        self.assertNotIn('cancel', keymap.bindings('baseus'), 'one key, one action')
        self.assertEqual(keymap.vendor('baseus', 0x70), 'quick_note', 'untouched bindings are kept')
        saved = json.loads(self.path.read_text(encoding='utf-8'))['devices']['baseus']
        self.assertEqual(saved['quick_note']['fire'], ['70'], 'now in bindings.json itself')
        self.assertEqual(saved['arm_toggle']['fire'], ['6a'])
        self.assertFalse(self.recovered.exists())
        copies = kept_copies(self.folder, 'bindings-recovered')
        self.assertEqual(len(copies), 1, 'renamed, never deleted')
        self.assertIn(copies[0], keymap.warning)
        self.assertEqual(Keymap(self.path, clock=Clock()).warning, '', 'once')

    def test_an_older_copy_is_set_aside_and_never_comes_back(self):
        self.busy_session()
        age(self.recovered, 3600)  # bindings.json was edited after it
        keymap = Keymap(self.path, clock=Clock())
        self.assertIn('未采用', keymap.warning)
        self.assertEqual(keymap.vendor('baseus', 0x6a), None)
        self.assertEqual(keymap.vendor('baseus', 0x6c), 'cancel')
        self.assertFalse(self.recovered.exists())
        self.assertEqual(len(kept_copies(self.folder, 'bindings-recovered')), 1)
        with patch.object(Path, 'read_text', busy):
            later = Keymap(self.path, clock=Clock())  # bindings.json busy again one day
        self.assertIn('本次使用默认值', later.warning, 'the old copy is not "the saved bindings"')
        self.assertEqual(later.vendor('baseus', 0x6a), None)

    def test_the_copy_in_use_says_when_it_was_saved(self):
        self.busy_session()
        real = Path.read_text

        def original_busy(path, *args, **kwargs):
            if path == self.path:
                busy()
            return real(path, *args, **kwargs)

        with patch.object(Path, 'read_text', original_busy):
            again = Keymap(self.path, clock=Clock())
        self.assertIn('之前另存的绑定', again.warning)
        self.assertRegex(again.warning, r'\d{4}-\d{2}-\d{2} \d{2}:\d{2} 保存')
        self.assertEqual(again.vendor('baseus', 0x6a), 'arm_toggle')


class UsageRecoveredTests(unittest.TestCase):
    TOTALS = {'observed': 100, 'armed': 50, 'recording': 10, 'sessions': 20}

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.path = Path(folder.name) / 'usage.json'
        self.recovered = Path(folder.name) / 'usage-recovered.json'
        patcher = patch.object(usage, 'READ_RETRY', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def ledger(self):
        return UsageLedger(self.path, lambda: 1000, lambda: 1000)

    def write(self, path, **values):
        path.write_text(json.dumps({'version': 1, 'active': None, 'cycles': [],
                                    'totals': dict(self.TOTALS), **values}), encoding='utf-8')

    def test_records_kept_while_usage_json_was_busy_are_added_at_the_next_start(self):
        self.write(self.path)
        with patch.object(Path, 'read_text', busy):
            busy_ledger = self.ledger()  # counted from zero
        self.assertEqual(busy_ledger.path, self.recovered)
        busy_ledger.full()
        busy_ledger.completed()
        busy_ledger.battery(80)
        ledger = self.ledger()
        self.assertIn('已并入', ledger.warning)
        self.assertEqual(ledger.path, self.path)
        self.assertEqual(ledger.data['totals']['sessions'], 21, 'never replaced by the counts of one session')
        self.assertEqual(ledger.data['battery']['percent'], 80)
        self.assertEqual(ledger.data['active']['sessions'], 1)
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual((saved['totals']['sessions'], saved['battery']['percent']), (21, 80))
        self.assertFalse(self.recovered.exists())
        self.assertEqual(len(kept_copies(self.folder, 'usage-recovered')), 1)
        self.assertEqual(self.ledger().data['totals']['sessions'], 21, 'added once')

    def test_an_observation_that_went_on_meanwhile_gets_those_counts(self):
        self.write(self.path, active={'started': 500, 'observed': 10, 'armed': 5, 'recording': 1, 'sessions': 2})
        self.write(self.recovered, totals={'observed': 7, 'armed': 3, 'recording': 1, 'sessions': 4},
                   recent_sessions=[{'audio_seconds': 1, 'speech_seconds': 1, 'ended': 900, 'outcome': 'ok'}])
        ledger = self.ledger()
        self.assertEqual(ledger.data['totals']['sessions'], 24)
        self.assertEqual({k: ledger.data['active'][k] for k in ('observed', 'armed', 'recording', 'sessions')},
                         {'observed': 17, 'armed': 8, 'recording': 2, 'sessions': 6})
        self.assertEqual(len(ledger.data['recent_sessions']), 1)

    def test_a_newer_observation_ends_the_older_one(self):
        self.write(self.path, active={'started': 500, 'observed': 10, 'armed': 5, 'recording': 1, 'sessions': 2})
        self.write(self.recovered, active={'started': 800, 'observed': 1, 'armed': 0, 'recording': 0, 'sessions': 0})
        ledger = self.ledger()
        self.assertEqual(ledger.data['active']['started'], 800)
        self.assertEqual([(c['started'], c['ended'], c['complete']) for c in ledger.data['cycles']],
                         [(500, 800, False)])

    def test_a_copy_that_cannot_be_renamed_is_not_added_yet(self):
        self.write(self.path)
        self.write(self.recovered, totals={'observed': 0, 'armed': 0, 'recording': 0, 'sessions': 5})
        with patch.object(Path, 'rename', side_effect=PermissionError(32, 'sharing violation')):
            ledger = self.ledger()
        self.assertEqual(ledger.data['totals']['sessions'], 20)
        self.assertTrue(self.recovered.exists())
        self.assertEqual(self.ledger().data['totals']['sessions'], 25)
        self.assertEqual(self.ledger().data['totals']['sessions'], 25, 'never twice')

    def test_an_unreadable_copy_is_set_aside(self):
        self.write(self.path)
        self.recovered.write_text('{"version": 2}', encoding='utf-8')
        ledger = self.ledger()
        self.assertIn('未并入', ledger.warning)
        self.assertEqual(ledger.data['totals']['sessions'], 20)
        self.assertFalse(self.recovered.exists())


class UsageWarningTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'usage.json'
        patcher = patch.object(usage, 'READ_RETRY', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fail_once(self, ledger, message):
        """What the core does when a save raises."""
        ledger.completed()
        with patch.object(Path, 'write_text', side_effect=PermissionError(13, 'denied')):
            with self.assertRaises(OSError):
                ledger.save()
        ledger.warning = message

    def test_a_write_failure_warning_ends_with_the_next_save(self):
        ledger = UsageLedger(self.path, lambda: 1000, lambda: 1000)
        self.fail_once(ledger, '续航记录写入失败；语音输入仍可使用。')
        self.assertIn('写入失败', ledger.snapshot()['warning'])
        ledger.completed()
        ledger.save()
        self.assertEqual(ledger.snapshot()['warning'], '')

    def test_a_note_about_the_file_itself_stays(self):
        self.path.write_text('broken', encoding='utf-8')
        ledger = UsageLedger(self.path, lambda: 1000, lambda: 1000)
        notice = ledger.warning
        self.assertIn('已损坏', notice)
        self.fail_once(ledger, '续航记录写入失败；语音输入仍可使用。')
        ledger.completed()
        ledger.save()
        self.assertEqual(ledger.warning, notice)

    def test_another_warning_without_a_failed_save_is_kept(self):
        ledger = UsageLedger(self.path, lambda: 1000, lambda: 1000)
        ledger.warning = '已切换设备，但无法保存选择；重启后请重新选择。'
        ledger.completed()
        ledger.save()
        self.assertIn('已切换设备', ledger.warning)


class UsageShapeTests(unittest.TestCase):
    BASE = {'version': 1, 'cycles': [], 'totals': {'observed': 0, 'armed': 0, 'recording': 0, 'sessions': 3}}

    def test_a_usage_json_of_the_wrong_shape_never_stops_the_core(self):
        for change in ({'active': []}, {'active': 'x'}, {'active': {'started': 1}},
                       {'battery_samples': None}, {'recent_sessions': None}, {'battery': {'percent': '80'}},
                       {'battery': {'percent': 80}}, {'battery_samples': [{'percent': 80}]},
                       {'recent_sessions': [5]}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'usage.json'
                text = json.dumps({**self.BASE, **change})
                path.write_text(text, encoding='utf-8')
                ledger = UsageLedger(path, lambda: 1000, lambda: 1000)
                self.assertIn('已损坏', ledger.warning)
                self.assertEqual(ledger.path.name, 'usage-recovered.json')
                ledger.tick('idle')
                ledger.battery(90)
                ledger.session({'audio_seconds': 1, 'speech_seconds': 1, 'ended': 1000, 'outcome': 'ok'})
                ledger.full()
                self.assertEqual(path.read_text(encoding='utf-8'), text, 'the original file is kept')

    def test_a_well_formed_file_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'usage.json'
            path.write_text(json.dumps({**self.BASE, 'active': {
                'started': 900, 'observed': 1, 'armed': 1, 'recording': 0, 'sessions': 0},
                'battery': {'percent': 80, 'reported_at': 950.5, 'source': 'baseus_hid'},
                'battery_samples': [{'percent': 80, 'at': 950.5}],
                'recent_sessions': [{'audio_seconds': 1, 'speech_seconds': 1, 'ended': 1, 'outcome': 'ok'}]}),
                encoding='utf-8')
            ledger = UsageLedger(path, lambda: 1000, lambda: 1000)
            self.assertEqual(ledger.warning, '')
            self.assertEqual(ledger.data['totals']['sessions'], 3)


A_USB = r'USB\VID_ABC8&PID_CA08\6&4C78E7C&0&2'
B_USB = r'USB\VID_ABC8&PID_CA08\6&4C78E7C&0&3'
C_USB = r'USB\VID_ABC8&PID_CA08\6&4C78E7C&0&4'
GUID_HID = '{4D1E55B2-F16F-11CF-88CB-001111000030}'
A_HID = r'\\?\HID#VID_ABC8&PID_CA08&MI_03#8&701A230&0&0000#' + GUID_HID
B_HID = r'\\?\HID#VID_ABC8&PID_CA08&MI_03#8&5F00D1&0&0000#' + GUID_HID
C_HID = r'\\?\HID#VID_ABC8&PID_CA08&MI_03#8&ABCDEF&0&0000#' + GUID_HID
PARENTS = {r'HID\VID_ABC8&PID_CA08&MI_03\8&701A230&0&0000': r'USB\VID_ABC8&PID_CA08&MI_03\7&79B523&0&0003',
           r'USB\VID_ABC8&PID_CA08&MI_03\7&79B523&0&0003': A_USB,
           r'HID\VID_ABC8&PID_CA08&MI_03\8&5F00D1&0&0000': r'USB\VID_ABC8&PID_CA08&MI_03\7&1C2D3E&0&0003',
           r'USB\VID_ABC8&PID_CA08&MI_03\7&1C2D3E&0&0003': B_USB,
           r'HID\VID_ABC8&PID_CA08&MI_03\8&ABCDEF&0&0000': r'USB\VID_ABC8&PID_CA08&MI_03\7&ABCDEF&0&0003',
           r'USB\VID_ABC8&PID_CA08&MI_03\7&ABCDEF&0&0003': C_USB}
A_ID, B_ID, C_ID = (instance_id('baseus', usb) for usb in (A_USB, B_USB, C_USB))


def found(*paths):
    return [{'path': path.upper(), 'kind': 2, 'page': 0xff03, 'usage': 0} for path in paths]


class InstancesFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = folder.name
        self.path = Path(folder.name) / 'instances.json'
        self.recovered = Path(folder.name) / 'instances-recovered.json'
        patcher = patch.object(instances_module, 'READ_RETRY', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def registry(self, path=None):
        return InstanceRegistry(path or self.path, tree=FakeTree(PARENTS), clock=lambda: 1000.0)

    def named(self):
        registry = self.registry()
        registry.update_present(found(A_HID, B_HID))
        registry.rename(A_ID, 'MY-PEN')
        return self.path.read_text(encoding='utf-8')

    def test_a_briefly_busy_file_still_loads(self):
        self.named()
        real, calls = Path.read_text, []

        def busy_once(path, *args, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError(32, 'sharing violation')
            return real(path, *args, **kwargs)

        with patch.object(Path, 'read_text', busy_once):
            registry = self.registry()
        self.assertEqual((registry.warning, registry.label(A_ID)), ('', 'MY-PEN'))

    def test_a_file_held_open_for_good_is_never_overwritten(self):
        before = self.named()
        with patch.object(Path, 'read_text', busy):
            registry = self.registry()
        self.assertIn('占用', registry.warning)
        registry.update_present(found(A_HID))
        registry.battery(A_ID, 50)
        self.assertEqual(self.path.read_text(encoding='utf-8'), before, 'names, numbers and battery kept')
        self.assertTrue(self.recovered.exists())

    def test_a_file_of_the_wrong_shape_never_stops_the_core(self):
        for saved in ({'instances': None}, {'instances': 5}, [], 5):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'instances.json'
                path.write_text(json.dumps(saved), encoding='utf-8')
                registry = self.registry(path)
                self.assertIn('已损坏', registry.warning)
                registry.update_present(found(A_HID))
                self.assertEqual(registry.label(A_ID), '倍思 #1')
                self.assertEqual(path.read_text(encoding='utf-8'), json.dumps(saved), 'the original file is kept')

    def test_a_number_too_large_for_a_receiver_skips_only_that_one(self):
        self.path.write_text('{"instances": [{"id": "x", "model": "baseus", "seq": Infinity}]}', encoding='utf-8')
        registry = self.registry()
        registry.update_present(found(A_HID))
        self.assertEqual(registry.label(A_ID), '倍思 #1')

    def test_names_saved_while_the_file_was_busy_are_merged_at_the_next_start(self):
        self.named()  # A = MY-PEN (#1), B = #2
        with patch.object(Path, 'read_text', busy):
            registry = self.registry()  # numbered afresh
        registry.update_present(found(A_HID, C_HID))
        registry.rename(A_ID, 'RENAMED')
        registry.battery(C_ID, 40)
        again = self.registry()
        self.assertIn('已并入', again.warning)
        self.assertEqual(again.label(A_ID), 'RENAMED')
        self.assertEqual(again.label(B_ID), '倍思 #2', 'a receiver not seen then keeps its number')
        self.assertEqual(again.label(C_ID), '倍思 #3', 'one seen only then gets the next free number')
        saved = {item['id']: item for item in json.loads(self.path.read_text(encoding='utf-8'))['instances']}
        self.assertEqual((saved[A_ID]['label'], saved[C_ID]['seq'], saved[C_ID]['battery']['percent']),
                         ('RENAMED', 3, 40))
        self.assertFalse(self.recovered.exists())
        self.assertEqual(len(kept_copies(self.folder, 'instances-recovered')), 1)
        self.assertEqual(self.registry().warning, '', 'once')

    def test_a_failed_save_warning_ends_with_the_next_save(self):
        self.named()
        registry = self.registry()
        with patch.object(Path, 'write_text', side_effect=PermissionError(13, 'denied')):
            registry.rename(A_ID, 'A')
        self.assertIn('未能保存', registry.warning)
        registry.rename(A_ID, 'B')
        self.assertEqual(registry.warning, '')


class KeepAwakeRetryTests(unittest.TestCase):
    def make(self, results):
        awake, calls = KeepAwake(), []

        def start():
            calls.append('start')
            if results.pop(0):
                awake.handle = object()
                return True
            return False

        def close():
            awake.handle = None
        awake._start, awake._close = start, close
        return awake, calls

    def test_a_failed_reopen_is_retried(self):
        awake, calls = self.make([True, False, True])
        self.assertTrue(awake.start())
        awake.refresh()  # the default output device was switching just then
        self.assertFalse(awake.running)
        awake.refresh()
        self.assertTrue(awake.running)
        self.assertEqual(len(calls), 3)

    def test_no_output_device_at_start_is_retried(self):
        awake, calls = self.make([False, True])
        self.assertFalse(awake.start())  # a Bluetooth speaker still connecting at boot
        awake.refresh()
        self.assertTrue(awake.running)

    def test_switched_off_stays_off(self):
        awake, calls = self.make([True])
        awake.start()
        awake.stop()
        awake.refresh()
        self.assertFalse(awake.running)
        self.assertEqual(calls, ['start'])


class CtrlFamilyTailTests(unittest.TestCase):
    """⑧ types Ctrl+P and nothing after it: the real keyboard's Win+E right after works."""

    def make(self):
        self.clock = Clock()
        self.down = set()
        self.guard = airmouse.ShortcutGuard(clock=self.clock, slideshow=lambda: False,
                                            presentation_app=lambda: False, wait=0.0,
                                            pressed=lambda name: name in self.down)
        self.guard.enabled = True
        self.mouse = airmouse.AirMouse(lambda: {'skip_slideshow': True}, lambda: {'wheel_up': {0xAF}}, clock=self.clock,
                                       sender=lambda *args: None, presenting=lambda: False,
                                       guard=self.guard, injector=lambda keys: None)
        self.mouse.spawn = lambda work: work()
        self.mouse.remap_enabled = False
        self.mouse.refresh()

    def key(self, vk, down, pen=None):
        swallowed = self.mouse.handle(vk, WM_KEYDOWN if down else WM_KEYUP, False)
        if vk in MODIFIERS:
            (self.down.add if down and not swallowed else self.down.discard)(MODIFIERS[vk])
        if pen is not None and not swallowed:
            self.guard.raw_key({0xA2: 0x11}.get(vk, vk), down, pen)
        return swallowed

    def test_a_real_win_key_after_the_pens_ctrl_p_is_the_win_key(self):
        self.make()
        self.assertFalse(self.key(0xA2, True, pen=True))
        self.assertTrue(self.key(0x50, True), "⑧'s Ctrl+P is swallowed")
        self.key(0x50, False)
        self.key(0xA2, False, pen=True)
        self.clock.now += .5
        self.assertFalse(self.key(0x5B, True, pen=False), 'Win is not swallowed')
        self.assertFalse(self.key(0x45, True), 'E with Win held: the shortcut, never a typed "e"')
        self.key(0x45, False)
        self.key(0x5B, False, pen=False)


class TidepoolMessageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        receiver = self.root / 'receiver'
        receiver.mkdir()
        self.source = receiver / 'tidepool_receiver.py'
        self.source.write_text('VERSION = "4.3.4"\n', encoding='utf-8')
        (self.root / 'config.json').write_text(json.dumps({'port': 17876}), encoding='utf-8')

    def owners(self, port):
        return [{'pid': 123, 'exe': r'D:\python\python.exe',
                 'command': f'"D:\\python\\python.exe" -u "{self.source}" --port 17876'}]

    def failing(self, cause):
        def request(*args):
            try:
                raise cause
            except (OSError, ValueError, KeyError) as exc:
                raise RuntimeError('Tidepool 控制接口不可用；需要更新并启用接收器控制 API') from exc
        return request

    def test_a_stale_status_is_not_reported_as_an_old_receiver(self):
        snapshot = TidepoolAdapter(self.root, self.owners, lambda *args: {'ok': True, 'fresh': False}).snapshot()
        self.assertEqual(snapshot['message'], 'Tidepool 接收器状态未更新，请检查设备与接收器')
        self.assertFalse(snapshot['api_upgrade_required'])
        self.assertTrue(snapshot['receiver_running'])

    def test_an_unreachable_api_is_temporary(self):
        for cause in (ConnectionRefusedError(10061, 'refused'), TimeoutError('timed out'),
                      ValueError('receiver identity changed')):
            with self.subTest(cause=cause):
                snapshot = TidepoolAdapter(self.root, self.owners, self.failing(cause)).snapshot()
                self.assertIn('暂时连不上', snapshot['message'])
                self.assertFalse(snapshot['api_upgrade_required'])

    def test_a_missing_or_incompatible_api_still_asks_for_the_update(self):
        missing = TidepoolAdapter(self.root, self.owners).snapshot()  # no control/endpoint.json
        self.assertIn('需更新并启用控制 API', missing['message'])
        self.assertTrue(missing['api_upgrade_required'])
        gone = urllib.error.HTTPError('http://127.0.0.1:1/v1/status', 404, 'Not Found', {}, None)
        for cause in (gone, KeyError('token'), ValueError('invalid endpoint')):
            with self.subTest(cause=cause):
                snapshot = TidepoolAdapter(self.root, self.owners, self.failing(cause)).snapshot()
                self.assertIn('需更新并启用控制 API', snapshot['message'])
                self.assertTrue(snapshot['api_upgrade_required'])


class TidepoolRestoreTests(unittest.TestCase):
    def adapter(self):
        state = dict(ok=True, fresh=True, control_supported=True, receiver_pid=123, enabled=True,
                     cancel_pending=False, cancel_generation=0, cancel_error=None)
        self.fail_post = 0

        def request(method, route, payload=None):
            if method == 'POST':
                if payload['action'] == 'resume' and self.fail_post:
                    self.fail_post -= 1
                    raise RuntimeError('Tidepool 控制接口不可用')
                state['enabled'] = payload['action'] == 'resume'
                if payload['action'] == 'pause':
                    state['cancel_generation'] += 1
            return dict(state)
        adapter = TidepoolAdapter(Path(tempfile.gettempdir()) / 'no-tidepool-here', request=request)
        adapter.control('pause', temporary=True)
        return adapter, state

    def test_a_failed_resume_can_be_tried_again(self):
        adapter, state = self.adapter()
        self.fail_post = 1
        with self.assertRaises(RuntimeError):
            adapter.restore_presenter_pause()
        self.assertFalse(state['enabled'])
        self.assertIsNotNone(adapter.presenter_pause, 'still known as our pause')
        self.assertTrue(adapter.restore_presenter_pause())  # e.g. when DotVoice quits
        self.assertTrue(state['enabled'])
        self.assertIsNone(adapter.presenter_pause)

    def test_a_stale_status_keeps_the_marker(self):
        adapter, state = self.adapter()
        state['fresh'] = False
        self.assertFalse(adapter.restore_presenter_pause())
        state['fresh'] = True
        self.assertTrue(adapter.restore_presenter_pause())
        self.assertTrue(state['enabled'])


class FakeRegisterUser32:
    def __init__(self):
        self.timers = []

    def RegisterRawInputDevices(self, reg, count, size):
        return 1

    def SetTimer(self, hwnd, timer, ms, proc):
        self.timers.append((timer, ms))
        return timer


class EmptyScanTests(unittest.TestCase):
    def test_without_a_receiver_the_monitor_looks_often_then_relaxes(self):
        seen = []
        monitor = rawinput.RawMonitor(lambda event: None, devices_callback=seen.append)
        fake = FakeRegisterUser32()
        receiver = {'handle': 1, 'path': r'\\?\HID#VID_ABC8&PID_CA08&MI_03#7&1&0003'.upper(),
                    'kind': 2, 'page': 0xFF03, 'usage': 1}
        with patch.object(rawinput, 'user32', fake):
            with patch.object(rawinput, 'devices', return_value=[]):
                monitor._register()
            self.assertEqual(fake.timers[-1], (1, rawinput.EMPTY_SCAN_MS), 'no device notification can come')
            with patch.object(rawinput, 'devices', return_value=[receiver]):
                monitor._register()  # the receiver was plugged in: found by the short scan
            self.assertEqual(fake.timers[-1], (1, rawinput.FALLBACK_SCAN_MS))
            with patch.object(rawinput, 'devices', return_value=[]):
                monitor._register()  # unplugged: its usage stays registered and notifies
            self.assertEqual(fake.timers[-1], (1, rawinput.FALLBACK_SCAN_MS))
        self.assertEqual(len(seen), 3)
        self.assertLessEqual(rawinput.EMPTY_SCAN_MS, 3000)


if __name__ == '__main__':
    unittest.main()
