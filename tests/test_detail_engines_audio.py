"""WinMM capture detail: finished buffers are read in the order they were queued, so a stall
that lets several finish at once never reorders the audio. No real device is opened."""
import ctypes as c
import threading
import unittest
from unittest.mock import Mock, patch

from bridge import audio
from bridge.controller import Controller


class FakeWinMM:
    """waveIn* stand-ins: the 'driver' fills queued headers strictly in the order they were added."""

    def __init__(self):
        self.prepared, self.queue = [], []

    def waveInOpen(self, handle, *args):
        handle._obj.value = 1
        return 0

    def waveInPrepareHeader(self, handle, header, size):
        self.prepared.append(header._obj)
        return 0

    def waveInAddBuffer(self, handle, header, size):
        header._obj.flags &= ~1   # as winmm does: WHDR_DONE is cleared on queueing
        self.queue.append(header._obj)
        return 0

    def waveInStart(self, handle):
        return 0

    waveInReset = waveInClose = waveInStart

    def waveInUnprepareHeader(self, handle, header, size):
        return 0

    def complete(self, payloads):
        for payload in payloads:
            header = self.queue.pop(0)
            c.memmove(header.data, payload, len(payload))
            header.recorded = len(payload)
            header.flags |= 1

    def queued_indexes(self):
        return [next(i for i, known in enumerate(self.prepared) if known is header) for header in self.queue]


class Steps:
    """Stands in for capture.stopped: each wait() runs the next driver step, then the capture stops."""

    def __init__(self, steps):
        self.steps, self.done = list(steps), False

    def is_set(self):
        return self.done

    def set(self):
        self.done = True

    def wait(self, timeout=None):
        if self.steps:
            self.steps.pop(0)()
        else:
            self.done = True


def chunk(n):
    return bytes([n, n])


class BufferOrderTests(unittest.TestCase):
    def run_capture(self, *batches):
        driver, got, errors = FakeWinMM(), [], []
        capture = audio.AudioCapture(got.append, errors.append, settle=0)
        counter = iter(range(1000))
        capture.stopped = Steps([lambda size=size: driver.complete([chunk(next(counter)) for _ in range(size)])
                                 for size in batches])
        with patch('bridge.audio.win', driver), patch('bridge.audio.find_device', return_value=(0, 'Baseus')):
            capture._run()
        self.assertEqual((errors, capture.error), ([], None))
        return got, driver

    def test_buffers_finished_during_a_stall_keep_their_queue_order(self):
        # 0..5 are read, then 6, 7 and the requeued 0 finish together (a stall of 150 ms).
        got, driver = self.run_capture(6, 3)
        self.assertEqual(got, [chunk(n) for n in range(9)], 'newest 50 ms must not jump ahead')
        self.assertEqual(driver.queued_indexes(), [1, 2, 3, 4, 5, 6, 7, 0], 'requeued in rotation')

    def test_a_full_lap_of_finished_buffers_is_read_once_in_order(self):
        got, driver = self.run_capture(5, 8, 2)
        self.assertEqual(got, [chunk(n) for n in range(15)])
        self.assertEqual(driver.queued_indexes(), [7, 0, 1, 2, 3, 4, 5, 6])


class CaptureOwnershipTests(unittest.TestCase):
    def stuck_capture(self):
        capture = audio.AudioCapture(lambda pcm: None, lambda error: None)
        capture.thread = Mock()
        capture.thread.is_alive.return_value = True
        return capture

    def test_a_timed_out_capture_close_is_reported(self):
        capture = self.stuck_capture()
        with self.assertRaisesRegex(RuntimeError, '释放'):
            capture.close()
        self.assertTrue(capture.stopped.is_set())
        capture.thread.is_alive.return_value = False
        capture.close()  # retry confirms ownership may now be released

    def test_receiver_switch_does_not_open_another_capture_before_release(self):
        control = Controller.__new__(Controller)
        capture = control.capture = self.stuck_capture()
        control._open_capture = Mock()
        control.audio_lock, control.preroll = threading.Lock(), []
        with self.assertRaisesRegex(RuntimeError, '释放'):
            control._route({'id': 'another-pen'})
        control._open_capture.assert_not_called()
        self.assertIs(control.capture, capture)

    def test_meter_keeps_ownership_until_the_capture_really_stops(self):
        control = Controller.__new__(Controller)
        capture = control.meter_capture = self.stuck_capture()
        self.assertFalse(control._close_meter())
        self.assertIs(control.meter_capture, capture)
        capture.thread.is_alive.return_value = False
        self.assertTrue(control._close_meter())
        self.assertIsNone(control.meter_capture)

    def test_enabling_voice_waits_for_the_meter_capture_to_release(self):
        control = Controller.__new__(Controller)
        control.hardware = control.capture = control.default_route = None
        control.device, control.backend = 'baseus', Mock()
        control.abort, control.closing = threading.Event(), threading.Event()
        control.meter_capture = self.stuck_capture()
        control._open_capture = Mock()
        with self.assertRaisesRegex(RuntimeError, '试音麦克风尚未完全释放'):
            control._arm()
        control._open_capture.assert_not_called()

    def test_failed_meter_start_still_retains_a_stuck_capture(self):
        control = Controller.__new__(Controller)
        capture = self.stuck_capture()
        capture.start = Mock(side_effect=RuntimeError('启动超时'))
        control.meter_until, control.meter_retry = float('inf'), 0
        control.meter_capture = control.capture = control.default_route = None
        control._open_capture, control.notify = Mock(return_value=capture), Mock()
        control._sync_meter()
        self.assertIs(control.meter_capture, capture)
        control._sync_meter()
        self.assertEqual(control._open_capture.call_count, 1)


if __name__ == '__main__':
    unittest.main()
