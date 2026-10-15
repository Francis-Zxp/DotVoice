"""Opt-in debug recordings (prefs diagnostics.save_audio): off by default, local files only,
the last 3 recordings, nothing older than 24 hours. No audio device, nothing uploaded."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

from bridge.controller import Controller
from bridge.diagnostics import KEEP, MAX_AGE, AudioDiagnostics
from bridge.prefs import Preferences
from bridge.speech_meter import SpeechMeter

try:
    from .test_bridge import FakeBackend, FakeCapture, wait_for
    from .core_fakes import make_core
    from .test_whisper import Deaf, very_soft
except ImportError:
    from test_bridge import FakeBackend, FakeCapture, wait_for
    from core_fakes import make_core
    from test_whisper import Deaf, very_soft


class Switch:
    def __init__(self, on=True):
        self.on = on

    def __call__(self):
        return self.on


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def frames(path):
    with wave.open(str(path)) as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (16000, 1, 2)
        return reader.readframes(reader.getnframes())


class RecorderTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name) / 'diagnostics'
        self.switch, self.clock = Switch(), Clock()
        self.diag = AudioDiagnostics(self.folder, enabled=self.switch, clock=self.clock)

    def record(self, outcome='completed', raw=b'\x01\x00' * 1600, sent=b'\x02\x00' * 1600):
        self.clock.now += 1.5  # each recording a little later than the one before
        self.assertTrue(self.diag.begin({'engine': '豆包语音', 'whisper_mode': 'auto'}))
        self.diag.raw(raw)
        self.diag.sent(sent)
        return self.diag.finish(outcome, {'dsp': {'engaged': True}, 'text': '轻声说的话'})

    def files(self):
        return sorted(p.name for p in self.folder.iterdir()) if self.folder.exists() else []

    def test_nothing_is_written_while_switched_off(self):
        self.switch.on = False
        self.assertFalse(self.diag.begin({'engine': 'OpenLess'}))
        self.diag.raw(b'\x01\x00' * 160)
        self.diag.sent(b'\x01\x00' * 160)
        self.assertIsNone(self.diag.finish('completed', {'text': 'x'}))
        self.diag.purge()
        self.assertFalse(self.folder.exists(), 'not even the folder')

    def test_a_recording_leaves_what_the_microphone_sent_what_the_engine_got_and_the_numbers(self):
        path = self.record()
        stamp = path.name[:-len('.json')]
        self.assertEqual(self.files(), [stamp + '-raw.wav', stamp + '-sent.wav', stamp + '.json'])
        self.assertEqual(frames(self.folder / (stamp + '-raw.wav')), b'\x01\x00' * 1600)
        self.assertEqual(frames(self.folder / (stamp + '-sent.wav')), b'\x02\x00' * 1600)
        info = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual((info['engine'], info['whisper_mode'], info['outcome'], info['text'], info['dsp']),
                         ('豆包语音', 'auto', 'completed', '轻声说的话', {'engaged': True}))
        self.assertEqual((info['raw_seconds'], info['sent_seconds'], info['truncated']), (.1, .1, False))
        self.assertEqual(info['files'], {'raw': stamp + '-raw.wav', 'sent': stamp + '-sent.wav'})

    def test_only_the_last_three_recordings_are_kept(self):
        stamps = [self.record().name[:-len('.json')] for _ in range(5)]
        self.assertEqual(len(set(stamps)), 5)
        kept = sorted({name[:19] for name in self.files()})
        self.assertEqual(kept, stamps[-KEEP:])
        self.assertEqual(len(self.files()), 3 * KEEP)

    def test_recordings_older_than_a_day_go_at_start_and_with_the_next_recording(self):
        self.record()
        day_ago = time.time() - MAX_AGE - 60
        for name in self.files():
            os.utime(self.folder / name, (day_ago, day_ago))
        mine = self.folder / 'notes.txt'
        mine.write_text('not a recording', encoding='utf-8')
        os.utime(mine, (day_ago, day_ago))
        self.diag.purge()  # DotVoice starting (and its hourly check)
        self.assertEqual(self.files(), ['notes.txt'], 'only debug recordings are ever deleted')
        fresh = self.record()
        for name in self.files():
            if name != 'notes.txt':
                os.utime(self.folder / name, (day_ago, day_ago))
        self.clock.now += 1.5
        self.assertTrue(self.diag.begin({}))  # the next recording starts
        self.assertNotIn(fresh.name, self.files())
        self.assertEqual(len(self.files()), 3, 'notes.txt and the two open WAV files')
        self.diag.abort()
        self.assertEqual(self.files(), ['notes.txt'])

    def test_switching_off_mid_recording_or_an_aborted_one_keeps_nothing(self):
        self.assertTrue(self.diag.begin({}))
        self.diag.raw(b'\x01\x00' * 160)
        self.switch.on = False
        self.assertIsNone(self.diag.finish('completed'))
        self.assertEqual(self.files(), [])
        self.switch.on = True
        self.assertTrue(self.diag.begin({}))
        self.diag.abort()  # a refused press
        self.assertEqual(self.files(), [])

    def test_switching_off_deletes_the_kept_recordings_at_once(self):
        # Review 2026-10-14: they used to stay until their 24 hours were up.
        self.record()
        self.record()
        mine = self.folder / 'notes.txt'
        mine.write_text('not a recording', encoding='utf-8')
        self.clock.now += 1.5
        self.assertTrue(self.diag.begin({}))   # one is being written right now
        self.diag.raw(b'\x01\x00' * 160)
        self.switch.on = False
        self.diag.clear()
        self.assertEqual(self.files(), ['notes.txt'], 'only debug recordings are ever deleted')
        self.diag.raw(b'\x01\x00' * 160)
        self.assertIsNone(self.diag.finish('completed'))
        self.assertEqual(self.files(), ['notes.txt'])

    def test_a_long_recording_keeps_its_first_minutes_and_a_bad_disk_breaks_nothing(self):
        with patch('bridge.diagnostics.MAX_BYTES', 3200):
            self.assertTrue(self.diag.begin({}))
            for _ in range(3):
                self.diag.raw(b'\x01\x00' * 800)
            path = self.diag.finish('completed')
        info = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual((info['raw_seconds'], info['truncated']), (.1, True))
        broken = AudioDiagnostics(path, enabled=Switch())  # "the folder" is a file
        self.assertFalse(broken.begin({}))
        broken.raw(b'\x01\x00' * 160)
        self.assertIsNone(broken.finish('completed'))
        broken.purge()


class ControllerDiagnosticsTests(unittest.TestCase):
    def record(self, switch, pcm):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        diag = AudioDiagnostics(Path(folder.name), enabled=switch)
        backend, events = FakeBackend(), []
        with patch('bridge.controller.SpeechMeter', lambda: SpeechMeter(detector=Deaf())):
            control = Controller(lambda *event: events.append(event), backend, FakeCapture)
            try:
                control.diagnostics = diag
                control.command('arm')
                wait_for(lambda: control.phase == 'idle')
                control.command('start')
                wait_for(lambda: control.phase == 'recording')
                for k in range(0, len(pcm), 800):
                    control._audio(pcm[k:k + 800])
                control.command('stop')
                wait_for(lambda: control.phase == 'processing', 6)
                backend.incoming.put({'type': 'result', 'text': '轻声说的话'})
                wait_for(lambda: control.phase == 'idle', 6)
            finally:
                control.close()
        sent = b''.join(call[1] for call in backend.calls if isinstance(call, tuple))
        return Path(folder.name), sent

    def test_a_session_saves_the_raw_audio_and_exactly_what_the_engine_got(self):
        pcm, _ = very_soft(41, -66.0)
        folder, sent = self.record(Switch(True), pcm)
        info = json.loads(next(folder.glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual((info['outcome'], info['text'], info['engine'], info['whisper_mode']),
                         ('completed', '轻声说的话', 'OpenLess', 'auto'))
        self.assertTrue(info['dsp']['engaged'])
        self.assertIn('floor_db', info['dsp'])
        self.assertIn('speech_seconds', info['vad'])
        self.assertEqual(frames(next(folder.glob('*-raw.wav'))), pcm)
        saved = frames(next(folder.glob('*-sent.wav')))
        self.assertEqual(saved, sent, 'exactly what went to the engine')
        self.assertNotEqual(saved, pcm, 'lifted by the 气声 front end')

    def test_switched_off_a_session_writes_nothing(self):
        pcm, _ = very_soft(42, -66.0, bursts=2)
        folder, sent = self.record(Switch(False), pcm)
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual(len(sent), len(pcm), 'the recording itself is unaffected')


class PrefsTests(unittest.TestCase):
    def test_the_switch_is_off_by_default_and_reaches_the_recorder(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        core = make_core(Path(folder.name))
        self.addCleanup(core.close)
        self.assertEqual(core.snapshot()['prefs']['diagnostics'], {'save_audio': False})
        self.assertIs(core.controller.diagnostics, core.diagnostics)
        self.assertEqual(core.diagnostics.folder, Path(folder.name) / 'runtime' / 'diagnostics')
        self.assertFalse(core.diagnostics._on())
        self.assertTrue(core.prefs_set({'diagnostics': {'save_audio': True}})['diagnostics']['save_audio'])
        self.assertTrue(core.diagnostics._on())
        self.assertIn('error', core.prefs_set({'diagnostics': {'save_audio': 'yes'}}))
        self.assertIn('error', core.prefs_set({'diagnostics': {'upload': True}}))
        self.assertTrue(Preferences(Path(folder.name) / 'runtime' / 'prefs.json').get('diagnostics', 'save_audio'))
        with patch('bridge.core.os.startfile') as shown:  # never open Explorer from a test
            answer = core.open_diagnostics()
        self.assertEqual(answer, {'ok': True, 'path': str(core.diagnostics.folder)})
        shown.assert_called_once_with(str(core.diagnostics.folder))
        self.assertTrue(core.diagnostics.folder.is_dir())
        kept = core.diagnostics.folder / '20261014-120000-000.json'
        kept.write_text('{}', encoding='utf-8')
        core.prefs_set({'diagnostics': {'save_audio': False}})
        self.assertFalse(kept.exists(), 'switched off: deleted now, not in 24 hours')


if __name__ == '__main__':
    unittest.main()
