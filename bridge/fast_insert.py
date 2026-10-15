"""极速落字: in OpenLess's 原文 (raw) mode, type the local SenseVoice text at the cursor.

Why: OpenLess may be set to a whole-file cloud recogniser (e.g. Bailian
fun-asr-flash) that only starts after the pen is released, so even raw text took
5-20 s to appear. The live preview has already recognised every sentence on this
PC by then; finishing the last one takes a fraction of a second. When OpenLess is
in raw mode nothing else would change the words, so DotVoice types them itself.
Polish modes still go through OpenLess (the LLM is the point there).

Read-only use of OpenLess files: preferences.json (only `defaultMode`),
correction-rules.json (its enabled literal replacements) and dictionary.json (its
enabled phrases), so the text matches what OpenLess would have inserted. The
local model has no hotword biasing, so dictionary phrases are restored after the
fact: a span that sounds exactly like a phrase (same pinyin, any reading of a
polyphone) becomes that phrase; Latin phrases get their spelling's case back.
The same enabled phrases, most used first, are the built-in Doubao engine's hotwords
(OpenLessSettings.hotwords). Nothing else is read; credentials live elsewhere.
"""
import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import threading
import time


def openless_dir():
    return Path(os.environ.get('APPDATA', str(Path.home()))) / 'OpenLess'


class _Cached:
    """A JSON file re-read only when its modification time changes."""
    def __init__(self, path, parse):
        self.path, self.parse = Path(path), parse
        self.stamp, self.value = None, parse(None)
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            try:
                stamp = self.path.stat().st_mtime_ns
            except OSError:
                self.stamp, self.value = None, self.parse(None)
                return self.value
            if stamp != self.stamp:
                try:
                    data = json.loads(self.path.read_text(encoding='utf-8-sig'))
                except (OSError, ValueError):
                    data = None
                self.stamp, self.value = stamp, self.parse(data)
            return self.value


def _mode(data):
    mode = data.get('defaultMode') if isinstance(data, dict) else None
    return mode if isinstance(mode, str) else None


def _rules(data):
    rules = []
    for rule in data if isinstance(data, list) else []:
        if (isinstance(rule, dict) and rule.get('enabled', True) and isinstance(rule.get('pattern'), str)
                and rule['pattern'] and isinstance(rule.get('replacement'), str)):
            rules.append((rule['pattern'], rule['replacement']))
    return rules


def _phrases(data):
    phrases = []
    for entry in data if isinstance(data, list) else []:
        phrase = entry.get('phrase') if isinstance(entry, dict) and entry.get('enabled', True) else None
        if isinstance(phrase, str) and 2 <= len(phrase.strip()) <= 32:
            phrases.append(phrase.strip())
    return phrases


def _hotwords(data):
    """The enabled phrases, most useful first: most used (OpenLess counts `hits`), then the
    newest (`createdAt`), then file order. A recogniser's hotword list is short."""
    ranked = []
    for index, entry in enumerate(data if isinstance(data, list) else []):
        phrase = entry.get('phrase') if isinstance(entry, dict) and entry.get('enabled', True) else None
        if not isinstance(phrase, str) or not 2 <= len(phrase.strip()) <= 32:
            continue
        hits = entry.get('hits')
        created = entry.get('createdAt')
        ranked.append((hits if type(hits) is int and hits > 0 else 0,
                       created if isinstance(created, str) else '', -index, phrase.strip()))
    ranked.sort(reverse=True)
    words = []
    for *_, phrase in ranked:
        if phrase not in words:
            words.append(phrase)
    return words


def _cjk(ch):
    return '\u3400' <= ch <= '\u9fff'


@lru_cache(maxsize=4096)
def _readings(ch):
    """Every toneless pinyin reading of one character (empty without pypinyin)."""
    try:
        from pypinyin import Style, pinyin
    except ImportError:
        return frozenset()
    return frozenset(pinyin(ch, style=Style.NORMAL, heteronym=True)[0])


@lru_cache(maxsize=512)
def _phrase_pinyin(phrase):
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        return None
    return tuple(lazy_pinyin(phrase, style=Style.NORMAL))


def restore_phrases(text, phrases):
    for phrase in phrases:
        if all(_cjk(ch) for ch in phrase):
            sound = _phrase_pinyin(phrase)
            if not sound or len(sound) != len(phrase):
                continue
            out, i, n = [], 0, len(phrase)
            while i < len(text):
                window = text[i:i + n]
                if (len(window) == n and window != phrase and all(_cjk(ch) for ch in window)
                        and all(s in _readings(ch) for s, ch in zip(sound, window))):
                    out.append(phrase)
                    i += n
                else:
                    out.append(text[i])
                    i += 1
            text = ''.join(out)
        elif phrase.isascii() and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 .+\-]*", phrase):
            text = re.sub(r'(?<![A-Za-z0-9])' + re.escape(phrase) + r'(?![A-Za-z0-9])', phrase, text, flags=re.IGNORECASE)
    return text


class OpenLessSettings:
    def __init__(self, folder=None):
        folder = Path(folder) if folder else openless_dir()
        self._mode = _Cached(folder / 'preferences.json', _mode)
        self._rules = _Cached(folder / 'correction-rules.json', _rules)
        self._phrases = _Cached(folder / 'dictionary.json', _phrases)
        self._hotwords = _Cached(folder / 'dictionary.json', _hotwords)

    def mode(self):
        """'raw', 'light', 'structured', … or None when OpenLess is not installed."""
        return self._mode.get()

    def hotwords(self):
        """The dictionary's enabled phrases, most useful first ([] without OpenLess)."""
        return list(self._hotwords.get())

    def correct(self, text):
        text = restore_phrases(text, self._phrases.get())
        for pattern, replacement in self._rules.get():
            text = text.replace(pattern, replacement)
        return text


# -- typing --------------------------------------------------------------------------
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x2
KEYEVENTF_UNICODE = 0x4
BATCH = 120  # characters per SendInput call; a short pause between batches keeps slow apps in step


class _KEYBDINPUT(c.Structure):
    _fields_ = [('wVk', w.WORD), ('wScan', w.WORD), ('dwFlags', w.DWORD), ('time', w.DWORD),
                ('dwExtraInfo', c.c_size_t)]


class _MOUSEINPUT(c.Structure):
    _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('mouseData', w.DWORD), ('dwFlags', w.DWORD),
                ('time', w.DWORD), ('dwExtraInfo', c.c_size_t)]


class _INPUTUNION(c.Union):
    _fields_ = [('ki', _KEYBDINPUT), ('mi', _MOUSEINPUT)]


class _INPUT(c.Structure):
    _fields_ = [('type', w.DWORD), ('u', _INPUTUNION)]


def key_events(text):
    """UTF-16 text strokes only. Inline line breaks as spaces so dictation cannot submit;
    the result and saved note still retain the original multiline text."""
    events = []
    for ch in text.replace('\r\n', '\n').replace('\r', '\n').replace('\n', ' '):
        data = ch.encode('utf-16-le')
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], 'little')
            events += [(0, unit, KEYEVENTF_UNICODE), (0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return events


class PartlyTyped(OSError):
    """Some strokes went in before Windows refused the rest (UAC, lock screen…)."""


def type_text(text, sender=None, pause=time.sleep):
    """Type text into the focused window without touching the clipboard. OSError if
    nothing went in; PartlyTyped if any strokes went in (do not retype it all)."""
    events = key_events(text)
    send = sender or _send_input
    for start in range(0, len(events), BATCH * 2):
        try:
            send(events[start:start + BATCH * 2])
        except OSError as error:
            if start:
                raise PartlyTyped(str(error)) from error
            raise
        if start + BATCH * 2 < len(events):
            pause(.008)


def _send_input(events):
    array = (_INPUT * len(events))()
    for item, (vk, scan, flags) in zip(array, events):
        item.type = INPUT_KEYBOARD
        item.u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    sent = c.windll.user32.SendInput(len(events), array, c.sizeof(_INPUT))
    if sent != len(events):
        error = PartlyTyped if sent else OSError
        raise error('SendInput 被系统拒绝（目标可能是管理员权限窗口）')
