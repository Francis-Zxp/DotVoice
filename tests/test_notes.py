import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from bridge.notes import NoteStore, clean_note
from bridge.backend import OpenLessBackend
from bridge.controller import Controller
from test_bridge import FakeBackend, FakeCapture, wait_for


class NotesTests(unittest.TestCase):
    def test_cleanup_preserves_research_claims_and_exact_return(self):
        text = '嗯，可能不支持这个假设。呃，样本 n=120，p=0.048；不要删除 TypeScript 参数。'
        cleaned = clean_note(text)
        self.assertEqual(cleaned, '可能不支持这个假设。\n样本 n=120，p=0.048；不要删除 TypeScript 参数。')
        with tempfile.TemporaryDirectory() as folder:
            store = NoteStore(folder)
            note = store.add(text, ['论文'])
            store.update(note['id'], '人工修改后的整理稿。')
            loaded = NoteStore(folder).list()['items'][0]
            self.assertEqual(loaded['source'], text)
            self.assertEqual(loaded['text'], '人工修改后的整理稿。')
            self.assertEqual(loaded['tags'], ['论文'])
            with self.assertRaises(ValueError): store.update('../outside', 'x')
            with self.assertRaises(ValueError): store.update(note['id'], '  ')
            self.assertIsNone(store.add(' '))

    def test_bad_file_is_preserved_and_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / ('a' * 32 + '.json')
            path.write_text('{invalid', encoding='utf8')
            result = NoteStore(folder).list()
            self.assertTrue(result['warning'])
            self.assertEqual(result['items'], [])
            self.assertEqual(path.read_text(encoding='utf8'), '{invalid')

    def test_backend_orders_no_insert_before_start_on_each_session(self):
        sent = []
        class Socket:
            def send(self, value): sent.append(json.loads(value))
        backend = OpenLessBackend()
        backend.ws = Socket()
        backend.poll = lambda: {'type':'started', 'sessionId':'a'*32}
        for mode in (False, True, False):
            backend.session_id = None
            backend.insert_text = mode
            backend.start()
            self.assertEqual(sent[-2:], [{'type':'set_insert','value':mode},{'type':'start'}])

    def test_modes_cannot_change_mid_session_and_envelope_survives_hidden_ui(self):
        events = []
        backend = FakeBackend()
        control = Controller(lambda *e: events.append(e), backend, FakeCapture)
        try:
            control.set_destination('notes', 'paper')
            control.command('arm'); wait_for(lambda: control.phase == 'idle')
            control.command('start'); wait_for(lambda: control.phase == 'recording')
            self.assertFalse(backend.insert_text)
            with self.assertRaises(RuntimeError): control.set_destination('input')
            # Even with no frontend, retain the full speech/pause envelope.
            for i, level in enumerate((1000, 2000, 0)):
                control.last_meter = 0
                control._audio(level.to_bytes(2,'little',signed=True)*160)
            control.command('stop'); wait_for(lambda: control.phase == 'processing')
            backend.incoming.put({'type':'result','text':'可能存在交互效应。'})
            wait_for(lambda: control.phase == 'idle')
            result = next(v for k,v in events if k == 'result')
            self.assertEqual(result['envelope'], [.125,.25,0])
            self.assertEqual(result['destination'], 'notes')
            self.assertEqual(result['category'], 'paper')
            self.assertFalse(control.voice_levels)
            control.set_destination('input')
            control.command('start'); wait_for(lambda: control.phase == 'recording')
            self.assertTrue(backend.insert_text)
        finally:
            control.close()
