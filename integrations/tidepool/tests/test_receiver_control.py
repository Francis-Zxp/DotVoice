"""DotVoice control protocol tests; fake audio/serial, no physical devices."""
import importlib.util
import json
from pathlib import Path
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

PC = Path(__file__).resolve().parents[1] / "receiver"
sys.path.insert(0, str(PC))
import tidepool_control as api
spec = importlib.util.spec_from_file_location("receiver_control_test", PC / "tidepool_receiver.py")
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)


class FakeWorker:
    def __init__(self):
        self.actions = []
        self.callback = None
    def start(self, *args, **kwargs):
        self.actions.append("start")
        return True
    def feed(self, *args):
        self.actions.append("feed")
        return True
    def cancel(self, callback):
        self.actions.append("cancel")
        self.callback = callback


class ControlTests(unittest.TestCase):
    def test_http_auth_origin_allowlist_and_status_boundary(self):
        server = api.ControlServer("unused.json", writer=lambda *args: None)
        server.publish({"receiver_running": True, "receiver_pid": 123, "device_online": False})
        server.start()
        base = "http://127.0.0.1:%d" % server.server.server_port
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(route="/v1/status", token=None, origin=None, data=None):
            headers = {"Content-Type": "application/json"}
            if token:
                headers["Authorization"] = "Bearer " + token
            if origin:
                headers["Origin"] = origin
            request = urllib.request.Request(base + route, headers=headers, data=data)
            return opener.open(request, timeout=3)
        try:
            with self.assertRaises(urllib.error.HTTPError) as failure:
                get()
            self.assertEqual(failure.exception.code, 403)
            with self.assertRaises(urllib.error.HTTPError):
                get(token=server.token, origin="https://example.com")
            with get(token=server.token) as response:
                value = json.load(response)
            self.assertTrue(value["fresh"])
            self.assertNotIn("token", value)
            self.assertNotIn("text", value)
            with self.assertRaises(urllib.error.HTTPError) as failure:
                get("/v1/control", server.token, data=b'{"action":"voice-start"}')
            self.assertEqual(failure.exception.code, 400)
        finally:
            server.close()

    def test_pause_cancels_and_blocks_both_transports_until_resume(self):
        worker = FakeWorker()
        sessions = receiver.AudioSession(worker, lambda *args: None)
        control = api.ReceiverControl(sessions, None, None, "4.0.5", 17876)
        peer = ("192.0.2.10", 17876)
        self.assertTrue(sessions.start("old", peer))
        request = api.ControlRequest("pause")
        control.apply(request)
        self.assertFalse(sessions.enabled)
        self.assertFalse(sessions.start("new", peer))
        self.assertFalse(sessions.start("new", receiver.USB_ADDR))
        self.assertFalse(control.ready_for_audio())
        resume = api.ControlRequest("resume")
        control.apply(resume)
        self.assertFalse(resume.result["ok"])
        worker.callback(None)
        resume = api.ControlRequest("resume")
        control.apply(resume)
        self.assertTrue(resume.result["ok"])
        self.assertFalse(sessions.start("old", peer))
        self.assertTrue(sessions.start("new", receiver.USB_ADDR))

    def test_online_requires_recent_device_packet(self):
        sessions = receiver.AudioSession(FakeWorker(), lambda *args: None)
        control = api.ReceiverControl(sessions, None, None, "4.0.5", 17876)
        self.assertFalse(control.state("wifi")["device_online"])
        control.observe("wifi")
        self.assertTrue(control.state("wifi")["device_online"])
        control.seen["wifi"] -= 11
        self.assertFalse(control.state("wifi")["device_online"])
        self.assertIsNone(control.state("wifi")["battery_percent"])

    def test_expired_control_does_not_run(self):
        server = api.ControlServer("unused.json")
        request = api.ControlRequest("resume", timeout=-1)
        server.requests.put(request)
        seen = []
        server.drain(seen.append)
        self.assertEqual(seen, [])
        self.assertFalse(request.result["ok"])

    def test_pause_failure_does_not_allow_resume(self):
        worker = FakeWorker()
        sessions = receiver.AudioSession(worker, lambda *args: None)
        control = api.ReceiverControl(sessions, None, None, "4.0.5", 17876)
        control.apply(api.ControlRequest("pause"))
        worker.callback("fake cancel failed")
        request = api.ControlRequest("resume")
        control.apply(request)
        self.assertFalse(request.result["ok"])
        self.assertFalse(control.ready_for_audio())

    def test_serial_control_is_not_written_until_owner_pumps_and_expires(self):
        class Serial:
            def __init__(self):
                self.writes = []
            def write(self, payload):
                self.writes.append(payload)
                return len(payload)
        link = receiver.SerialLink()
        link.ser = Serial()
        link.last_rx = time.monotonic()
        request = api.ControlRequest("wake")
        link.control(request)
        self.assertEqual(link.ser.writes, [])
        request.deadline = time.monotonic() - 1
        link._pump_control()
        self.assertEqual(link.ser.writes, [])
        self.assertFalse(request.result["ok"])
        request = api.ControlRequest("sleep")
        link.control(request)
        link._pump_control()
        self.assertEqual(link.ser.writes, [b"TPSHOT sleep\n"])
        self.assertTrue(request.result["written"])
        self.assertFalse(request.result["confirmed"])

    def test_serial_reconnect_never_replays_old_control(self):
        class Serial:
            def write(self, payload):
                raise AssertionError("old request must not be written")
        link = receiver.SerialLink()
        link.ser = Serial()
        link.last_rx = time.monotonic()
        request = api.ControlRequest("wake")
        link.control(request)
        link.ser = Serial()
        link._pump_control()
        self.assertFalse(request.result["ok"])

    def test_worker_cancel_discards_queued_audio(self):
        began, release, done = threading.Event(), threading.Event(), threading.Event()
        class Sink:
            remote = None
            stream = None
            io_failed = False
            def __init__(self):
                self.actions = []
            def start(self, *args):
                self.actions.append("start")
                began.set()
                release.wait(1)
            def feed(self, *args):
                self.actions.append("feed")
            def cancel(self):
                self.actions.append("cancel")
            def stop(self):
                self.actions.append("stop")
        sink = Sink()
        worker = receiver.AudioWorker(sink)
        try:
            worker.start("s")
            self.assertTrue(began.wait(1))
            worker.feed(0, b"pcm")
            worker.cancel(lambda error: done.set())
            release.set()
            self.assertTrue(done.wait(1))
            self.assertEqual(sink.actions, ["start", "cancel"])
        finally:
            release.set()
            worker.close()

    def test_remote_cancel_sends_cancel_not_stop_and_closes(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.closed = False
            def send(self, data):
                self.sent.append(json.loads(data))
            def close(self):
                self.closed = True
        remote = receiver.OpenLessRemoteBridge("wss://127.0.0.1:8443/ws", "unused")
        sock = remote.ws = Socket()
        remote.session_id = "s"
        remote.cancel()
        self.assertEqual(sock.sent, [{"type": "cancel"}])
        self.assertTrue(sock.closed)
        self.assertIsNone(remote.session_id)


if __name__ == "__main__":
    unittest.main()

