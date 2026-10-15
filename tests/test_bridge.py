import json
import queue
import struct
import threading
import time
import unittest
import uuid
from bridge.backend import OpenLessBackend, audio_frame
from bridge.controller import Controller, decode_report

PATH = r"\\?\HID#VID_ABC8&PID_CA08&MI_03#TEST"


def event(code):
    return {"path": PATH, "kind": 2, "hex": "0a01004cff" + f"{code:02x}" + "00732101c781784000ab"}


class FakeBackend:
    def __init__(self):
        self.calls = []
        self.incoming = queue.Queue()
        self.gate = None
    def connect(self): self.calls.append("connect")
    def start(self):
        self.calls.append("start")
        if self.gate:
            self.gate.wait(2)
    def feed(self, data): self.calls.append(("pcm", data))
    def stop(self): self.calls.append("stop")
    def cancel(self): self.calls.append("cancel")
    def close(self): self.calls.append("close")
    def poll(self):
        try: return self.incoming.get_nowait()
        except queue.Empty: return None


class FakeCapture:
    def __init__(self, callback, error): self.callback, self.error = callback, error
    def start(self): pass
    def close(self): pass


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for state")
        time.sleep(0.01)


class ProtocolTests(unittest.TestCase):
    def test_measured_control_reports_and_native_keys(self):
        self.assertEqual(decode_report(event(0x76)), "start")
        self.assertEqual(decode_report(event(0x77)), "stop")
        self.assertEqual(decode_report(event(0x74)), "start")
        for code in [0, 0x6a, 0x6c]:
            self.assertIsNone(decode_report(event(code)))
        wrong = event(0x76)
        wrong["path"] = "OTHER_KEYBOARD"
        self.assertIsNone(decode_report(wrong))
        wrong = event(0x76)
        wrong["kind"] = 1
        self.assertIsNone(decode_report(wrong))
        self.assertIsNone(decode_report({"kind": 2, "path": PATH, "hex": "invalid"}))

    def test_frame_preserves_pcm_and_sequence(self):
        sid = str(uuid.uuid4())
        pcm = b"\x12\x34" * 320
        frame = audio_frame(sid, 123, pcm)
        self.assertEqual(frame[:4], b"OL20")
        self.assertEqual(frame[4:20], uuid.UUID(sid).bytes)
        self.assertEqual(struct.unpack(">Q", frame[20:28])[0], 123)
        self.assertEqual(frame[28:], pcm)
        for bad in [b"", b"x", b"x" * 65538]:
            with self.assertRaises(ValueError): audio_frame(sid, 0, bad)

    def test_changing_receiver_telemetry_must_not_disable_voice_keys(self):
        for telemetry in (0, 1, 25, 76, 100, 255):
            for code, action in ((0x74, 'start'), (0x75, 'stop'), (0x76, 'start'), (0x77, 'stop')):
                report = event(code)
                report['hex'] = '0a0100' + f'{telemetry:02x}' + report['hex'][8:]
                self.assertEqual(decode_report(report), action)
        report = event(0x76)
        report['hex'] = report['hex'][:8] + '00' + report['hex'][10:]
        self.assertIsNone(decode_report(report))

    def test_backend_rejects_nonlocal_endpoint(self):
        for url in ["ws://127.0.0.1/ws", "wss://example.com/ws"]:
            with self.assertRaises(ValueError): OpenLessBackend(url)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.notifications = []
        self.control = Controller(lambda *value: self.notifications.append(value),
                                  self.backend, FakeCapture)
        self.control.command("arm")
        wait_for(lambda: self.control.phase == "idle")
    def tearDown(self): self.control.close()

    def test_duplicate_reports_do_not_double_start_or_stop(self):
        for _ in range(4): self.control.on_hid(event(0x76))
        wait_for(lambda: self.control.phase == "recording")
        self.control._audio(b"\x04\x00" * 800)
        for _ in range(4): self.control.on_hid(event(0x77))
        wait_for(lambda: self.control.phase == "processing")
        self.assertEqual(self.backend.calls.count("start"), 1)
        self.assertEqual(self.backend.calls.count("stop"), 1)
        self.assertTrue(any(isinstance(call, tuple) for call in self.backend.calls))
        self.backend.incoming.put({"type": "result", "text": "测试"})
        wait_for(lambda: self.control.phase == "idle")

    def test_begin_ack_wait_keeps_initial_audio(self):
        gate = self.backend.gate = threading.Event()
        self.control.on_hid(event(0x76))
        wait_for(lambda: "start" in self.backend.calls)
        self.control._audio(b"\x01\x00" * 800)
        self.control._audio(b"\x02\x00" * 800)
        self.control.on_hid(event(0x77))
        gate.set()
        wait_for(lambda: self.control.phase == "processing")
        frames = [call[1] for call in self.backend.calls if isinstance(call, tuple)]
        self.assertEqual(frames, [b"\x01\x00" * 800, b"\x02\x00" * 800])

    def test_silence_does_not_end_a_recording(self):
        self.control.on_hid(event(0x76))
        wait_for(lambda: self.control.phase == "recording")
        for _ in range(10): self.control._audio(bytes(1600))
        time.sleep(0.15)
        self.assertEqual(self.control.phase, "recording")
        self.assertNotIn("stop", self.backend.calls)

    def test_disconnection_cancels_without_stop_or_replay(self):
        self.control.on_hid(event(0x76))
        wait_for(lambda: self.control.phase == "recording")
        self.control._audio_error("unplugged")
        wait_for(lambda: self.control.phase == "error")
        self.assertIn("cancel", self.backend.calls)
        self.assertNotIn("stop", self.backend.calls)
        self.control.command("arm")
        wait_for(lambda: self.control.phase == "idle")
        time.sleep(0.1)
        self.assertEqual(self.backend.calls.count("start"), 1)

    def test_native_page_reports_do_not_start_asr(self):
        self.control.on_hid(event(0x6a))
        self.control.on_hid(event(0x6c))
        time.sleep(0.1)
        self.assertNotIn("start", self.backend.calls)

    def test_new_start_while_processing_is_not_queued_for_later(self):
        self.control.on_hid(event(0x76))
        wait_for(lambda: self.control.phase == "recording")
        self.control.on_hid(event(0x77))
        wait_for(lambda: self.control.phase == "processing")
        self.control.on_hid(event(0x76))
        time.sleep(0.1)
        self.backend.incoming.put({"type": "result", "text": "完成"})
        wait_for(lambda: self.control.phase == "idle")
        self.assertEqual(self.backend.calls.count("start"), 1)

    def test_real_short_click_start_and_stop_after_neutral(self):
        self.control.on_hid(event(0x74))
        self.control.on_hid(event(0x74))  # duplicate before neutral must not stop
        self.control.on_hid(event(0))
        wait_for(lambda: self.control.phase == "recording")
        time.sleep(0.2)
        self.assertNotIn("stop", self.backend.calls)
        self.control._audio(b"\x01\x00" * 800)
        self.control.on_hid(event(0x75))
        self.control.on_hid(event(0x75))
        self.control.on_hid(event(0))
        wait_for(lambda: self.control.phase == "processing")
        self.assertEqual(self.backend.calls.count("start"), 1)
        self.assertEqual(self.backend.calls.count("stop"), 1)

    def test_short_click_and_hold_can_be_used_in_alternating_sessions(self):
        for first, last in [(0x74, 0x75), (0x76, 0x77), (0x74, 0x75)]:
            self.control.on_hid(event(first))
            self.control.on_hid(event(0))
            wait_for(lambda: self.control.phase == "recording")
            self.control.on_hid(event(last))
            self.control.on_hid(event(0))
            wait_for(lambda: self.control.phase == "processing")
            self.backend.incoming.put({"type": "result", "text": "完成"})
            wait_for(lambda: self.control.phase == "idle")
        self.assertEqual(self.backend.calls.count("start"), 3)
        self.assertEqual(self.backend.calls.count("stop"), 3)

    def test_short_stop_at_idle_never_starts_a_muted_recording(self):
        self.control.on_hid(event(0x75))
        self.control.on_hid(event(0))
        time.sleep(.1)
        self.assertEqual(self.control.phase, 'idle')
        self.assertNotIn('start', self.backend.calls)

    def test_firmware_short_reports_ten_sessions_and_repeated_start_is_not_stop(self):
        for _ in range(10):
            self.control.on_hid(event(0x74))
            self.control.on_hid(event(0))
            wait_for(lambda: self.control.phase == 'recording')
            self.control.on_hid(event(0x74))
            self.control.on_hid(event(0))
            time.sleep(.02)
            self.assertEqual(self.control.phase, 'recording')
            self.control.on_hid(event(0x75))
            self.control.on_hid(event(0))
            wait_for(lambda: self.control.phase == 'processing')
            self.backend.incoming.put({'type': 'result', 'text': 'test'})
            wait_for(lambda: self.control.phase == 'idle')
        self.assertEqual(self.backend.calls.count('start'), 10)
        self.assertEqual(self.backend.calls.count('stop'), 10)


if __name__ == "__main__": unittest.main()
