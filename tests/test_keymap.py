"""Key bindings, guided learning and mode toggling through the core."""
import json
from pathlib import Path
import tempfile
import time
import unittest
import unittest.mock

from bridge.keymap import HOLD_RESET_NOTICE, VERSION, Keymap

try:
    from .test_bridge import FakeBackend, event, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, event, wait_for
    from core_fakes import make_core


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class KeymapTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'bindings.json'
        self.progress = []
        self.clock = Clock()
        self.keymap = Keymap(self.path, self.progress.append, clock=self.clock)

    def test_default_key7_long_press_switches_and_short_press_is_a_quick_note(self):
        # Measured: a short press (7a/7b) latches the pen microphone on/off -> quick note.
        self.assertEqual(self.keymap.vendor('baseus', 0x7c), 'toggle_mode')
        self.assertEqual(self.keymap.vendor('baseus', 0x7d), 'ignored')
        for code in (0x7a, 0x7b):
            self.assertEqual(self.keymap.vendor('baseus', code), 'quick_note')
        for code in (0x74, 0x75, 0x76, 0x77, 0x6a):
            self.assertIsNone(self.keymap.vendor('baseus', code))
        # The volume keys send vendor codes (78 / 79, key log 2026-10-12): wheel slots by
        # default, so the keyboard hook watches no key for them. ④'s long press is 6D.
        self.assertEqual(self.keymap.vendor('baseus', 0x78), 'airmouse_wheel_up')
        self.assertEqual(self.keymap.vendor('baseus', 0x79), 'airmouse_wheel_down')
        self.assertEqual(self.keymap.vendor('baseus', 0x6d), 'right_click')
        self.assertEqual(self.keymap.airmouse_vks('baseus'), {'wheel_up': set(), 'wheel_down': set()})
        self.assertEqual(self.keymap.hold_slot('baseus', 0x72), ('airmouse_activity', True))
        self.assertEqual(self.keymap.hold_slot('baseus', 0x73), ('airmouse_activity', False))
        self.assertIsNone(self.keymap.hold_slot('baseus', 0x7c))

    def test_learning_two_taps_and_hold_classifies_fire_and_release(self):
        self.keymap.learn_start('baseus', 'arm_toggle')
        for code in (0x71, 0x72):  # firmware latch: start then stop
            self.assertEqual(self.keymap.vendor('baseus', code), 'learning')
        self.assertEqual(self.keymap.learning['step'], 'hold')
        self.keymap.vendor('baseus', 0x78)
        self.clock.now += 1
        self.keymap.vendor('baseus', 0x79)
        binding = self.keymap.bindings('baseus')['arm_toggle']
        self.assertEqual(binding['fire'], ['71', '72', '78'])
        self.assertEqual(binding['ignore'], ['79'])
        self.assertIsNone(self.keymap.learning)
        self.assertEqual(self.progress[-1]['status'], 'done')
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['devices']['baseus']['arm_toggle']['fire'], ['71', '72', '78'])
        self.assertEqual(Keymap(self.path).vendor('baseus', 0x72), 'arm_toggle')

    def test_learning_rejects_record_key_and_moves_reused_key(self):
        self.keymap.learn_start('baseus', 'cancel')
        self.keymap.vendor('baseus', 0x74)
        self.assertEqual(self.progress[-1]['status'], 'error')
        self.assertEqual(self.keymap.learning['captured'], [])
        # Rebinding key 7 (taps + hold) to another action releases it from toggle_mode.
        for code in (0x7a, 0x7b, 0x7c):
            self.keymap.vendor('baseus', code)
        self.clock.now += 1
        self.keymap.vendor('baseus', 0x7d)
        bindings = self.keymap.bindings('baseus')
        self.assertNotIn('toggle_mode', bindings)
        self.assertEqual(self.keymap.vendor('baseus', 0x7a), 'cancel')
        self.assertEqual(self.keymap.vendor('baseus', 0x7d), 'ignored')
        self.keymap.reset('baseus')
        self.assertEqual(self.keymap.vendor('baseus', 0x7c), 'toggle_mode')

    def test_fire_beats_another_slots_ignore_list(self):
        # Short taps are ignored by the default toggle but may be bound elsewhere.
        self.keymap.set('baseus', 'cancel', {'source': 'vendor', 'fire': ['7a']})
        self.assertEqual(self.keymap.vendor('baseus', 0x7a), 'cancel')
        self.assertEqual(self.keymap.vendor('baseus', 0x7c), 'toggle_mode')

    def test_wheel_learning_takes_the_volume_keys_code_while_key5_is_held(self):
        self.keymap.learn_start('baseus', 'airmouse_wheel_down')
        self.assertEqual(self.keymap.learning['step'], 'tap')
        # "按住 ⑤ + 音量 −": ⑤'s own codes (and its tap) are never recorded as the wheel key.
        for code in (0x72, 0x73, 0x71, 0x68):
            self.assertFalse(self.keymap.learns('baseus', code), hex(code))
        self.assertNotEqual(self.keymap.vendor('baseus', 0x72), 'learning')
        self.assertTrue(self.keymap.learns('baseus', 0x79))
        self.assertEqual(self.keymap.vendor('baseus', 0x79), 'learning')
        self.assertIsNone(self.keymap.learning, 'one press is enough')
        self.assertEqual(self.progress[-1]['status'], 'done')
        self.assertEqual(self.progress[-1]['captured'], ['79'])
        binding = self.keymap.bindings('baseus')['airmouse_wheel_down']
        self.assertEqual((binding['source'], binding['fire'], binding['default']), ('vendor', ['79'], False))
        self.assertEqual(self.keymap.vendor('baseus', 0x79), 'airmouse_wheel_down')

    def test_wheel_learning_still_takes_a_keyboard_key_but_a_vendor_code_wins(self):
        # A receiver whose volume keys type VKs: the key is taken on its release.
        self.keymap.learn_start('baseus', 'airmouse_wheel_up')
        self.assertEqual(self.keymap.keyboard('baseus', 0x11, True), 'learning', "the pen's Ctrl is skipped")
        self.assertEqual(self.keymap.keyboard('baseus', 0xAF, True), 'learning')
        self.assertIsNotNone(self.keymap.learning, 'not yet: a vendor code may follow')
        self.keymap.keyboard('baseus', 0xAF, False)
        self.assertIsNone(self.keymap.learning)
        self.assertEqual(self.progress[-1]['captured'], ['VK AF'])
        self.assertEqual(self.keymap.airmouse_vks('baseus')['wheel_up'], {0xAF})
        self.assertIsNone(self.keymap.vendor('baseus', 0x78), 'the vendor default was replaced')
        # ④'s long press types B a moment before its 6D: the vendor code is what gets recorded.
        self.keymap.learn_start('baseus', 'airmouse_wheel_up')
        self.keymap.keyboard('baseus', 0x42, True)
        self.assertEqual(self.keymap.vendor('baseus', 0x6d), 'learning')
        self.assertIsNone(self.keymap.keyboard('baseus', 0x42, False), 'the recording is over')
        binding = self.keymap.bindings('baseus')['airmouse_wheel_up']
        self.assertEqual((binding['source'], binding['fire']), ('vendor', ['6d']))
        self.assertNotIn('right_click', self.keymap.bindings('baseus'), 'one key, one action')
        self.assertEqual(self.keymap.airmouse_vks('baseus')['wheel_up'], set())

    def test_learning_refusals(self):
        with self.assertRaises(ValueError):  # 0.10's page-key click slots are gone
            self.keymap.learn_start('baseus', 'airmouse_left')
        with self.assertRaises(ValueError):
            self.keymap.learn_start('tidepool', 'toggle_mode')
        with self.assertRaises(ValueError):
            self.keymap.set('baseus', 'toggle_mode', {'source': 'vendor', 'fire': ['76']})
        with self.assertRaises(ValueError):  # a keyboard VK fits the wheel slots only
            self.keymap.set('baseus', 'right_click', {'source': 'keyboard', 'vk': [0x42]})
        self.keymap.learn_start('baseus', 'airmouse_wheel_up')
        self.keymap.vendor('baseus', 0x74)  # the record key
        self.assertEqual(self.progress[-1]['status'], 'error')
        self.assertIsNotNone(self.keymap.learning)

    def test_bindings_saved_before_vendor_wheel_codes_are_carried_forward(self):
        # 0.11.0 wrote version 1: a cleared wheel slot had cleared the keyboard default that
        # never worked on the BS-OH085; a learned keyboard key is kept as it was.
        self.path.write_text(json.dumps({'version': 1, 'devices': {'baseus': {
            'airmouse_wheel_up': None,
            'airmouse_wheel_down': {'source': 'keyboard', 'vk': [0xAE], 'label': '键盘 VK AE'},
            'right_click': {'source': 'vendor', 'fire': ['6d'], 'ignore': [], 'gesture': 'single',
                            'label': '自定义 6D'}}}}), encoding='utf-8')
        keymap = Keymap(self.path, clock=self.clock)
        self.assertEqual(keymap.warning, '')
        bindings = keymap.bindings('baseus')
        self.assertEqual((bindings['airmouse_wheel_up']['fire'], bindings['airmouse_wheel_up']['default']),
                         (['78'], True))
        self.assertEqual(keymap.airmouse_vks('baseus'), {'wheel_up': set(), 'wheel_down': {0xAE}})
        self.assertEqual(bindings['right_click']['label'], '自定义 6D', 'a learned key loads as it was')
        keymap.set('baseus', 'airmouse_wheel_up', None)  # cleared again, now on purpose
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['version'], VERSION)
        self.assertNotIn('airmouse_wheel_up', Keymap(self.path).bindings('baseus'), 'a version-2 clear stays')

    def test_double_gesture_and_pv60_release(self):
        self.keymap.set('pv60', 'toggle_mode', {'source': 'pv60', 'fire': ['13'], 'gesture': 'double'})
        self.assertEqual(self.keymap.pv60(0x13, True), 'ignored')
        self.assertEqual(self.keymap.pv60(0x13, False), 'ignored')
        self.clock.now += .3
        self.assertEqual(self.keymap.pv60(0x13, True), 'toggle_mode')
        self.clock.now += 2
        self.assertEqual(self.keymap.pv60(0x13, True), 'ignored')
        self.assertIsNone(self.keymap.pv60(0x14, True))

    def test_hold_learning_never_saves_an_unrelated_pair(self):
        # Field review 2026-10-14: a stray 67 then ⑧'s press 68 was saved as fire 67 / ignore 68,
        # so ⑧ only ever "released" the slot and Ctrl / annotate stayed on via 67.
        self.keymap.learn_start('baseus', 'airmouse_ctrl')
        self.assertEqual(self.keymap.vendor('baseus', 0x67), 'learning')
        self.assertEqual(self.keymap.vendor('baseus', 0x68), 'learning')
        self.assertIsNotNone(self.keymap.learning, 'not saved')
        self.assertEqual(self.progress[-1]['status'], 'error')
        self.assertIn('68 → 69', self.progress[-1]['message'])
        self.assertEqual(self.keymap.learning['captured'], ['68'], 'it starts over from the new press')
        self.clock.now += 1
        self.assertEqual(self.keymap.vendor('baseus', 0x69), 'learning')
        self.assertIsNone(self.keymap.learning)
        binding = self.keymap.bindings('baseus')['airmouse_ctrl']
        self.assertEqual((binding['fire'], binding['ignore']), (['68'], ['69']))
        # Another key's codes in between are refused as well (72 is ⑤'s press, 6a ③'s tap).
        self.keymap.learn_start('baseus', 'annotate')
        for code in (0x72, 0x6a):
            self.keymap.vendor('baseus', code)
        self.assertIsNotNone(self.keymap.learning)
        self.keymap.learn_cancel()
        self.assertNotIn('annotate', self.keymap.bindings('baseus'))

    def test_a_hold_key_without_a_release_code_ends_on_its_neutral_report(self):
        self.keymap.learn_start('baseus', 'annotate')
        self.keymap.vendor('baseus', 0x6e)
        self.clock.now += .02
        self.assertFalse(self.keymap.neutral('baseus'), 'the neutral report right after a press is not a release')
        self.assertIsNotNone(self.keymap.learning)
        self.clock.now += 1
        self.assertTrue(self.keymap.neutral('baseus'))
        binding = self.keymap.bindings('baseus')['annotate']
        self.assertEqual((binding['fire'], binding['ignore']), (['6e'], []))
        self.assertEqual(self.keymap.hold_slot('baseus', 0x6e), ('annotate', True))
        self.assertEqual(self.keymap.neutral_release('baseus', 0x6e), ('annotate', False))
        self.assertIsNone(self.keymap.neutral_release('baseus', 0x68), '⑧ has its own release code')
        self.assertFalse(self.keymap.neutral('baseus'), 'nothing is being recorded')

    def test_a_release_list_never_shadows_another_slots_press(self):
        # airmouse_activity listed 68 as a release: ⑧'s press (airmouse_ctrl) must still press.
        self.keymap.user['baseus']['airmouse_activity'] = {'source': 'vendor', 'fire': ['72'], 'ignore': ['68', '73'],
                                                          'gesture': 'single', 'label': '旧的'}
        self.assertEqual(self.keymap.hold_slot('baseus', 0x68), ('airmouse_ctrl', True))
        self.assertEqual(self.keymap.hold_slot('baseus', 0x73), ('airmouse_activity', False))
        # Binding a key's press elsewhere takes it off every other slot's release list.
        self.keymap.set('baseus', 'airmouse_activity', {'source': 'vendor', 'fire': ['72'], 'ignore': ['73', '6e']})
        self.assertEqual(self.keymap.hold_slot('baseus', 0x6e), ('airmouse_activity', False))
        self.keymap.set('baseus', 'annotate', {'source': 'vendor', 'fire': ['6e'], 'ignore': ['6f']})
        activity = self.keymap.bindings('baseus')['airmouse_activity']
        self.assertEqual((activity['fire'], activity['ignore']), (['72'], ['73']))
        self.assertEqual(self.keymap.hold_slot('baseus', 0x6e), ('annotate', True))
        saved = json.loads(self.path.read_text(encoding='utf-8'))['devices']['baseus']['airmouse_activity']
        self.assertEqual(saved['ignore'], ['73'])
        self.assertNotIn('default', saved)

    def test_a_mislearned_hold_key_is_reset_once_and_the_rest_kept(self):
        right_click = {'source': 'vendor', 'fire': ['6d'], 'ignore': [], 'gesture': 'single', 'label': '自定义 6D'}
        activity = {'source': 'vendor', 'fire': ['72'], 'ignore': ['73'], 'gesture': 'single', 'label': '自定义 72'}
        self.path.write_text(json.dumps({'version': 2, 'devices': {'baseus': {
            'airmouse_ctrl': {'source': 'vendor', 'fire': ['67'], 'ignore': ['68'], 'gesture': 'single',
                              'label': '自定义 67'},
            'airmouse_activity': activity, 'right_click': right_click}}}), encoding='utf-8')
        keymap = Keymap(self.path, clock=self.clock)
        self.assertEqual(keymap.warning, HOLD_RESET_NOTICE.format(labels='标注键 ⑧'))
        bindings = keymap.bindings('baseus')
        self.assertEqual((bindings['airmouse_ctrl']['fire'], bindings['airmouse_ctrl']['ignore'],
                          bindings['airmouse_ctrl']['default']), (['68'], ['69'], True))
        self.assertEqual(keymap.hold_slot('baseus', 0x68), ('airmouse_ctrl', True))
        self.assertIsNone(keymap.hold_slot('baseus', 0x67))
        self.assertEqual(bindings['airmouse_activity']['label'], '自定义 72', 'other bindings untouched')
        self.assertEqual(bindings['right_click']['label'], '自定义 6D')
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['version'], VERSION)
        self.assertNotIn('airmouse_ctrl', saved['devices']['baseus'])
        self.assertEqual(Keymap(self.path).warning, '', 'once: the upgraded file loads quietly')
        self.assertIn('warning', keymap.snapshot())

    def test_learning_times_out(self):
        self.keymap.learn_start('baseus', 'toggle_mode')
        self.clock.now += 30
        self.keymap.tick()
        self.assertIsNone(self.keymap.learning)
        self.assertEqual(self.progress[-1]['status'], 'timeout')


class ModeToggleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.events, self.sounds = [], []
        self.backend = FakeBackend()
        self.core = make_core(Path(folder.name), self.backend, events=self.events, sounds=self.sounds)
        self.addCleanup(self.core.close)
        self.control = self.core.controller

    def modes(self):
        return [value for kind, value in self.events if kind == 'mode']

    def test_key7_toggles_with_distinct_sounds_and_skips_record_path(self):
        for code in (0x7c, 0x00, 0x7d, 0x00, 0x7a, 0x00, 0x7b, 0x00):
            self.core.on_hid(event(code))  # long press toggles; short taps do not
        wait_for(lambda: self.control.destination == 'notes')
        for _ in range(2):  # notes -> present -> input
            self.core.on_hid(event(0x7c))
            self.core.on_hid(event(0x7d))
        wait_for(lambda: len(self.sounds) >= 3)
        self.assertEqual([m['value'] for m in self.modes()], ['notes', 'present', 'input'])
        self.assertEqual(self.sounds[-3:], ['mode_notes', 'mode_present', 'mode_input'])
        self.assertEqual(self.control.destination, 'input')
        self.assertNotIn('start', self.backend.calls)
        self.assertEqual(self.core.prefs.get('mode'), 'input')

    def test_present_mode_types_at_cursor_and_cycles_pointer_tools(self):
        self.core.set_mode('present')
        self.assertEqual(self.control.destination, 'input', 'voice still types in present mode')
        tools = []
        self.core.emit = lambda kind, value: tools.append((kind, value))
        self.core.on_hid(event(0x72))  # the laser is the default: holding ⑤ shows it at once
        self.assertEqual(tools[-1], ('pointer', {'tool': 'laser', 'active': True, 'hide_cursor': True, 'zoom': 200}))
        self.core.on_hid(event(0x73))
        self.assertEqual(tools[-1], ('pointer', {'tool': 'laser', 'active': False, 'hide_cursor': True, 'zoom': 200}))
        self.core.on_hid(event(0x72))  # a second hold right away is just another hold
        self.core.on_hid(event(0x73))
        self.assertNotIn('tool', [k for k, _ in tools])
        for code in (0x71, 0x00, 0x71, 0x00):  # the pen's double-tap
            self.core.on_hid(event(code))
        wait_for(lambda: ('tool', {'tool': 'spotlight'}) in tools)
        self.assertEqual(self.core.prefs.get('present', 'tool'), 'spotlight', 'the choice is remembered')
        self.core.on_hid(event(0x72))
        self.assertIn(('pointer', {'tool': 'spotlight', 'active': True, 'hide_cursor': True, 'zoom': 200}), tools)
        self.core.on_hid(event(0x73))

    def test_key5_single_tap_does_nothing_and_a_double_tap_cycles_tools_in_present_mode(self):
        tools = []
        self.core.emit = lambda kind, value: kind == 'tool' and tools.append(value['tool'])
        self.core.set_mode('present')
        self.core.on_hid(event(0x71))  # one quick tap of ⑤
        self.core.on_hid(event(0x00))
        time.sleep(.7)
        self.core.on_hid(event(0x71))  # another, too late to pair
        self.core.on_hid(event(0x00))
        time.sleep(.7)
        self.assertEqual(tools, [], 'single taps never switch')
        for code in (0x71, 0x00, 0x71, 0x00):  # a double-click
            self.core.on_hid(event(code))
        wait_for(lambda: tools)
        self.assertEqual(tools, ['spotlight'])
        self.core.on_hid(event(0x71))  # the third tap starts a new pair
        self.core.on_hid(event(0x00))
        time.sleep(.3)
        self.assertEqual(tools, ['spotlight'])
        self.core.set_mode('input')
        for code in (0x71, 0x00, 0x71, 0x00):
            self.core.on_hid(event(code))
        time.sleep(.2)
        self.assertEqual(tools, ['spotlight'], 'other modes click instead (test_pen_keys)')

    def test_tool_preview_shows_and_hides_without_the_pen(self):
        events = []
        self.core.emit = lambda kind, value: events.append((kind, value))
        with unittest.mock.patch('bridge.core.TOOL_PREVIEW', .2):
            self.assertEqual(self.core.preview_tool('magnifier')['tool'], 'magnifier')
            wait_for(lambda: len(events) >= 2)
        self.assertEqual([v['active'] for _, v in events], [True, False])
        self.assertIn('error', self.core.preview_tool('mouse'))

    def test_laser_key_toggles_a_latched_laser_only_in_present_mode(self):
        pointers = []
        self.core.emit = lambda kind, value: kind == 'pointer' and pointers.append(value)
        self.core.on_hid(event(0x64))
        self.core.on_hid(event(0x00))
        self.assertEqual(pointers, [], 'input mode leaves ② alone')
        self.core.set_mode('present')
        for code in (0x64, 0x00, 0x64, 0x00, 0x64, 0x00):
            self.core.on_hid(event(code))
        self.assertEqual([p['active'] for p in pointers], [True, False, True])
        self.assertTrue(all(p['latched'] and p['tool'] == 'laser' for p in pointers))
        self.core.set_mode('input')
        self.assertEqual(pointers[-1], {'tool': 'laser', 'active': False, 'latched': True, 'hide_cursor': True})

    def test_key7_short_press_records_a_quick_note_in_input_mode(self):
        self.control.command('arm')
        wait_for(lambda: self.control.phase == 'idle')
        self.assertEqual(self.control.destination, 'input')
        self.core.on_hid(event(0x7a))  # pen microphone on
        self.core.on_hid(event(0x00))
        wait_for(lambda: self.control.phase == 'recording')
        self.assertFalse(self.control.backend.insert_text, 'a quick note never types at the cursor')
        states = [v for k, v in self.events if k == 'state' and v['phase'] == 'recording']
        self.assertEqual(states[-1]['destination'], 'notes')
        self.core.on_hid(event(0x74))  # ⑥ during a quick note does not start a second session
        self.core.on_hid(event(0x00))
        self.core.on_hid(event(0x7b))  # pen microphone off
        self.core.on_hid(event(0x00))
        wait_for(lambda: self.control.phase == 'processing')
        self.backend.incoming.put({'type': 'result', 'text': '闪念：明天带样品'})
        wait_for(lambda: self.control.phase == 'idle')
        self.assertEqual(self.core.notes()['items'][0]['source'], '闪念：明天带样品')
        self.assertEqual(self.control.destination, 'input', 'the mode itself is unchanged')
        self.assertEqual(self.backend.calls.count('start'), 1)
        self.assertIn('start_notes', self.sounds)

    def test_toggle_while_recording_is_deferred_until_idle(self):
        self.control.command('arm')
        wait_for(lambda: self.control.phase == 'idle')
        self.control.command('start')
        wait_for(lambda: self.control.phase == 'recording')
        answer = self.core.toggle_mode('key')
        self.assertEqual(answer['value'], 'input')
        self.assertEqual(answer['pending'], 'notes')
        self.assertTrue(self.control.backend.insert_text)
        self.control.command('stop')
        wait_for(lambda: self.control.phase == 'processing')
        self.backend.incoming.put({'type': 'result', 'text': '先输入这一段'})
        wait_for(lambda: self.control.destination == 'notes')
        # set_mode clears pending_mode just after the controller applied the destination.
        wait_for(lambda: self.core.pending_mode is None)
        self.assertEqual(self.core.app_mode, 'notes')
        self.assertIn('start_input', self.sounds)
        self.assertIn('stop', self.sounds)
        self.assertIn('done_input', self.sounds)


class BatteryViewTests(unittest.TestCase):
    def test_rebound_is_smoothed_but_charging_is_shown(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        core = make_core(Path(folder.name))
        self.addCleanup(core.close)
        core.ledger.battery(76)
        core.ledger.battery(78)
        view = core.usage_view()['battery']
        self.assertEqual((view['percent'], view['raw']), (76, 78))
        core.ledger.battery(100)
        self.assertEqual(core.usage_view()['battery']['percent'], 100)


if __name__ == '__main__':
    unittest.main()
