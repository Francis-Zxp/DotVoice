"""Multi-presenter integration tests using fake audio and fake PV60 sessions."""
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.controller import Controller
from bridge.devices import get_profile

try:
    from .test_bridge import FakeBackend, FakeCapture, event, wait_for
    from .core_fakes import make_core
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, event, wait_for
    from core_fakes import make_core


class FakeSession:
    instances = []

    def __init__(self, on_button, on_error):
        self.on_button, self.on_error = on_button, on_error
        self.started = self.closed = False
        self.refreshes = 0
        self.close_error = False
        self.instances.append(self)

    def start(self):
        self.started = True

    def refresh(self):
        self.refreshes += 1

    def close(self):
        if self.close_error:
            raise RuntimeError('Fake HID worker did not exit')
        self.closed = True


class DeviceTests(unittest.TestCase):
    def setUp(self):
        FakeSession.instances = []
        self.session_patch = patch('bridge.pv60.PV60Session', FakeSession)
        self.session_patch.start()
        self.addCleanup(self.session_patch.stop)
        self.backend = FakeBackend()
        self.control = Controller(lambda *_: None, self.backend, FakeCapture)
        self.addCleanup(self.control.close)

    def arm(self, device='pv60', mode='hold'):
        self.control.select_device(device, mode)
        self.control.command('arm')
        wait_for(lambda: self.control.phase == 'idle')

    def complete(self):
        self.backend.incoming.put({'type': 'result', 'text': 'test'})
        wait_for(lambda: self.control.phase == 'idle')

    def desktop(self):
        # The system temp folder: nothing may land in the project's runtime/.
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        return make_core(Path(temp.name), self.backend, controller=self.control)

    def test_profile_selection_rejects_unknown_and_busy_states(self):
        with self.assertRaises(ValueError):
            get_profile('unknown')
        with self.assertRaises(ValueError):
            self.control.select_device('pv60', 'invalid')
        self.arm()
        with self.assertRaises(RuntimeError):
            self.control.select_device('baseus')
        self.backend.gate = threading.Event()
        FakeSession.instances[-1].on_button(True)
        wait_for(lambda: self.control.phase == 'starting')
        with self.assertRaises(RuntimeError):
            self.control.select_device('baseus')
        self.backend.gate.set()
        wait_for(lambda: self.control.phase == 'recording')
        with self.assertRaises(RuntimeError):
            self.control.select_device('baseus')
        FakeSession.instances[-1].on_button(False)
        wait_for(lambda: self.control.phase == 'processing')
        with self.assertRaises(RuntimeError):
            self.control.select_device('baseus')
        self.complete()
        self.control.command('disarm')
        wait_for(lambda: self.control.phase == 'disconnected')
        self.assertTrue(FakeSession.instances[-1].closed)
        self.control.select_device('baseus')
        self.assertEqual(self.control.device, 'baseus')

    def test_pv60_hold_maps_press_release_and_ignores_baseus(self):
        self.arm()
        session = FakeSession.instances[-1]
        self.control.on_hid(event(0x76))
        self.assertNotIn('start', self.backend.calls)
        session.on_button(True)
        wait_for(lambda: self.control.phase == 'recording')
        self.control._audio(b'\x01\x00' * 800)
        session.on_button(False)
        wait_for(lambda: self.control.phase == 'processing')
        session.on_button(True)  # Busy input must not start after this result.
        session.on_button(False)
        self.complete()
        self.assertEqual(session.refreshes, 1)
        self.assertEqual(self.backend.calls.count('start'), 1)
        self.assertEqual(self.backend.calls.count('stop'), 1)

    def test_pv60_toggle_ignores_release_and_stops_on_next_press(self):
        self.arm(mode='toggle')
        session = FakeSession.instances[-1]
        session.on_button(True)
        wait_for(lambda: self.control.phase == 'recording')
        session.on_button(False)
        self.assertEqual(self.control.phase, 'recording')
        self.assertNotIn('stop', self.backend.calls)
        session.on_button(True)
        session.on_button(False)
        wait_for(lambda: self.control.phase == 'processing')
        self.assertEqual(self.backend.calls.count('start'), 1)
        self.assertEqual(self.backend.calls.count('stop'), 1)

    def test_switching_keeps_per_device_usage_separate(self):
        desktop = self.desktop()
        desktop.ledger.completed()
        desktop.ledger.completed()
        selected = desktop.select_device('pv60', 'toggle')
        self.assertNotIn('error', selected)
        self.assertEqual(selected['selected'], 'pv60')
        self.assertEqual(selected['usage']['totals']['sessions'], 0)
        desktop.ledger.completed()
        restored = desktop.select_device('baseus')
        self.assertNotIn('error', restored)
        self.assertEqual(restored['usage']['totals']['sessions'], 2)
        selected = desktop.select_device('pv60')
        self.assertEqual(selected['usage']['totals']['sessions'], 1)

    def test_usage_save_failure_cannot_split_selected_device_from_controller(self):
        desktop = self.desktop()
        desktop.ledger.completed()
        with patch.object(desktop.ledger, 'save', side_effect=OSError('disk full')):
            result = desktop.select_device('pv60')
        self.assertEqual(desktop.selected, self.control.device,
                         'UI and audio controller selected different devices')
        expected_file = 'usage.json' if desktop.selected == 'baseus' else 'usage-pv60.json'
        self.assertEqual(desktop.ledger.path.name, expected_file,
                         'Usage records belong to the previously selected device')
        if 'error' not in result:
            self.assertEqual(result['selected'], desktop.selected)

    def test_hid_close_failure_still_cancels_backend_and_prevents_second_session(self):
        self.arm()
        session = FakeSession.instances[-1]
        session.on_button(True)
        wait_for(lambda: self.control.phase == 'recording')
        session.close_error = True
        try:
            self.control.command('disarm')
            wait_for(lambda: self.control.phase == 'error')
            self.assertIn('cancel', self.backend.calls)
            self.assertIn('close', self.backend.calls)
            self.control.command('arm')
            wait_for(lambda: self.control.commands.empty())
            time.sleep(0.05)  # Fake start/close are synchronous, with no I/O waits.
            self.assertEqual(len(FakeSession.instances), 1,
                             'A second HID session replaced a worker that failed to close')
            self.assertIs(self.control.hardware, session)
            session.close_error = False
            self.control.command('arm')
            wait_for(lambda: self.control.phase == 'idle')
            self.assertTrue(session.closed)
            self.assertEqual(len(FakeSession.instances), 2)
            self.assertEqual(self.backend.calls.count('start'), 1,
                             'Recovery replayed the interrupted recording')
        finally:
            session.close_error = False


if __name__ == '__main__':
    unittest.main()
