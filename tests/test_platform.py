"""Raw input parsing, RPC framing, sounds and prefs. (The pen's mouse keys: test_pen_keys.)"""
import io
import json
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
import wave

from bridge.prefs import Preferences
from bridge.rawinput import parse_keyboard, parse_mouse
from bridge.rpc import RpcServer
from bridge.sounds import SOUNDS, Sounds, render


class RawParseTests(unittest.TestCase):
    def test_rawmouse_and_rawkeyboard_layouts(self):
        mouse = struct.pack('<H2xHHIiiI', 0, 0x0001, 0, 0, -7, 12, 0)
        self.assertEqual(parse_mouse(mouse), {'dx': -7, 'dy': 12, 'buttons': 1, 'absolute': False})
        key = struct.pack('<HHHHII', 0x49, 0, 0, 0x21, 0x100, 0)
        self.assertEqual(parse_keyboard(key)['vk'], 0x21)
        self.assertTrue(parse_keyboard(key)['down'])
        self.assertFalse(parse_keyboard(struct.pack('<HHHHII', 0x49, 1, 0, 0x21, 0x101, 0))['down'])
        self.assertIsNone(parse_mouse(b'\0' * 4))


class RpcTests(unittest.TestCase):
    def test_round_trip_errors_and_bad_lines(self):
        requests = io.BytesIO(
            b'{"id":1,"method":"echo","params":{"value":"\xe4\xbd\xa0\xe5\xa5\xbd","extra":1}}\n'
            b'not json\n'
            b'{"id":2,"method":"missing"}\n'
            b'{"id":3,"method":"boom","params":[]}\n')
        output = io.BytesIO()
        done = threading.Event()
        def boom():
            raise RuntimeError('失败原因')
        server = RpcServer(requests, output, {'echo': lambda value: {'value': value}, 'boom': boom})
        server.serve()
        server.pool.shutdown(wait=True)
        lines = [json.loads(line) for line in output.getvalue().decode('utf-8').splitlines()]
        by_id = {m.get('id'): m for m in lines if 'id' in m}
        self.assertEqual(by_id[1]['result'], {'value': '你好'})
        self.assertIn('未知方法', by_id[2]['error'])
        self.assertEqual(by_id[3]['error'], '失败原因')
        self.assertTrue(any(m.get('event') == 'notice' for m in lines))

    def test_shutdown_stops_reading(self):
        requests = io.BytesIO(b'{"id":1,"method":"shutdown"}\n{"id":2,"method":"echo"}\n')
        output = io.BytesIO()
        server = RpcServer(requests, output, {'echo': lambda: 'late'})
        server.serve()
        lines = output.getvalue().decode('utf-8').splitlines()
        self.assertEqual(json.loads(lines[0])['result'], {'ok': True})
        self.assertEqual(len(lines), 1)


class SoundTests(unittest.TestCase):
    def test_every_cue_is_short_valid_audio_and_respects_switch(self):
        for name in SOUNDS:
            with wave.open(io.BytesIO(render(name, 'low'))) as file:
                self.assertEqual((file.getnchannels(), file.getsampwidth()), (1, 2))
                self.assertLess(file.getnframes() / file.getframerate(), 2.0, name)
        played = []
        settings = {'enabled': False, 'volume': 'low'}
        with tempfile.TemporaryDirectory() as folder:
            sounds = Sounds(folder, lambda: settings, player=lambda path: played.append(path.name))
            self.assertFalse(sounds.play('mode_notes'))
            self.assertTrue(sounds.play('mode_notes', force=True))
            settings['enabled'] = True
            time.sleep(.1)
            self.assertTrue(sounds.play('mode_input'))
            self.assertFalse(sounds.play('mode_input'), 'immediate repeat is rate limited')
            self.assertFalse(sounds.play('unknown'))
        self.assertEqual(played, ['mode_notes.wav', 'mode_input.wav'])


class PrefsTests(unittest.TestCase):
    def test_whitelist_partial_recovery_and_legacy_migration(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'prefs.json'
            prefs = Preferences(path, [{'overlay': {'enabled': False}}])
            self.assertFalse(prefs.get('overlay', 'enabled'))
            prefs.update({'overlay': {'all_monitors': True}, 'mode': 'present'})
            for bad in ({'unknown': 1}, {'preview': {'show_local_text': 1}}, {'mode': 'other'},
                        {'sounds': {'enabled': 'yes'}}, {'preview': {'enabled': True}}):
                with self.assertRaises(ValueError):
                    prefs.update(bad)
            self.assertEqual(prefs.get('mode'), 'present')
            saved = json.loads(path.read_text(encoding='utf-8'))
            saved['sounds'] = {'volume': 'deafening'}
            saved['notes'] = {'pause_ms': 700}  # a removed 0.7 setting is skipped, not fatal
            path.write_text(json.dumps(saved), encoding='utf-8')
            again = Preferences(path)
            self.assertTrue(again.get('overlay', 'all_monitors'))
            self.assertEqual(again.get('sounds', 'volume'), 'medium')


if __name__ == '__main__':
    unittest.main()
