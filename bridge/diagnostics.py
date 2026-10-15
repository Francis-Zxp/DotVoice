"""Opt-in debug recordings for tuning the 气声 front end (prefs diagnostics.save_audio).

Off by default: nothing here writes anything unless the switch is on. Then each
recording leaves three files in runtime/diagnostics:

  <stamp>-raw.wav   what the microphone sent (16 kHz mono 16-bit)
  <stamp>-sent.wav  what went to the speech engine, after bridge.voice_dsp
  <stamp>.json      the front end's numbers (WhisperDSP.stats()), engine, mode, outcome

Only the newest KEEP recordings are kept and none older than MAX_AGE: checked when
DotVoice starts, hourly, and whenever a recording begins or is written. Switching it off
deletes them at once (clear). Nothing is uploaded or sent anywhere; the files stay on
this PC until they expire.
"""
import json
from pathlib import Path
import re
import threading
import time
import wave

KEEP = 3
MAX_AGE = 24 * 3600
RATE = 16000
MAX_SECONDS = 300           # per file: a longer recording keeps its first five minutes
MAX_BYTES = MAX_SECONDS * RATE * 2
TEXT_LIMIT = 2000
# Only files of exactly this shape are ever deleted (on expiry or rotation).
NAME = re.compile(r'^(\d{8}-\d{6}-\d{3})(?:-raw\.wav|-sent\.wav|\.json)$')
KINDS = ('raw', 'sent')


def _unlink(path):
    try:
        path.unlink()
    except OSError:
        pass


def _writer(path):
    writer = wave.open(str(path), 'wb')
    writer.setnchannels(1)
    writer.setsampwidth(2)
    writer.setframerate(RATE)
    return writer


class AudioDiagnostics:
    """begin / raw / sent / finish / abort come from the controller thread, purge() also
    from the core's start and ticker. A full or failing disk never disturbs a recording:
    the debug copy is dropped instead."""

    def __init__(self, folder, enabled=lambda: False, clock=time.time):
        self.folder = Path(folder)
        self.enabled = enabled
        self.clock = clock
        self.context = {}  # added to every JSON (the core puts its version here)
        self.lock = threading.RLock()
        self.session = None

    def _on(self):
        try:
            return bool(self.enabled())
        except Exception:
            return False

    # -- one recording ---------------------------------------------------------------
    def begin(self, info=None):
        """A recording starts: open its two WAV files, only when the switch is on."""
        with self.lock:
            self._drop()
            if not self._on():
                return False
            now = self.clock()
            paths, writers = {}, {}
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                self._purge(KEEP - 1, now)  # room for this one
                stamp = self._stamp(now)
                for kind in KINDS:
                    paths[kind] = self.folder / f'{stamp}-{kind}.wav'
                    writers[kind] = _writer(paths[kind])
            except (OSError, wave.Error):
                self._discard({'paths': paths, 'writers': writers})
                return False
            self.session = {'stamp': stamp, 'paths': paths, 'writers': writers,
                            'bytes': dict.fromkeys(KINDS, 0), 'truncated': False,
                            'info': dict(info or {}), 'started': now}
            return True

    def raw(self, pcm):
        """What the microphone sent (before the 气声 front end)."""
        self._write('raw', pcm)

    def sent(self, pcm):
        """What went to the speech engine (after the 气声 front end)."""
        self._write('sent', pcm)

    def _write(self, kind, pcm):
        with self.lock:
            session = self.session
            if session is None or not pcm:
                return
            room = MAX_BYTES - session['bytes'][kind]
            if len(pcm) > room:
                session['truncated'] = True
                pcm = pcm[:max(0, room - room % 2)]
                if not pcm:
                    return
            try:
                session['writers'][kind].writeframes(pcm)  # header patched as it grows
            except (OSError, wave.Error, ValueError):
                self._drop()
                return
            session['bytes'][kind] += len(pcm)

    def finish(self, outcome, details=None):
        """The recording ended: close its files and write the numbers beside them.
        Returns the JSON path, or None when nothing was kept (switched off meanwhile)."""
        with self.lock:
            session, self.session = self.session, None
            if session is None:
                return None
            if not self._close(session) or not self._on():
                self._discard(session)
                return None
            now = self.clock()
            record = {**self.context, **session['info'], 'outcome': outcome,
                      'started': round(session['started'], 3), 'ended': round(now, 3),
                      **{f'{kind}_seconds': round(session['bytes'][kind] / (2 * RATE), 3) for kind in KINDS},
                      'truncated': session['truncated'],
                      'files': {kind: session['paths'][kind].name for kind in KINDS}}
            record.update(details or {})
            if isinstance(record.get('text'), str):
                record['text'] = record['text'][:TEXT_LIMIT]
            path = self.folder / (session['stamp'] + '.json')
            try:
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
            except (OSError, TypeError, ValueError):
                self._discard(session)
                _unlink(path)
                return None
            self._purge(KEEP, now)
            return path

    def abort(self):
        """No recording after all (a refused press, an unfinished session): its files go."""
        with self.lock:
            self._drop()

    def _drop(self):
        session, self.session = self.session, None
        if session is not None:
            self._close(session)
            self._discard(session)

    @staticmethod
    def _close(session):
        ok = True
        for writer in session['writers'].values():
            try:
                writer.close()
            except (OSError, wave.Error, ValueError):
                ok = False
        return ok

    @staticmethod
    def _discard(session):
        for writer in session['writers'].values():
            try:
                writer.close()
            except Exception:
                pass
        for path in session['paths'].values():
            _unlink(path)

    def _stamp(self, now):
        base = time.strftime('%Y%m%d-%H%M%S', time.localtime(now))
        millis = int(now * 1000) % 1000
        for step in range(1000):
            stamp = '%s-%03d' % (base, min(999, millis + step))
            if not any((self.folder / f'{stamp}{suffix}').exists() for suffix in ('-raw.wav', '-sent.wav', '.json')):
                break
        return stamp

    # -- expiry ------------------------------------------------------------------------
    def clear(self):
        """Switched off: the recording being written is dropped and every kept one deleted
        now, not after 24 hours. Only files of the debug recordings' own shape go."""
        with self.lock:
            self._drop()
            self._purge(0, self.clock())

    def purge(self):
        """Delete debug recordings older than MAX_AGE and all but the newest KEEP."""
        with self.lock:
            self._purge(KEEP, self.clock())

    def _purge(self, keep, now):
        try:
            entries = list(self.folder.iterdir())
        except OSError:
            return  # no folder yet: nothing to delete
        current = self.session['stamp'] if self.session else None
        groups = {}
        for path in entries:
            match = NAME.match(path.name)
            if not match or match.group(1) == current:
                continue
            try:
                if not path.is_file():
                    continue
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age > MAX_AGE:
                _unlink(path)
            else:
                groups.setdefault(match.group(1), []).append(path)
        for stamp in sorted(groups, reverse=True)[max(0, keep):]:
            for path in groups[stamp]:
                _unlink(path)
