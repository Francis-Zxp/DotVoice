"""Several identical Baseus receivers: identity, routing, battery per pen, names, fallback.

Uses a fake device tree, fake raw reports from two receivers and fake captures;
no device, microphone or cfgmgr32 call is involved.
"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge import audio
from bridge.instances import (InstanceRegistry, fallback_id, instance_id, interface_instance,
                              usb_parent)

try:
    from .test_bridge import FakeBackend, wait_for
    from .core_fakes import FakeTree, make_core
except ImportError:
    from test_bridge import FakeBackend, wait_for
    from core_fakes import FakeTree, make_core

GUID_HID = '{4D1E55B2-F16F-11CF-88CB-001111000030}'
GUID_KS = '{6994ad04-93ef-11d0-a3cc-00a0c9223196}'
# Receiver A as measured on 2026-10-07; receiver B is the same layout on another port.
A_USB = r'USB\VID_ABC8&PID_CA08\6&4C78E7C&0&2'
B_USB = r'USB\VID_ABC8&PID_CA08\6&4C78E7C&0&3'
A_HID = r'\\?\HID#VID_ABC8&PID_CA08&MI_03#8&701A230&0&0000#' + GUID_HID
A_KBD = r'\\?\HID#VID_ABC8&PID_CA08&MI_02&COL01#8&3B1AC34&0&0000#{884B96C3-56EF-11D1-BC8C-00A0C91405DD}'
B_HID = r'\\?\HID#VID_ABC8&PID_CA08&MI_03#8&5F00D1&0&0000#' + GUID_HID
A_MIC = r'\\?\usb#vid_abc8&pid_ca08&mi_00#7&79b523&0&0000#' + GUID_KS + r'\global'
B_MIC = r'\\?\usb#vid_abc8&pid_ca08&mi_00#7&1c2d3e&0&0000#' + GUID_KS + r'\global'
OTHER_MIC = r'\\?\hdaudio#func_01&ven_10ec&dev_0256#5&25c9f8d&0&0001#' + GUID_KS + r'\rtmicinwave_c2'
PARENTS = {
    r'HID\VID_ABC8&PID_CA08&MI_03\8&701A230&0&0000': r'USB\VID_ABC8&PID_CA08&MI_03\7&79B523&0&0003',
    r'USB\VID_ABC8&PID_CA08&MI_03\7&79B523&0&0003': A_USB,
    r'HID\VID_ABC8&PID_CA08&MI_02&COL01\8&3B1AC34&0&0000': r'USB\VID_ABC8&PID_CA08&MI_02\7&79B523&0&0002',
    r'USB\VID_ABC8&PID_CA08&MI_02\7&79B523&0&0002': A_USB,
    r'USB\VID_ABC8&PID_CA08&MI_00\7&79B523&0&0000': A_USB,
    r'HID\VID_ABC8&PID_CA08&MI_03\8&5F00D1&0&0000': r'USB\VID_ABC8&PID_CA08&MI_03\7&1C2D3E&0&0003',
    r'USB\VID_ABC8&PID_CA08&MI_03\7&1C2D3E&0&0003': B_USB,
    r'USB\VID_ABC8&PID_CA08&MI_00\7&1C2D3E&0&0000': B_USB,
    A_USB: r'USB\ROOT_HUB30\5&29356010&0&0',
    B_USB: r'USB\ROOT_HUB30\5&29356010&0&0',
    r'HDAUDIO\FUNC_01&VEN_10EC&DEV_0256\5&25C9F8D&0&0001': r'PCI\VEN_1022&DEV_15E3\4&E5B94E7&0&0641',
}
A_ID, B_ID = instance_id('baseus', A_USB), instance_id('baseus', B_USB)


def report(path, code, battery=0x4c):
    """A vendor report as RawMonitor delivers it (paths are upper-cased there)."""
    return {'path': path.upper(), 'kind': 2,
            'hex': '0a0100%02xff%02x00732101c781784000ab' % (battery, code)}


def found(*paths):
    return [{'path': path.upper(), 'kind': 2, 'page': 0xff03, 'usage': 0} for path in paths]


def press(core, path, code):
    core.on_hid(report(path, code))
    core.on_hid(report(path, 0))


def capture_log():
    """A capture class that remembers which receiver each microphone was opened for."""
    opened = []

    class Capture:
        def __init__(self, callback, error, target=None):
            self.callback, self.error, self.target = callback, error, target
            self.closed = False

        def start(self):
            opened.append(self)

        def close(self):
            self.closed = True

    return Capture, opened


class IdentityTests(unittest.TestCase):
    def test_interface_paths_map_to_their_usb_receiver(self):
        tree = FakeTree(PARENTS)
        self.assertEqual(interface_instance(A_HID), r'HID\VID_ABC8&PID_CA08&MI_03\8&701A230&0&0000')
        self.assertEqual(interface_instance(A_MIC), r'USB\VID_ABC8&PID_CA08&MI_00\7&79B523&0&0000')
        self.assertIsNone(interface_instance('BaseusPresent'))
        self.assertEqual(usb_parent(A_HID, tree=tree), A_USB)
        self.assertEqual(usb_parent(A_KBD, tree=tree), A_USB, 'keyboard collection is the same receiver')
        self.assertEqual(usb_parent(A_MIC, tree=tree), A_USB)
        self.assertEqual(usb_parent(B_HID, tree=tree), B_USB)
        self.assertIsNone(usb_parent(OTHER_MIC, tree=tree), 'stops at the root without a Baseus node')
        self.assertIsNone(usb_parent(A_HID, tree=FakeTree()))
        self.assertNotEqual(A_ID, B_ID)
        self.assertRegex(A_ID, r'^baseus:[0-9a-f]{8}$')

    def test_microphone_is_chosen_by_receiver_not_by_name(self):
        tree = FakeTree(PARENTS)
        inputs = [(0, 'Microphone (3- BaseusPresent)'), (1, '麦克风 (Realtek(R) Audio)'),
                  (2, 'Microphone (4- BaseusPresent)'), (3, '会议麦克风')]
        paths = {0: A_MIC, 1: OTHER_MIC, 2: B_MIC, 3: None}
        lookups = []

        def interface(index):
            lookups.append(index)
            return paths[index]
        with patch('bridge.rawinput.devices', return_value=found(A_HID, B_HID)), \
                patch('bridge.audio.wave_inputs', return_value=inputs), \
                patch('bridge.audio.interface_path', interface):
            self.assertEqual(audio.find_device('baseus', B_USB, tree)[0], 2)
            self.assertEqual(lookups, [0, 2], 'same-named inputs are mapped first')
            self.assertEqual(audio.find_device('baseus', A_USB.lower(), tree)[0], 0)
            with self.assertRaisesRegex(RuntimeError, '唯一'):
                audio.find_device('baseus')  # without a receiver: the old unique-name rule
            with self.assertRaisesRegex(RuntimeError, '2 个接收器'):
                audio.find_device('baseus', B_USB, FakeTree())  # mapping failed, two names
            paths[3], paths[2] = B_MIC, None  # B's endpoint renamed in Sound settings
            self.assertEqual(audio.find_device('baseus', B_USB, tree), (3, '会议麦克风'))
        single = inputs[:2]
        with patch('bridge.rawinput.devices', return_value=found(A_HID)), \
                patch('bridge.audio.wave_inputs', return_value=single), \
                patch('bridge.audio.interface_path', return_value=None):
            self.assertEqual(audio.find_device('baseus', A_USB, FakeTree())[0], 0, 'name fallback')
            self.assertEqual(audio.find_device('baseus')[0], 0, 'one receiver works exactly as before')

    def test_capture_passes_its_receiver_to_the_lookup(self):
        seen = []

        def fake_find(device, parent=None, tree=None):
            seen.append(parent)
            raise RuntimeError('stop here')
        capture = audio.AudioCapture(lambda pcm: None, lambda error: None,
                                     target={'id': B_ID, 'parent': B_USB}, settle=.25)
        with patch('bridge.audio.find_device', fake_find):
            with self.assertRaisesRegex(RuntimeError, 'stop here'):
                capture.start()
        self.assertEqual((seen, capture.settle), ([B_USB], .25))


class RegistryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / 'instances.json'
        self.tree = FakeTree(PARENTS)
        self.now = [1000.0]

    def registry(self, tree=None):
        return InstanceRegistry(self.path, tree=tree or self.tree, clock=lambda: self.now[0])

    def labels(self, registry):
        return {item['id']: (item['label'], item['present']) for item in registry.snapshot()}

    def test_labels_follow_first_sight_persist_and_rename(self):
        registry = self.registry()
        self.assertTrue(registry.update_present(found(A_HID, A_KBD)))
        self.assertFalse(registry.update_present(found(A_HID, A_KBD)), 'no change, no event')
        self.now[0] += 60
        registry.update_present(found(A_HID, A_KBD, B_HID))
        self.assertEqual(self.labels(registry), {A_ID: ('倍思 #1', True), B_ID: ('倍思 #2', True)})
        registry.rename(B_ID, '  小明的\n笔 ')
        self.assertEqual(registry.label(B_ID), '小明的 笔')
        with self.assertRaises(ValueError):
            registry.rename(B_ID, '长' * 25)
        with self.assertRaises(KeyError):
            registry.rename('baseus:00000000', '无')
        registry.update_present(found(A_HID))  # B unplugged: still listed, by name
        again = self.registry()
        self.assertEqual(self.labels(again), {A_ID: ('倍思 #1', False), B_ID: ('小明的 笔', False)})
        again.rename(B_ID, '')
        self.assertEqual(again.label(B_ID), '倍思 #2', 'an empty name restores the default')
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual([item['seq'] for item in saved['instances']], [1, 2])

    def test_receivers_found_together_are_numbered_by_usb_location(self):
        registry = self.registry()
        registry.update_present(found(B_HID, A_HID))
        self.assertEqual(registry.label(A_ID), '倍思 #1')  # ...&0&2 before ...&0&3
        self.assertEqual(registry.label(B_ID), '倍思 #2')

    def test_only_receiver_moved_to_another_port_keeps_its_name(self):
        registry = self.registry()
        registry.update_present(found(A_HID))
        registry.rename(A_ID, '我的笔')
        registry.update_present([])
        registry.update_present(found(B_HID))  # same pen, other USB port
        self.assertEqual(self.labels(registry), {B_ID: ('我的笔', True)})
        registry.update_present(found(A_HID, B_HID))
        self.assertEqual(registry.label(A_ID), '倍思 #2', 'with two known pens nothing is guessed')

    def test_unreadable_tree_means_one_receiver_per_model(self):
        registry = self.registry(FakeTree())
        registry.update_present(found(A_HID, A_KBD))
        self.assertEqual(self.labels(registry), {fallback_id('baseus'): ('倍思 #1', True)})
        self.assertEqual(registry.observe(A_HID.upper()), (fallback_id('baseus'), False))
        self.assertEqual(registry.default_target('baseus'), {'id': fallback_id('baseus'), 'parent': None})

    def test_unreadable_path_among_several_receivers_is_not_guessed(self):
        registry = self.registry()
        registry.update_present(found(A_HID, B_HID))
        stray = r'\\?\HID#VID_ABC8&PID_CA08&MI_03#9&DEAD&0&0000#' + GUID_HID
        self.assertEqual(registry.observe(stray.upper()), (None, False))
        self.assertIsNone(registry.target_for(stray))
        self.assertEqual(len(registry.snapshot()), 2, 'no ghost receiver')
        registry.update_present(found(A_HID))
        self.assertEqual(registry.observe(stray.upper()), (A_ID, False), 'one receiver: it must be that one')

    def test_corrupt_file_starts_fresh(self):
        self.path.write_text('{not json', encoding='utf-8')
        registry = self.registry()
        self.assertTrue(registry.warning)
        registry.update_present(found(A_HID))
        self.assertEqual(registry.label(A_ID), '倍思 #1')


class CoreRoutingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.events, self.backend = [], FakeBackend()
        self.capture, self.opened = capture_log()

    def make(self, tree=None):
        core = make_core(self.base, self.backend, events=self.events, capture=self.capture,
                         device_tree=tree or FakeTree(PARENTS))
        self.addCleanup(core.close)
        return core

    def arm(self, core):
        core.controller.command('arm')
        wait_for(lambda: core.controller.phase == 'idle')

    def instances(self, core):
        return {item['id']: item for item in core.snapshot()['instances']}

    def notices(self):
        return [value for kind, value in self.events if kind == 'notice']

    def test_each_pen_records_from_its_own_microphone_one_session_at_a_time(self):
        core = self.make()
        core._devices_changed(found(A_HID, A_KBD, B_HID))
        listed = core.snapshot()['instances']
        self.assertEqual([(i['id'], i['label'], i['present']) for i in listed],
                         [(A_ID, '倍思 #1', True), (B_ID, '倍思 #2', True)])
        self.assertTrue(core.snapshot()['devices'], 'the model list is unchanged')
        self.assertIn('instances', [kind for kind, _ in self.events])
        self.arm(core)
        self.assertEqual(self.opened[-1].target['id'], A_ID, 'nothing used yet: the first pen')

        press(core, B_HID, 0x74)  # pen B: short press starts
        wait_for(lambda: core.controller.phase == 'recording')
        self.assertEqual([c.target['id'] for c in self.opened], [A_ID, B_ID])
        self.assertTrue(self.opened[0].closed, 'only one microphone is open')
        state = self.instances(core)
        self.assertTrue(state[B_ID]['active'])
        self.assertFalse(state[A_ID]['active'])
        self.assertIsNotNone(state[B_ID]['last_used'])

        press(core, A_HID, 0x74)  # pen A while B records: told to wait
        wait_for(lambda: self.notices())
        press(core, A_HID, 0x75)  # A's own latch releasing must not stop B
        time.sleep(.3)
        self.assertEqual(core.controller.phase, 'recording')
        self.assertEqual(len(self.opened), 2)

        pcm = b'\x01\x00' * 800
        self.opened[-1].callback(pcm)
        wait_for(lambda: ('pcm', pcm) in self.backend.calls)
        press(core, B_HID, 0x75)
        wait_for(lambda: core.controller.phase == 'processing')
        self.backend.incoming.put({'type': 'result', 'text': '第二支笔'})
        wait_for(lambda: core.controller.phase == 'idle')
        self.assertEqual(self.backend.calls.count('start'), 1)
        self.assertFalse(self.instances(core)[B_ID]['active'])

        core.controller.command('disarm')
        wait_for(lambda: core.controller.phase == 'disconnected')
        self.arm(core)
        self.assertEqual(self.opened[-1].target['id'], B_ID, 'the last pen used stays warm')

    def test_quick_note_follows_the_pen_and_another_pens_stop_is_ignored(self):
        core = self.make()
        core._devices_changed(found(A_HID, B_HID))
        self.arm(core)
        press(core, B_HID, 0x7a)  # ⑦ on pen B: quick note from B's microphone
        wait_for(lambda: core.controller.phase == 'recording')
        self.assertEqual(self.opened[-1].target['id'], B_ID)
        self.assertEqual(core.controller.session_destination, 'notes')
        press(core, A_HID, 0x7a)
        wait_for(lambda: self.notices())
        press(core, A_HID, 0x7b)
        time.sleep(.3)
        self.assertEqual(core.controller.phase, 'recording', "pen A's 7B leaves B's note running")
        press(core, B_HID, 0x7b)
        wait_for(lambda: core.controller.phase == 'processing')
        self.assertEqual(self.backend.calls.count('start'), 1)

    def test_battery_is_kept_per_pen_and_the_ledger_follows_pen_one(self):
        core = self.make()
        core._devices_changed(found(A_HID, B_HID))
        core.on_hid(report(A_HID, 0, battery=80))
        core.on_hid(report(B_HID, 0, battery=15))
        state = self.instances(core)
        self.assertEqual((state[A_ID]['battery']['percent'], state[B_ID]['battery']['percent']), (80, 15))
        self.assertIsInstance(state[B_ID]['battery']['reported_at'], float)
        self.assertEqual(core.ledger.data['battery']['percent'], 80, 'usage history is pen #1 only')
        alerts = [value for kind, value in self.events if kind == 'battery_alert']
        self.assertEqual(alerts, [{'percent': 15, 'level': 20, 'instance': B_ID, 'label': '倍思 #2'}])
        core.on_hid(report(A_HID, 0, battery=80))  # pen A's full battery never re-arms B's alert
        core.on_hid(report(B_HID, 0, battery=15))
        self.assertEqual(len([1 for kind, _ in self.events if kind == 'battery_alert']), 1)

    def test_rename_rpc_persists_and_emits(self):
        core = self.make()
        core._devices_changed(found(A_HID, B_HID))
        answer = core.methods()['rename_instance'](id=B_ID, label='送小王')
        self.assertEqual([i['label'] for i in answer['instances']], ['倍思 #1', '送小王'])
        self.assertEqual(self.events[-1], ('instances', answer['instances']))
        self.assertIn('error', core.rename_instance(id='baseus:ffffffff', label='x'))
        self.assertIn('error', core.rename_instance(id=B_ID, label=7))
        saved = json.loads((self.base / 'runtime' / 'instances.json').read_text(encoding='utf-8'))
        self.assertIn('送小王', [item['label'] for item in saved['instances']])

    def test_unplugging_the_warm_receiver_moves_the_microphone(self):
        core = self.make()
        core._devices_changed(found(A_HID, B_HID))
        self.arm(core)
        self.assertEqual(self.opened[-1].target['id'], A_ID)
        core._devices_changed(found(B_HID))
        wait_for(lambda: self.opened[-1].target['id'] == B_ID)
        self.assertTrue(self.opened[0].closed)
        self.assertFalse(self.instances(core)[A_ID]['present'])
        self.assertEqual(core.controller.phase, 'idle')

    def test_unreadable_tree_keeps_a_single_receiver_working_as_before(self):
        core = self.make(FakeTree())
        self.arm(core)  # receivers not known yet: the microphone is found by name
        self.assertIsNone(self.opened[-1].target)
        press(core, A_HID, 0x76)
        wait_for(lambda: core.controller.phase == 'recording')
        self.assertEqual(len(self.opened), 1, 'no reopen: the only receiver already records')
        listed = core.snapshot()['instances']
        self.assertEqual([(i['id'], i['label'], i['active']) for i in listed],
                         [(fallback_id('baseus'), '倍思 #1', True)])
        core.on_hid(report(A_HID, 0x77))
        wait_for(lambda: core.controller.phase == 'processing')
        self.backend.incoming.put({'type': 'result', 'text': 'ok'})
        wait_for(lambda: core.controller.phase == 'idle')
        self.assertEqual(self.backend.calls.count('start'), 1)
        self.assertEqual(core.ledger.data['battery']['percent'], 0x4c)


if __name__ == '__main__':
    unittest.main()
