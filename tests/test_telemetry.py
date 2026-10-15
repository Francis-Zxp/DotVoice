import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from bridge.battery import baseus_battery
from bridge.speech_meter import SpeechMeter
from bridge.usage import UsageLedger
from bridge.backend import OpenLessBackend


class TelemetryTests(unittest.TestCase):
    def test_actual_charged_report_and_unknown_values(self):
        event = {'path': 'HID#VID_ABC8&PID_CA08&MI_03', 'kind': 2,
                 'hex': '0a010064ff7700732101c781784000ab'}
        self.assertEqual(baseus_battery(event), 100)
        for value in (0, 40, 76, 99, 100, 101, 255):
            event['hex'] = f'0a0100{value:02x}ff7700732101c781784000ab'
            self.assertEqual(baseus_battery(event), value if value <= 100 else None)
        event['path'] = 'OTHER_RECEIVER'
        self.assertIsNone(baseus_battery(event))

    def test_silence_and_fragment_boundaries(self):
        meter = SpeechMeter()
        for _ in range(100):
            meter.feed(bytes(320))
        self.assertEqual(meter.snapshot(), {'audio_seconds': 1, 'speech_seconds': 0})
        class Detector:
            def is_speech(self, frame, rate):
                assert len(frame) == 320 and rate == 16000
                return frame[0] == 1
        meter = SpeechMeter(Detector())
        meter.feed(b'\1' * 160)
        meter.feed(b'\1' * 160 + bytes(320))
        self.assertEqual(meter.snapshot(), {'audio_seconds': .02, 'speech_seconds': .01})

    def test_automatic_cycle_survives_restart_without_reset_or_fabricated_charge_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [1000]
            ledger = UsageLedger(Path(tmp) / 'usage.json', lambda: now[0], lambda: now[0])
            ledger.battery(100)
            ledger.audio({'audio_seconds': 10, 'speech_seconds': 6})
            ledger.session({'audio_seconds': 10, 'speech_seconds': 6, 'ended': 1010, 'outcome': 'completed', 'text': 'never save'})
            now[0] += 60
            ledger.battery(100)
            self.assertEqual(ledger.data['active']['started'], 1000)
            ledger.save()
            reopened = UsageLedger(ledger.path, lambda: now[0], lambda: now[0])
            reopened.battery(100)
            self.assertEqual(reopened.data['active']['started'], 1000)
            reopened.battery(10)
            cycle = reopened.data['cycles'][0]
            self.assertTrue(cycle['complete'])
            self.assertEqual(cycle['speech_seconds'], 6)
            self.assertEqual(cycle['audio_seconds'], 10)
            self.assertNotIn('text', reopened.data['recent_sessions'][0])
            self.assertNotIn('charging', reopened.data['battery'])
            reopened.battery(100)
            self.assertEqual(len(reopened.data['cycles']), 1)

    def test_reconnect_reads_changed_local_port_and_pairing(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefs, pin = Path(tmp) / 'prefs.json', Path(tmp) / 'pin.txt'
            backend = OpenLessBackend(pin_file=pin, preferences_file=prefs)
            for port, pairing in [(8443, '123456'), (9443, '654321')]:
                prefs.write_text(json.dumps({'remoteInputEnabled': True, 'remoteInputPort': port}))
                pin.write_text(pairing)
                with patch('bridge.backend.websocket.create_connection') as connect:
                    connect.return_value.recv.return_value = '{"type":"auth","ok":true}'
                    backend.connect()
                    self.assertEqual(connect.call_args.args[0], f'wss://127.0.0.1:{port}/ws')
                    self.assertEqual(json.loads(connect.return_value.send.call_args.args[0])['pin'], pairing)
                    backend.close()
            prefs.write_text('{"remoteInputEnabled":false}')
            with self.assertRaisesRegex(RuntimeError, '已关闭'):
                backend.endpoint()
            for value in (True, -1, 65536, '8443'):
                prefs.write_text(json.dumps({'remoteInputPort': value}))
                with self.assertRaises(RuntimeError):
                    backend.endpoint()

    def test_old_manual_full_is_preserved_when_real_full_report_arrives(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = [1000]
            ledger = UsageLedger(Path(tmp) / 'usage.json', lambda: now[0], lambda: now[0])
            ledger.full()
            now[0] = 1100
            ledger.battery(100)
            self.assertEqual(ledger.data['active']['source'], 'baseus_hid')
            self.assertEqual(ledger.data['active']['started'], 1100)
            self.assertFalse(ledger.data['cycles'][0]['complete'])
            self.assertEqual(ledger.data['cycles'][0]['started'], 1000)


if __name__ == '__main__':
    unittest.main()
