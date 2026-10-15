"""bridge.housekeeping: only allow-listed leftovers go, everything else stays (temp folders only)."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bridge import housekeeping
from bridge.housekeeping import clean_runtime

CUES = ('armed.wav', 'stop.wav', 'tool.wav')
# Files a user or DotVoice keeps: none of them may ever be removed.
PROTECTED = (
    'prefs.json', 'prefs-backup.json', 'smoke-prefs.json', 'bindings.json', 'bindings-pv60.json',
    'usage.json', 'usage-pv60.json', 'smoke-usage.json', 'instances.json', 'secrets.json',
    'status.json', 'devices.json', 'key-log.txt', 'sound-log.txt', 'openless-live-text.json',
    'cursor-hidden', 'tray-error.log', 'key-probe-20261004-030713.jsonl', 'smoke-capsule.bmp.txt',
    'notes/2026-10-14.md', 'notes/trash/old.md', 'smoke-notes/a.md', 'diagnostics/last.wav',
    'smoke/runtime/ui-smoke.json', 'smoke/runtime/smoke-capsule-1-chip.bmp', 'smoke/runtime/core.log',
    'smoke/runtime/sounds/v1-medium/armed.wav', 'sounds/README.txt', 'sounds/v1-medium.wav',
    'sounds/custom/armed.wav', 'sounds/vX-medium/armed.wav', 'sounds/v2/armed.wav',
    'sounds/v3-medium/armed.wav', 'sounds/v3-medium-tight/armed.wav', 'sounds/v4-low/armed.wav',
)


class CleanRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.runtime = self.root / 'runtime'
        self.runtime.mkdir()

    def make(self, *names, data=b'x'):
        for name in names:
            path = self.runtime / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return [self.runtime / name for name in names]

    def cache(self, folder, names=CUES):
        return self.make(*('sounds/%s/%s' % (folder, name) for name in names))

    def files(self):
        return sorted(p.relative_to(self.runtime).as_posix() for p in self.runtime.rglob('*') if p.is_file())

    def test_removes_old_versions_unused_volumes_and_smoke_leftovers_only(self):
        self.make(*PROTECTED)
        self.make('core-error.log', data=b'traceback')
        for folder in ('v1-medium', 'v1-low', 'v2-medium', 'v2-medium-tight', 'v2-high',
                       'v3-low', 'v3-low-tight', 'v3-high', 'v3-high-tight'):
            self.cache(folder)
        self.make('sounds/v3-low/armed.12345.tmp')
        self.make('smoke-capsule-1-chip.bmp', 'smoke-main-glass-red.bmp', 'SMOKE-main-dark.BMP', 'ui-smoke.json')
        self.make('core.log', data=b'')

        removed = clean_runtime(self.runtime, 3, {'medium'})

        expected = {self.runtime / 'sounds' / f for f in (
            'v1-medium', 'v1-low', 'v2-medium', 'v2-medium-tight', 'v2-high',
            'v3-low', 'v3-low-tight', 'v3-high', 'v3-high-tight')}
        expected |= {self.runtime / f for f in (
            'smoke-capsule-1-chip.bmp', 'smoke-main-glass-red.bmp', 'SMOKE-main-dark.BMP',
            'ui-smoke.json', 'core.log')}
        self.assertEqual(set(removed), expected)
        self.assertEqual(len(removed), len(expected), 'each item is reported once')
        self.assertEqual(self.files(), sorted(list(PROTECTED) + ['core-error.log']))
        self.assertFalse((self.runtime / 'sounds' / 'v1-medium').exists())
        self.assertTrue((self.runtime / 'sounds' / 'v3-medium').is_dir())

    def test_matches_the_real_cue_cache_names(self):
        from bridge.sounds import SOUND_VERSION, VOLUMES, Sounds
        cues = Sounds(self.runtime / 'sounds', lambda: {'volume': 'medium'})
        for volume in VOLUMES:
            for tight in (False, True):
                path = cues._file('armed', volume, tight)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b'RIFF')
        kept = {cues._file('armed', 'medium', tight).parent for tight in (False, True)}
        removed = clean_runtime(self.runtime, SOUND_VERSION, {'medium'})
        self.assertEqual(len(removed), 2 * (len(VOLUMES) - 1))
        self.assertEqual({p for p in (self.runtime / 'sounds').iterdir()}, kept)

    def test_nothing_to_do_changes_nothing(self):
        self.make(*PROTECTED)
        before = self.files()
        self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [])
        self.assertEqual(self.files(), before)
        # Run twice: the second run finds nothing.
        self.cache('v2-medium')
        self.assertEqual(len(clean_runtime(self.runtime, 3, {'medium'})), 1)
        self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [])

    def test_kept_volumes_and_newer_versions_stay(self):
        self.cache('v3-low')
        self.cache('v3-high-tight')
        self.cache('v4-medium')
        self.cache('v3-Medium')
        removed = clean_runtime(self.runtime, 3, {'low', 'high'})
        self.assertEqual(removed, [self.runtime / 'sounds' / 'v3-Medium'])
        self.assertTrue((self.runtime / 'sounds' / 'v3-low').is_dir())
        self.assertTrue((self.runtime / 'sounds' / 'v3-high-tight').is_dir())
        self.assertTrue((self.runtime / 'sounds' / 'v4-medium').is_dir(), 'a newer DotVoice may run here')

    def test_a_single_volume_string_is_one_volume(self):
        self.cache('v3-medium')
        self.cache('v3-low')
        removed = clean_runtime(self.runtime, 3, 'medium')
        self.assertEqual(removed, [self.runtime / 'sounds' / 'v3-low'])
        self.assertTrue((self.runtime / 'sounds' / 'v3-medium').is_dir())

    def test_upper_case_folder_of_a_kept_volume_stays(self):
        self.cache('V3-MEDIUM')
        self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [])

    def test_cache_folder_with_foreign_files_keeps_them(self):
        wavs = self.cache('v2-medium')
        self.make('sounds/v2-medium/my-notes.txt')
        self.make('sounds/v2-medium/sub/armed.wav')
        removed = clean_runtime(self.runtime, 3, {'medium'})
        self.assertEqual(set(removed), set(wavs), 'reported as the files that went')
        self.assertEqual(self.files(), ['sounds/v2-medium/my-notes.txt', 'sounds/v2-medium/sub/armed.wav'])

    def test_folders_and_files_with_allow_listed_names_but_wrong_kind_stay(self):
        (self.runtime / 'smoke-capsule.bmp').mkdir()
        (self.runtime / 'ui-smoke.json').mkdir()
        (self.runtime / 'core.log').mkdir()
        self.make('sounds/v1-medium.wav', 'sounds/v2-low.tmp')
        self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [])
        for name in ('smoke-capsule.bmp', 'ui-smoke.json', 'core.log'):
            self.assertTrue((self.runtime / name).is_dir())
        self.assertEqual(self.files(), ['sounds/v1-medium.wav', 'sounds/v2-low.tmp'])

    def test_non_empty_core_log_stays(self):
        self.make('core.log', data=b'Traceback (most recent call last)')
        self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [])
        self.assertEqual(self.files(), ['core.log'])

    def test_missing_folders_return_nothing(self):
        self.assertEqual(clean_runtime(self.root / 'absent', 3, {'medium'}), [])
        self.make('ui-smoke.json')
        self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [self.runtime / 'ui-smoke.json'])

    def test_permission_errors_leave_that_item_and_go_on(self):
        self.make('smoke-a.bmp', 'smoke-b.bmp', 'ui-smoke.json')
        self.cache('v1-medium')
        self.cache('v2-medium')
        blocked = {str(self.runtime / 'smoke-a.bmp'), str(self.runtime / 'sounds' / 'v1-medium' / 'stop.wav')}
        real_remove = os.remove

        def remove(path):
            if str(path) in blocked:
                raise PermissionError(13, 'Access is denied', str(path))
            real_remove(path)

        with patch.object(housekeeping.os, 'remove', side_effect=remove):
            removed = clean_runtime(self.runtime, 3, {'medium'})
        v1 = self.runtime / 'sounds' / 'v1-medium'
        self.assertEqual(set(removed), {self.runtime / 'smoke-b.bmp', self.runtime / 'ui-smoke.json',
                                        v1 / 'armed.wav', v1 / 'tool.wav', self.runtime / 'sounds' / 'v2-medium'})
        self.assertEqual(self.files(), ['smoke-a.bmp', 'sounds/v1-medium/stop.wav'])

    def test_unreadable_folders_are_skipped(self):
        self.make('ui-smoke.json')
        self.cache('v1-medium')
        real_scandir = os.scandir

        def scandir(path):
            if Path(path).name == 'sounds':
                raise PermissionError(13, 'Access is denied', str(path))
            return real_scandir(path)

        with patch.object(housekeeping.os, 'scandir', side_effect=scandir):
            self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [self.runtime / 'ui-smoke.json'])
        with patch.object(housekeeping.os, 'scandir', side_effect=PermissionError(13, 'Access is denied')):
            self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [])
        self.assertTrue((self.runtime / 'sounds' / 'v1-medium' / 'armed.wav').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows file sharing')
    def test_files_in_use_stay(self):
        self.make('core.log', data=b'')
        self.cache('v1-medium')
        with open(self.runtime / 'core.log', 'ab'), open(self.runtime / 'sounds' / 'v1-medium' / 'armed.wav', 'rb'):
            removed = clean_runtime(self.runtime, 3, {'medium'})
        v1 = self.runtime / 'sounds' / 'v1-medium'
        self.assertEqual(set(removed), {v1 / 'stop.wav', v1 / 'tool.wav'})
        self.assertEqual(self.files(), ['core.log', 'sounds/v1-medium/armed.wav'])
        # Closed again: the empty log goes now.
        self.assertEqual(set(clean_runtime(self.runtime, 3, {'medium'})), {self.runtime / 'core.log', v1})

    @unittest.skipUnless(os.name == 'nt', 'directory junctions are Windows-only')
    def test_junctions_are_never_followed_or_removed(self):
        import _winapi
        outside = self.root / 'elsewhere'
        outside.mkdir()
        (outside / 'armed.wav').write_bytes(b'x')
        (outside / 'ui-smoke.json').write_bytes(b'{}')
        (self.runtime / 'sounds').mkdir()
        _winapi.CreateJunction(str(outside), str(self.runtime / 'sounds' / 'v1-medium'))
        self.addCleanup(lambda: os.path.lexists(self.runtime / 'sounds' / 'v1-medium')
                        and os.rmdir(self.runtime / 'sounds' / 'v1-medium'))
        self.assertEqual(clean_runtime(self.runtime, 3, {'medium'}), [])
        self.assertTrue((outside / 'armed.wav').exists())
        self.assertTrue(os.path.lexists(self.runtime / 'sounds' / 'v1-medium'))


if __name__ == '__main__':
    unittest.main()
