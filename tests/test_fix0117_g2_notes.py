"""0.11.7 note fixes: whispered notes that mention "empty input" are saved, filler-only
results are not, and the cleanup keeps stacked end marks and closing quotes together.
No HID, microphone or network is touched."""
from pathlib import Path
import tempfile
import unittest

from bridge import secrets
from bridge.core import _empty_transcript
from bridge.notes import NoteStore, clean_note

try:
    from .core_fakes import make_core
except ImportError:
    from core_fakes import make_core

POLISHER_REPLY = '收到。\n音频转写内容为空（仅包含句号），无实质内容可整理。\n如果需要处理语音转写，请提供实际的音频转写文本。'


class Case(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        original = secrets._store
        self.addCleanup(setattr, secrets, '_store', original)   # each core points it at its runtime/


class WhisperedNoteTests(Case):
    def test_whispered_note_mentioning_an_empty_result_is_saved(self):
        # What Controller._backend_event builds for a whisper: the VAD heard 0.4 s, the
        # whisper detector 4.2 s.
        value = {'text': '提醒自己：识别结果为空的时候要给用户一个提示', 'destination': 'notes',
                 'audio_seconds': 6.0, 'speech_seconds': 0.4, 'whisper': True, 'voice_seconds': 4.2}
        self.assertFalse(_empty_transcript(value))
        core = make_core(self.base)
        self.addCleanup(core.close)
        core.notify('result', value)
        self.assertFalse(core.result.get('note_skipped'))
        items = core.notes()['items']
        self.assertEqual([n['source'] for n in items], [value['text']])
        self.assertEqual(core.result.get('note_id'), items[0]['id'])

    def test_polisher_chatter_after_little_audio_is_still_skipped(self):
        self.assertTrue(_empty_transcript({'text': POLISHER_REPLY, 'speech_seconds': 0.3}))
        self.assertTrue(_empty_transcript({'text': POLISHER_REPLY, 'speech_seconds': 0.3, 'voice_seconds': 0.5}))
        self.assertTrue(_empty_transcript({'text': POLISHER_REPLY, 'speech_seconds': 0.3, 'voice_seconds': None}))
        self.assertTrue(_empty_transcript({'text': POLISHER_REPLY}))
        self.assertFalse(_empty_transcript({'text': '这个文件夹里没有任何内容，下次记得先备份',
                                            'speech_seconds': 0.0, 'voice_seconds': 4.0}))


class FillerOnlyNoteTests(Case):
    def test_hesitation_sounds_alone_are_not_saved_as_an_empty_note(self):
        for text in ('嗯。', '嗯', '呃。'):
            self.assertTrue(_empty_transcript({'text': text, 'speech_seconds': 2.0}), text)
        self.assertFalse(_empty_transcript({'text': '嗯，明天交报告。', 'speech_seconds': 2.0}))
        core = make_core(self.base)
        self.addCleanup(core.close)
        core.notify('result', {'text': '嗯。', 'destination': 'notes', 'speech_seconds': 2.0})
        self.assertTrue(core.result.get('note_skipped'))
        self.assertEqual(core.notes()['items'], [])

    def test_store_never_saves_empty_text(self):
        with tempfile.TemporaryDirectory() as folder:
            note = NoteStore(folder).add('嗯。')
            self.assertEqual(note['text'], '嗯。')
            self.assertEqual(note['title'], '嗯。')


class CleanNoteTests(unittest.TestCase):
    def test_stacked_end_marks_and_closing_quotes_stay_on_their_line(self):
        cases = {
            '真的吗？！我不信。': '真的吗？！\n我不信。',
            '他说：“真的吗？”然后走了。': '他说：“真的吗？”\n然后走了。',
            '（备注：这里要改。）下一步。': '（备注：这里要改。）\n下一步。',
            'Really?! OK.': 'Really?!\nOK.',
            '「好的。」走吧。': '「好的。」\n走吧。',
            '第一句。第二句！第三句？': '第一句。\n第二句！\n第三句？',
        }
        for source, expected in cases.items():
            self.assertEqual(clean_note(source), expected, source)

    def test_existing_cleanup_is_unchanged(self):
        text = '嗯，可能不支持这个假设。呃，样本 n=120，p=0.048；不要删除 TypeScript 参数。'
        self.assertEqual(clean_note(text), '可能不支持这个假设。\n样本 n=120，p=0.048；不要删除 TypeScript 参数。')


if __name__ == '__main__':
    unittest.main()
