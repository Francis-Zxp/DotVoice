"""Protocol/lifecycle regression tests. Fake HID only; no device/audio access."""
import queue
import threading
import time
import unittest
from unittest.mock import patch

from bridge import pv60


DEVICE = dict(vendor_id=0x0611, product_id=0x3001, interface_number=5,
              usage_page=0xFF00, usage=1, path=b"fake-pv60")
DOWN = bytes.fromhex("0b305800101701")
UP = bytes.fromhex("0b305800101700")


class FakeHid:
    def __init__(self, acknowledge=True, before_ack=()):
        self.incoming = queue.Queue()
        self.writes = []
        self.closed = False
        self.acknowledge = acknowledge
        self.before_ack = before_ack
        self.fail_write = False
        self.fail_read = False
        self.short_write = False

    def write(self, report):
        if self.closed or self.fail_write:
            raise OSError("fake disconnected")
        self.writes.append(report)
        if self.short_write:
            return len(report) - 1
        if self.acknowledge:
            if report[:9] == bytes.fromhex("000600000000020501"):
                for data in self.before_ack:
                    self.incoming.put(data)
                self.incoming.put(pv60.DYNAMIC_ACK)
            elif report == pv60.SESSION_PING:
                self.incoming.put(pv60.SESSION_ACK)
            elif report == pv60.MIC_ON:
                self.incoming.put(pv60.MIC_ACK)
        return len(report)

    def read(self, size, timeout_ms):
        if self.closed or self.fail_read:
            raise OSError("fake read disconnected")
        try:
            return self.incoming.get(timeout=timeout_ms / 1000)
        except queue.Empty:
            return b""

    def close(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def test_known_challenge_from_upstream_fixture(self):
        report = pv60.dynamic_report(1_700_000_000)
        self.assertEqual(len(report), 33)
        self.assertEqual(report[:14].hex(), "0006000000000205013b03042e6f")
        self.assertEqual(report[14:], bytes(19))

    def test_enumeration_strict_read_only(self):
        candidates = [DEVICE]
        for field in ("vendor_id", "product_id", "interface_number", "usage_page"):
            candidates.append(dict(DEVICE, **{field: 0}))
        candidates.append(dict(DEVICE, usage_page=1, usage=2))
        class HidModule:
            @staticmethod
            def enumerate(vid, pid):
                self.assertEqual((vid, pid), (0x0611, 0x3001))
                return candidates
        self.assertEqual(pv60.list_devices(HidModule), [DEVICE])

    def test_voice_report_rejects_short_unknown_and_page_events(self):
        self.assertTrue(pv60.voice_button(DOWN))
        self.assertFalse(pv60.voice_button(UP))
        for report in (b"", DOWN[:6], bytes.fromhex("0b305800101702"),
                       bytes.fromhex("0b305800101301"),
                       bytes.fromhex("0b305800101401"),
                       bytes.fromhex("0b000000101701"),
                       bytes.fromhex("0a01004cff7400")):
            self.assertIsNone(pv60.voice_button(report), report)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(pv60, name, value) for name, value in (
            ("WARMUP_SECONDS", 0.03), ("OPEN_DELAY_SECONDS", 0),
            ("ACK_SECONDS", 0.08), ("KEEPALIVE_SECONDS", 0.025))]
        for item in self.patches:
            item.start()
        self.sessions = []

    def tearDown(self):
        for session in self.sessions:
            session.close()
        for item in reversed(self.patches):
            item.stop()

    def make_session(self, fake=None, device=None):
        fake = fake or FakeHid()
        buttons, errors = [], []
        session = pv60.PV60Session(buttons.append, errors.append,
                                  lambda: (device or DEVICE, fake))
        self.sessions.append(session)
        return session, fake, buttons, errors

    def wait_for(self, condition):
        end = time.monotonic() + 1
        while not condition() and time.monotonic() < end:
            time.sleep(0.005)
        self.assertTrue(condition())

    def test_handshake_discards_buttons_and_refresh_completes_before_return(self):
        session, fake, buttons, errors = self.make_session(FakeHid(before_ack=(DOWN, UP)))
        session.start()
        self.assertEqual(buttons, [])
        session.refresh()
        self.assertTrue(any(fake.writes[i:i+2] == [pv60.MIC_REFRESH, pv60.MIC_ON]
                            for i in range(len(fake.writes) - 1)))
        self.assertEqual(errors, [])

    def test_duplicate_and_malformed_reports_do_not_create_extra_edges(self):
        session, fake, buttons, _ = self.make_session()
        session.start()
        for report in (UP, DOWN, DOWN, DOWN[:6], bytes.fromhex("0b305800101401"),
                       UP, UP, DOWN, UP):
            fake.incoming.put(report)
        self.wait_for(lambda: len(buttons) == 4)
        self.assertEqual(buttons, [True, False, True, False])

    def test_other_keys_reach_on_key_without_touching_voice_edges(self):
        session, fake, buttons, _ = self.make_session()
        keys = []
        session.on_key = lambda code, down: keys.append((code, down))
        session.start()
        for report in (bytes.fromhex("0b305800101301"), bytes.fromhex("0b305800101301"),
                       bytes.fromhex("0b305800101300"), bytes.fromhex("0b305800101400"), DOWN):
            fake.incoming.put(report)
        self.wait_for(lambda: buttons == [True])
        self.assertEqual(keys, [(0x13, True), (0x13, False)])
        self.assertEqual(pv60.parse_key(bytes.fromhex("0b305800101401")), (0x14, True))

    def test_wrong_interface_is_closed_before_first_write(self):
        session, fake, _, _ = self.make_session(device=dict(DEVICE, usage_page=1))
        with self.assertRaisesRegex(RuntimeError, "非 PV60"):
            session.start()
        self.assertEqual(fake.writes, [])
        self.assertTrue(fake.closed)

    def test_missing_ack_fails_closed_without_mic_on(self):
        session, fake, _, errors = self.make_session(FakeHid(acknowledge=False))
        with self.assertRaisesRegex(RuntimeError, "未收到确认"):
            session.start()
        self.assertTrue(fake.closed)
        self.assertNotIn(pv60.MIC_ON, fake.writes)
        self.assertEqual(len(errors), 1)

    def test_partial_write_is_failure(self):
        fake = FakeHid()
        fake.short_write = True
        session, _, _, _ = self.make_session(fake)
        with self.assertRaisesRegex(RuntimeError, "未完整写入"):
            session.start()
        self.assertTrue(fake.closed)

    def test_keepalive_stops_on_close_and_no_reconnect(self):
        session, fake, _, errors = self.make_session()
        session.start()
        self.wait_for(lambda: fake.writes.count(pv60.SESSION_PING) >= 3)
        session.close()
        count = len(fake.writes)
        time.sleep(0.07)
        self.assertEqual(len(fake.writes), count)
        self.assertTrue(fake.closed)
        self.assertFalse(session.thread.is_alive())
        self.assertEqual(errors, [])
        with self.assertRaises(RuntimeError):
            session.refresh()

    def test_transport_read_failure_closes_and_reports_once(self):
        session, fake, _, errors = self.make_session()
        session.start()
        fake.fail_read = True
        self.wait_for(lambda: session.stopped.is_set())
        session.close()
        self.assertTrue(fake.closed)
        self.assertEqual(len(errors), 1)

    def test_refresh_write_failure_is_reported_to_caller(self):
        session, fake, _, _ = self.make_session()
        session.start()
        fake.fail_write = True
        with self.assertRaises(RuntimeError):
            session.refresh()
        session.close()
        self.assertTrue(fake.closed)

    def test_close_interrupts_pending_handshake(self):
        fake = FakeHid(acknowledge=False)
        session, _, buttons, _ = self.make_session(fake)
        errors = []
        def start():
            try:
                session.start()
            except RuntimeError as exc:
                errors.append(str(exc))
        thread = threading.Thread(target=start)
        thread.start()
        self.wait_for(lambda: bool(fake.writes))
        session.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(fake.closed)
        self.assertEqual(buttons, [])
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main()
