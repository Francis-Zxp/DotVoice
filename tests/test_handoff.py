"""Tidepool/presenter handoff, without live processes or devices.

HandoffTests cover the exclusive mode (prefs devices.coexist = false); CoexistTests
the default 随拿随用 mode, where neither device pauses the other."""
import threading
import unittest
from unittest.mock import patch

from bridge.core import Core as Desktop
from bridge.controller import Controller

try:
    from .test_bridge import FakeBackend, FakeCapture, wait_for
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, wait_for


class Prefs:
    def __init__(self, coexist):
        self.coexist = coexist

    def get(self, *keys):
        if keys == ('devices', 'coexist'):
            return self.coexist
        raise KeyError(keys)


class HandoffTests(unittest.TestCase):
    coexist = False

    def setUp(self):
        self.calls = []
        calls = self.calls

        class Backend(FakeBackend):
            def __init__(inner):
                super().__init__()
                inner.connect_gate = None
                inner.connect_entered = threading.Event()

            def connect(inner):
                super().connect()
                calls.append('backend.connect')
                inner.connect_entered.set()
                if inner.connect_gate:
                    inner.connect_gate.wait(2)

            def cancel(inner):
                calls.append('backend.cancel')
                super().cancel()

            def close(inner):
                calls.append('backend.close')
                super().close()

        class Capture(FakeCapture):
            def start(inner):
                calls.append('capture.start')

            def close(inner):
                calls.append('capture.close')

        self.backend = Backend()
        self.control = Controller(lambda *_: None, self.backend, Capture)
        self.addCleanup(self.control.close)
        owner = self

        class Tidepool:
            def control(inner, action, temporary=False):
                calls.append('tidepool.' + action)
                if action == 'resume' and not owner.coexist:
                    owner.assertIsNone(owner.control.capture)
                    owner.assertIsNone(owner.control.hardware)
                    owner.assertFalse(owner.control.want_armed)
                    owner.assertEqual(owner.control.phase, 'disconnected')
                return {'ok': True, 'action': action, 'enabled': action == 'resume',
                        'cancel_pending': False}

            def snapshot(inner):
                return {'control_supported': True, 'receiver_running': True}

        self.desktop = Desktop.__new__(Desktop)
        self.desktop.smoke = False
        self.desktop.lock = threading.RLock()
        self.desktop.switch_lock = threading.Lock()
        self.desktop.arm_epoch = 0
        self.desktop.closed = threading.Event()
        self.desktop.controller = self.control
        self.desktop.tidepool = Tidepool()
        self.desktop.tidepool_state = self.desktop.tidepool.snapshot()
        self.desktop.notify = lambda *_: None
        self.desktop.prefs = Prefs(self.coexist)

    def test_presenter_arm_waits_for_tidepool_pause(self):
        answer = self.desktop.action('arm')
        self.assertTrue(answer['ok'])
        wait_for(lambda: self.control.phase == 'idle')
        self.assertLess(self.calls.index('tidepool.pause'), self.calls.index('backend.connect'))

    def test_pause_failure_never_arms_presenter(self):
        with patch.object(self.desktop.tidepool, 'control', side_effect=RuntimeError('cancel timeout')):
            answer = self.desktop.action('arm')
        self.assertIn('error', answer)
        self.assertFalse(self.control.want_armed)
        self.assertIsNone(self.control.capture)
        self.assertEqual(self.backend.calls, [])
        self.assertFalse(self.desktop.switch_lock.locked())

    def test_disarm_during_external_pause_cancels_pending_arm(self):
        entered, release = threading.Event(), threading.Event()
        def slow_pause(action, temporary=False):
            entered.set()
            release.wait(2)
            return {'ok': True}
        answers = []
        with patch.object(self.desktop.tidepool, 'control', side_effect=slow_pause):
            worker = threading.Thread(target=lambda: answers.append(self.desktop.action('arm')))
            worker.start()
            try:
                self.assertTrue(entered.wait(1))
                self.desktop.action('disarm')
                release.set()
                worker.join(2)
                self.assertIn('error', answers[0])
                self.assertFalse(self.control.want_armed)
                self.assertNotIn('connect', self.backend.calls)
            finally:
                release.set()
                worker.join(2)

    def test_resume_waits_for_microphone_and_backend_close(self):
        self.desktop.action('arm')
        wait_for(lambda: self.control.phase == 'idle')
        answer = self.desktop.tidepool_action('resume')
        self.assertTrue(answer['ok'])
        resume = self.calls.index('tidepool.resume')
        self.assertLess(self.calls.index('capture.close'), resume)
        self.assertLess(self.calls.index('backend.cancel'), resume)
        self.assertLess(self.calls.index('backend.close'), resume)

    def test_resume_does_not_trust_disconnected_while_earlier_arm_is_pending(self):
        gate = self.backend.connect_gate = threading.Event()
        self.desktop.action('arm')
        self.assertTrue(self.backend.connect_entered.wait(1))
        self.assertEqual(self.control.phase, 'disconnected')
        answers = []
        resumed = threading.Event()

        def resume():
            answers.append(self.desktop.tidepool_action('resume'))
            resumed.set()

        worker = threading.Thread(target=resume)
        worker.start()
        try:
            wait_for(lambda: not self.control.want_armed)
            self.assertFalse(resumed.is_set())
            self.assertNotIn('tidepool.resume', self.calls)
            gate.set()
            self.assertTrue(resumed.wait(2))
            self.assertTrue(answers[0].get('ok'), answers)
            self.assertNotIn('capture.start', self.calls)
            self.assertLess(self.calls.index('backend.close'), self.calls.index('tidepool.resume'))
        finally:
            gate.set()
            worker.join(2)

    def test_release_failure_never_resumes_tidepool(self):
        with patch.object(self.control, 'disarm_and_wait', side_effect=RuntimeError('HID still closing')):
            answer = self.desktop.tidepool_action('resume')
        self.assertIn('error', answer)
        self.assertNotIn('tidepool.resume', self.calls)
        self.assertFalse(self.desktop.switch_lock.locked())

    def test_another_handoff_returns_busy_without_waiting_for_switch_lock(self):
        self.desktop.switch_lock.acquire()
        try:
            self.assertIn('error', self.desktop.action('arm'))
            self.assertIn('error', self.desktop.tidepool_action('resume'))
            self.assertEqual(self.calls, [])
        finally:
            self.desktop.switch_lock.release()


class CoexistTests(HandoffTests):
    """随拿随用 (default): arming the pen never pauses Tidepool and resuming Tidepool
    never disarms the pen; OpenLess arbitrates one session at a time."""
    coexist = True

    def test_presenter_arm_waits_for_tidepool_pause(self):
        self.assertTrue(self.desktop.action('arm')['ok'])
        wait_for(lambda: self.control.phase == 'idle')
        self.assertNotIn('tidepool.pause', self.calls)
        self.assertIn('backend.connect', self.calls)

    def test_pause_failure_never_arms_presenter(self):
        with patch.object(self.desktop.tidepool, 'control', side_effect=RuntimeError('cancel timeout')):
            self.assertTrue(self.desktop.action('arm')['ok'], 'Tidepool is not asked to pause')
        wait_for(lambda: self.control.phase == 'idle')

    def test_disarm_during_external_pause_cancels_pending_arm(self):
        self.desktop.action('arm')
        self.desktop.action('disarm')
        wait_for(lambda: self.control.phase == 'disconnected' and not self.control.want_armed)
        self.assertNotIn('tidepool.pause', self.calls)

    def test_resume_waits_for_microphone_and_backend_close(self):
        self.desktop.action('arm')
        wait_for(lambda: self.control.phase == 'idle')
        answer = self.desktop.tidepool_action('resume')
        self.assertTrue(answer['ok'])
        self.assertIn('tidepool.resume', self.calls)
        self.assertTrue(self.control.want_armed, 'the pen stays ready')
        self.assertEqual(self.control.phase, 'idle')
        self.assertNotIn('capture.close', self.calls)

    def test_resume_does_not_trust_disconnected_while_earlier_arm_is_pending(self):
        gate = self.backend.connect_gate = threading.Event()
        try:
            self.desktop.action('arm')
            self.assertTrue(self.backend.connect_entered.wait(1))
            answer = self.desktop.tidepool_action('resume')  # does not wait for the pen
            self.assertTrue(answer['ok'])
            self.assertTrue(self.control.want_armed)
        finally:
            gate.set()
        wait_for(lambda: self.control.phase == 'idle')

    def test_release_failure_never_resumes_tidepool(self):
        with patch.object(self.control, 'disarm_and_wait', side_effect=RuntimeError('HID still closing')) as wait:
            self.assertTrue(self.desktop.tidepool_action('resume')['ok'])
        wait.assert_not_called()

    def test_another_handoff_returns_busy_without_waiting_for_switch_lock(self):
        self.desktop.switch_lock.acquire()
        try:
            self.assertTrue(self.desktop.action('arm')['ok'], 'arming needs no handoff')
            self.assertIn('error', self.desktop.tidepool_action('resume'))
        finally:
            self.desktop.switch_lock.release()


if __name__ == '__main__':
    unittest.main()
