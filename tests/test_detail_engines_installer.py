"""Installer details: the uninstaller survives Windows PowerShell 5.1 turning schtasks' stderr into a
terminating error, an upgrade deletes its backup once it succeeded, and an uninstall removes the
folders it leaves empty. Nothing is installed or removed: the scripts are read and parsed, and only
snippets run (a schtasks query of a task that does not exist, folder cleanup in a temp directory)."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

INSTALLER = Path(__file__).resolve().parent.parent / 'installer'
POWERSHELL = shutil.which('powershell.exe')   # Windows PowerShell 5.1, which UninstallString runs


def lines_of(name):
    return (INSTALLER / name).read_bytes().decode('utf-8-sig').splitlines()


def index_of(lines, fragment, start=0):
    return next(i for i in range(start, len(lines)) if fragment in lines[i])


def run_snippet(lines, env=None):
    with tempfile.TemporaryDirectory() as folder:
        script = Path(folder) / 'snippet.ps1'
        script.write_text('\r\n'.join(lines) + '\r\n', encoding='utf-8-sig')
        return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                               '-File', str(script)], capture_output=True, timeout=120,
                              env=dict(os.environ, **(env or {})))


class InstallerScriptTests(unittest.TestCase):
    def test_scripts_keep_bom_and_crlf(self):
        for name in ('install.ps1', 'uninstall.ps1'):
            data = (INSTALLER / name).read_bytes()
            self.assertTrue(data.startswith(b'\xef\xbb\xbf'), name)
            self.assertEqual(data.count(b'\n'), data.count(b'\r\n'), name)

    def test_every_schtasks_call_runs_under_continue(self):
        current, saved, calls = None, None, 0
        for line in lines_of('uninstall.ps1'):
            for statement in (part.strip() for part in line.split(';')):
                assigned = re.match(r"\$ErrorActionPreference\s*=\s*'(\w+)'", statement)
                if assigned:
                    current = assigned.group(1)
                elif re.match(r'\$ErrorActionPreference\s*=\s*\$prev\b', statement):
                    current = saved
                elif re.match(r'\$prev\s*=\s*\$ErrorActionPreference\b', statement):
                    saved = current
                if '& $schtasks' in statement:
                    calls += 1
                    self.assertEqual(current, 'Continue', statement)
        self.assertEqual((calls, current), (2, 'Stop'))

    def test_upgrade_deletes_its_backup_only_after_success(self):
        lines = lines_of('install.ps1')
        rollback = index_of(lines, 'Move-Item -LiteralPath $backup -Destination $target')
        removal = index_of(lines, 'Remove-Item -LiteralPath $backup -Recurse')
        self.assertTrue(lines[removal].strip().startswith('if ($backup)'))
        self.assertLess(rollback, removal)
        self.assertLess(removal, index_of(lines, "Write-Output ('Installed: '"))

    @unittest.skipUnless(POWERSHELL, 'needs Windows PowerShell 5.1')
    def test_scripts_parse_in_windows_powershell(self):
        result = run_snippet([
            "foreach ($path in $env:DV_SCRIPTS -split '\\|') {",
            '    $tokens = $null; $errors = $null',
            '    [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)',
            '    if ($errors.Count) { $errors | Out-String; exit 1 }',
            '}',
            "Write-Output 'parsed'",
        ], {'DV_SCRIPTS': '|'.join(str(INSTALLER / name) for name in ('install.ps1', 'uninstall.ps1'))})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(b'parsed', result.stdout)

    @unittest.skipUnless(POWERSHELL, 'needs Windows PowerShell 5.1')
    def test_query_of_a_missing_task_does_not_stop_the_uninstaller(self):
        # The usual case: 兼容管理员权限窗口 was never turned on, so schtasks writes "not found" to stderr.
        lines = lines_of('uninstall.ps1')
        start = index_of(lines, '$taskName = ')
        end = index_of(lines, 'if ($queryExit', start)
        result = run_snippet(["$ErrorActionPreference = 'Stop'", "$taskName = 'DotVoice-test-no-such-task-5c1e'"]
                             + lines[start + 1:end]
                             + ['Write-Output "reached exit=$queryExit empty=$(-not $taskXml) pref=$ErrorActionPreference"'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, rb'reached exit=[1-9]\d* empty=True pref=Stop')

    @unittest.skipUnless(POWERSHELL, 'needs Windows PowerShell 5.1')
    def test_uninstall_removes_the_folders_it_leaves_empty(self):
        lines = lines_of('uninstall.ps1')
        start = index_of(lines, 'try {', index_of(lines, 'Remove-Item -LiteralPath $file -Force'))
        end = index_of(lines, '} catch', start)
        with tempfile.TemporaryDirectory() as folder:
            kept, emptied = Path(folder) / 'kept' / 'DotVoice', Path(folder) / 'emptied' / 'DotVoice'
            for target in (kept, emptied):
                (target / 'core' / '_internal' / 'x' / 'y').mkdir(parents=True)
                (target / 'ui' / 'assets').mkdir(parents=True)
            (kept / 'runtime').mkdir()
            (kept / 'runtime' / 'user.txt').write_text('keep', encoding='utf-8')
            result = run_snippet(["$ErrorActionPreference = 'Stop'",
                                  "foreach ($target in $env:DV_TARGETS -split '\\|') {"]
                                 + lines[start:end + 1] + ['}', "Write-Output 'done'"],
                                 {'DV_TARGETS': f'{kept}|{emptied}'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn(b'done', result.stdout)
            self.assertTrue((kept / 'runtime' / 'user.txt').is_file(), 'other files and their folders stay')
            self.assertEqual(sorted(p.name for p in kept.iterdir()), ['runtime'])
            self.assertFalse(emptied.exists(), 'an install folder left empty is removed too')


if __name__ == '__main__':
    unittest.main()
