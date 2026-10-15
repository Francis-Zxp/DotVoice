"""Local voice notes. Conservative cleanup; keep the exact backend return.

Notes carry free tags instead of a category chosen before recording. A new
note has no tag (the inbox); older notes keep their 0.6 category as a tag.
"""
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

TRASH_DAYS = 30
LEGACY_TAGS = {'idea': '灵感', 'paper': '论文'}


def clean_note(text):
    # Only standalone hesitation sounds. Never drop qualifiers such as
    # '可能', negation, repetitions, scientific numbers or technical tokens.
    text = re.sub(r'(^|[，,。！？!?；;\n]\s*)[嗯呃额]+(?:[，,、]\s*|(?=[。！？!?；;\n]|$))', r'\1', text.strip())
    text = re.sub(r'^[，,、。\s]+', '', text)
    text = re.sub(r'([。！？!?])\s*(?=\S)', r'\1\n', text)
    return '\n'.join(line.strip() for line in text.splitlines() if line.strip())


def _title(text):
    return (text.splitlines() or ['新想法'])[0][:48]


def clean_tags(tags):
    if not isinstance(tags, list):
        raise ValueError('标签格式无效')
    result = []
    for tag in tags:
        if not isinstance(tag, str):
            raise ValueError('标签格式无效')
        tag = tag.strip()[:12]
        if tag and tag not in result:
            result.append(tag)
    if len(result) > 8:
        raise ValueError('一条笔记最多 8 个标签')
    return result


def _normalize(note):
    if not isinstance(note.get('tags'), list):
        legacy = LEGACY_TAGS.get(note.get('category'))
        note['tags'] = [legacy] if legacy else []
    return note


class NoteStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.trash = self.directory / '.trash'
        self.lock = threading.RLock()

    def _path(self, identifier, folder=None):
        if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise ValueError('笔记编号无效')
        return (folder or self.directory) / (identifier + '.json')

    def _save(self, note):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(note['id'])
        temp = target.with_suffix('.tmp')
        temp.write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(target)
        return note

    def _load(self, identifier):
        return _normalize(json.loads(self._path(identifier).read_text(encoding='utf-8')))

    def add(self, text, tags=None, duration=None):
        if not isinstance(text, str) or not text.strip():
            return None
        polished = clean_note(text)
        note = {'id': uuid.uuid4().hex, 'created': time.time(), 'tags': clean_tags(tags or []),
                'title': _title(polished), 'source': text, 'text': polished,
                'cleanup': '沿用 OpenLess 当前配置；本地去除独立语气词并分段'}
        if isinstance(duration, (int, float)) and duration >= 0:
            note['duration'] = round(float(duration), 1)
        with self.lock:
            return self._save(note)

    def list(self):
        with self.lock:
            notes = []
            unreadable = 0
            for path in self.directory.glob('*.json'):
                try:
                    item = json.loads(path.read_text(encoding='utf-8'))
                    if (self._path(item['id']) != path or not all(isinstance(item[k], str) for k in ('text', 'source', 'title'))
                            or not isinstance(item['created'], (int, float))):
                        raise ValueError('Invalid note')
                    notes.append(_normalize(item))
                except (OSError, ValueError, KeyError, TypeError):
                    unreadable += 1
            tags = sorted({t for n in notes for t in n['tags']})
            return {'items': sorted(notes, key=lambda n: n['created'], reverse=True), 'tags': tags,
                    'warning': f'{unreadable} 条笔记暂时无法读取，原文件已保留。' if unreadable else ''}

    def update(self, identifier, text):
        if not isinstance(text, str) or not text.strip():
            raise ValueError('整理稿不能为空')
        with self.lock:
            note = self._load(identifier)
            note['text'] = text.strip()
            note['updated'] = time.time()
            return self._save(note)

    def set_tags(self, identifier, tags):
        tags = clean_tags(tags)
        with self.lock:
            note = self._load(identifier)
            note['tags'] = tags
            return self._save(note)

    def delete(self, identifier):
        """Move to notes/.trash so the deletion can be undone."""
        with self.lock:
            source = self._path(identifier)
            if not source.exists():
                raise ValueError('笔记不存在或已删除')
            self.trash.mkdir(parents=True, exist_ok=True)
            target = self._path(identifier, self.trash)
            source.replace(target)
            os.utime(target)  # Trash age counts from deletion, not creation.
            return {'id': identifier}

    def restore(self, identifier):
        with self.lock:
            source = self._path(identifier, self.trash)
            if not source.exists():
                raise ValueError('回收站中没有这条笔记')
            target = self._path(identifier)
            if target.exists():
                raise ValueError('同名笔记已存在，未覆盖')
            self.directory.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            return _normalize(json.loads(target.read_text(encoding='utf-8')))

    def purge_trash(self, days=TRASH_DAYS, now=None):
        """Remove notes deleted more than `days` ago from this app's own trash."""
        cutoff = (now or time.time()) - days * 86400
        removed = 0
        with self.lock:
            for path in self.trash.glob('*.json'):
                try:
                    if path.stat().st_mtime < cutoff:
                        path.unlink()
                        removed += 1
                except OSError:
                    pass
        return removed
