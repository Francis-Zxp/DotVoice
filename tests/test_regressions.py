"""Cancellation, telemetry and build-script regressions; no hardware or audio service is used, and
build.ps1 only has its functions run on a fake dist tree in a temp folder (no build, no Recycle Bin)."""
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.controller import Controller
from bridge.usage import UsageLedger

try:
    from .test_bridge import FakeBackend, FakeCapture, wait_for
    from .core_fakes import make_core
except ImportError:  # unittest discover -s tests imports modules without a package.
    from test_bridge import FakeBackend, FakeCapture, wait_for
    from core_fakes import make_core

ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which('powershell.exe')   # Windows PowerShell 5.1, which runs build.ps1


class RegressionTests(unittest.TestCase):
    def test_cancel_or_disarm_during_start_ack_discards_all_pending_pcm(self):
        for action in ('cancel', 'disarm'):
            with self.subTest(action=action):
                backend = FakeBackend()
                control = Controller(lambda *_: None, backend, FakeCapture)
                try:
                    control.command('arm')
                    wait_for(lambda: control.phase == 'idle')
                    # Cover both pre-roll and audio queued while start awaits ACK.
                    control._audio(b'\x01\x00' * 800)
                    backend.gate = threading.Event()
                    control.command('start')
                    wait_for(lambda: 'start' in backend.calls)
                    control._audio(b'\x02\x00' * 800)
                    control.command(action)
                    control._audio(b'\x03\x00' * 800)
                    backend.gate.set()
                    wait_for(lambda: 'cancel' in backend.calls)
                    self.assertFalse(any(isinstance(call, tuple)
                                         for call in backend.calls),
                                     'Cancelled pre-roll/queued audio was sent after ACK')
                    self.assertNotIn('stop', backend.calls)

                    # A new deliberate recording must work without stale audio.
                    control.command('arm')
                    wait_for(lambda: control.phase == 'idle')
                    control.command('start')
                    wait_for(lambda: control.phase == 'recording')
                    fresh = b'\x04\x00' * 800
                    control._audio(fresh)
                    wait_for(lambda: ('pcm', fresh) in backend.calls)
                    self.assertEqual([call[1] for call in backend.calls
                                      if isinstance(call, tuple)], [fresh])
                finally:
                    if backend.gate:
                        backend.gate.set()
                    control.close()

    def test_usage_write_failure_does_not_stop_voice_controller(self):
        # A real Core with fake HID/audio/sounds; never opens a device.
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        backend = FakeBackend()
        desktop = make_core(Path(folder.name), backend)
        self.addCleanup(desktop.close)
        with patch.object(Path, 'exists', return_value=False):
            # Inside the test's temp folder, never the project's runtime/ (cwd-relative).
            desktop.ledger = UsageLedger(Path(folder.name) / 'regression-no-write.json')
        desktop.ledger.last_save -= 31
        thread_errors = []
        with patch.object(desktop.ledger, 'save', side_effect=OSError('disk full')), \
                patch.object(threading, 'excepthook',
                             side_effect=lambda error: thread_errors.append(error.exc_value)):
            control = desktop.controller
            try:
                control.command('arm')
                wait_for(lambda: desktop.state['phase'] == 'idle' or not control.thread.is_alive())
                self.assertTrue(control.thread.is_alive(),
                                f'Telemetry write failure killed controller: {thread_errors}')
                self.assertEqual(desktop.state['phase'], 'idle')
                self.assertTrue(desktop.ledger.warning)

                control.command('start')
                wait_for(lambda: control.phase == 'recording')
                control._audio(b'\x01\x00' * 800)
                control.command('stop')
                wait_for(lambda: control.phase == 'processing')
                backend.incoming.put({'type': 'result', 'text': '假设备回归测试'})
                wait_for(lambda: desktop.state['phase'] == 'idle')
                self.assertEqual(desktop.result['text'], '假设备回归测试')
                self.assertEqual(desktop.ledger.snapshot()['totals']['sessions'], 1)
                self.assertTrue(control.thread.is_alive())
                self.assertFalse(thread_errors)
            finally:
                control.close()



def build_functions(commands, env):
    """Defines build.ps1's functions, and nothing else of it runs; the Recycle Bin becomes a move into
    DV_BIN (or a failure when DV_FAIL is set). Then runs `commands` under 'Stop'."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        '$tokens = $null; $errors = $null',
        '$ast = [Management.Automation.Language.Parser]::ParseFile($env:DV_BUILD, [ref]$tokens, [ref]$errors)',
        'if ($errors.Count) { $errors | Out-String; exit 3 }',
        '$found = $ast.FindAll({ param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)',
        'foreach ($f in $found) { . ([scriptblock]::Create($f.Extent.Text)) }',
        'function Remove-ToRecycleBin([string]$Folder) {',
        "    if ($env:DV_FAIL) { throw 'recycle failed' }",
        '    Move-Item -LiteralPath $Folder -Destination (Join-Path $env:DV_BIN (Split-Path -Leaf $Folder))',
        '}',
    ] + commands
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / 'snippet.ps1'
        script.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8-sig')
        return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                               '-File', str(script)], capture_output=True, timeout=120,
                              env=dict(os.environ, DV_BUILD=str(ROOT / 'build.ps1'), **env))


def make_files(base, *names):
    for name in names:
        (base / name).parent.mkdir(parents=True, exist_ok=True)
        (base / name).write_text(name, encoding='utf-8')


def files_under(base):
    return sorted(p.relative_to(base).as_posix() for p in base.rglob('*') if p.is_file())


@unittest.skipUnless(POWERSHELL, 'needs Windows PowerShell 5.1')
class BuildScriptTests(unittest.TestCase):
    """A portable copy run from dist\\<ver>\\DotVoice keeps its prefs, encrypted key and notes in
    DotVoice\\runtime: a rebuild must carry them over and a newer version's build must leave them."""

    PORTABLE = ('DotVoice/runtime/prefs.json', 'DotVoice/runtime/secrets.json', 'DotVoice/runtime/notes/n1.md',
                'DotVoice/runtime/smoke/runtime/ui-smoke.json')

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.dist, self.backups, self.bin = self.root / 'dist', self.root / 'backups', self.root / 'bin'
        self.bin.mkdir()

    def clear(self, version, fail=False):
        env = {'DV_OUT': str(self.dist / version), 'DV_BACKUPS': str(self.backups), 'DV_BIN': str(self.bin)}
        if fail:
            env['DV_FAIL'] = '1'
        return build_functions(['Clear-BuildOutput $env:DV_OUT $env:DV_BACKUPS', "Write-Output 'cleared'"], env)

    def test_rebuild_of_the_same_version_keeps_the_portable_data(self):
        out = self.dist / '0.11.3'
        make_files(out, 'DotVoice/DotVoice.exe', 'DotVoice/core/dotvoice-core.exe', 'core-build/x.txt', *self.PORTABLE)
        result = self.clear('0.11.3')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b'cleared', result.stdout)
        self.assertEqual(files_under(out), sorted(self.PORTABLE))
        self.assertEqual(files_under(self.bin), ['0.11.3/DotVoice/DotVoice.exe', '0.11.3/DotVoice/core/dotvoice-core.exe',
                                                 '0.11.3/core-build/x.txt'], 'only build output is recycled')
        self.assertEqual(list(self.backups.iterdir()), [], 'the data was put back, not left aside')

    def test_a_failed_recycle_still_puts_the_portable_data_back(self):
        out = self.dist / '0.11.3'
        make_files(out, 'DotVoice/DotVoice.exe', *self.PORTABLE)
        result = self.clear('0.11.3', fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'recycle failed', result.stdout + result.stderr)
        self.assertEqual(files_under(out), sorted(self.PORTABLE + ('DotVoice/DotVoice.exe',)))
        self.assertEqual(list(self.backups.iterdir()), [])

    def test_a_copy_running_from_the_folder_stops_the_build_before_anything_moves(self):
        out = self.dist / '0.11.3'
        make_files(out, 'DotVoice/DotVoice.exe', *self.PORTABLE)
        before = files_under(out)
        # Stands in for Get-Process: a DotVoice.exe of that folder is running.
        result = build_functions(["function Get-Process { [pscustomobject]@{ Path = $env:DV_EXE } }",
                                  'Clear-BuildOutput $env:DV_OUT $env:DV_BACKUPS'],
                                 {'DV_OUT': str(out), 'DV_BACKUPS': str(self.backups), 'DV_BIN': str(self.bin),
                                  'DV_EXE': str(out / 'DotVoice' / 'DotVoice.exe')})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b'close it before building', result.stdout + result.stderr)
        self.assertEqual(files_under(out), before)
        self.assertFalse(self.backups.exists())
        self.assertEqual(list(self.bin.iterdir()), [])

    def test_smoke_output_alone_is_not_kept(self):
        out = self.dist / '0.11.3'
        make_files(out, 'DotVoice/DotVoice.exe', 'DotVoice/runtime/smoke/runtime/ui-smoke.json')
        result = self.clear('0.11.3')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(out.exists())
        self.assertFalse(self.backups.exists())

    def test_a_new_version_leaves_older_portable_data_where_it_is(self):
        make_files(self.dist, '0.11.4/DotVoice/DotVoice.exe', '0.11.3/DotVoice/DotVoice.exe',
                   *('0.11.3/' + name for name in self.PORTABLE), '0.11.2/DotVoice/runtime/notes/n0.md',
                   '0.11.1/DotVoice/DotVoice.exe', '0.11.1/DotVoice/runtime/smoke/runtime/ui-smoke.json',
                   '0.11.0/DotVoice/DotVoice.exe', 'notes/keep.md')
        before = files_under(self.dist)
        result = build_functions(['Remove-OldBuilds $env:DV_DIST $env:DV_OUT', "Write-Output 'done'"],
                                 {'DV_DIST': str(self.dist), 'DV_OUT': str(self.dist / '0.11.4'), 'DV_BIN': str(self.bin)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b'done', result.stdout)
        self.assertEqual(sorted(p.name for p in self.dist.iterdir()), ['0.11.2', '0.11.3', '0.11.4', 'notes'])
        self.assertEqual(sorted(p.name for p in self.bin.iterdir()), ['0.11.0', '0.11.1'])
        self.assertEqual(files_under(self.dist), [name for name in before if not name.startswith(('0.11.0/', '0.11.1/'))])
        self.assertIn(b'0.11.3', result.stdout + result.stderr)   # the kept folders are named in a warning

    def test_build_script_clears_through_the_guarded_functions_only(self):
        data = (ROOT / 'build.ps1').read_bytes()
        self.assertTrue(all(byte < 128 for byte in data))
        self.assertEqual(data.count(b'\n'), data.count(b'\r\n'))
        lines = data.decode('ascii').splitlines()
        code = [line for line in lines if not line.lstrip().startswith('#')]
        self.assertFalse([line for line in code if 'Remove-Item' in line and '$output' in line.split('Remove-Item')[1]],
                         'dist\\<ver> is never removed outright')
        self.assertEqual(len([line for line in code if 'DeleteDirectory' in line]), 1,
                         'the Recycle Bin is reached through Remove-ToRecycleBin only')
        index = lambda text: next(i for i, line in enumerate(lines) if line.startswith(text))
        self.assertLess(index('Clear-BuildOutput $output'), index('# 1. Python core'))
        self.assertLess(index('Write-Output "Built $app"'), index('Remove-OldBuilds $distRoot $output'))


if __name__ == '__main__':
    unittest.main()
