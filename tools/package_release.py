"""Package only explicitly selected project files; exclude local runtime data.
Release files of other versions then go to the Recycle Bin (never deleted outright)."""
import ctypes
import hashlib
from pathlib import Path
import re
import zipfile

root = Path(__file__).resolve().parents[1]
out = root / 'release'
version = '0.11.5'
portable = out / f'DotVoice-{version}-windows-x64.zip'
source = out / f'DotVoice-{version}-source.zip'
SKIP_DIRS = {'__pycache__', 'references', 'device-refs', 'backups', 'runtime', 'node_modules', 'target', 'dist', 'gen', '.venv', '.git'}
# Earlier QA rounds' screenshots and captures: large, and `pnpm qa` makes them again.
SKIP_DESIGN = ('qa-', 'frames-', 'live-')
RELEASE_FILE = re.compile(r'DotVoice-(.+)-(?:windows-x64|source)\.zip|SHA256-(.+)\.txt')


def write_zip(target, entries):
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, name in sorted(entries, key=lambda item: str(item[1])):
            archive.write(path, str(name))
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        assert not any(name.startswith(('DotVoice/runtime/', 'DotVoice-source/runtime/')) or
                       'remote-input-pin' in name for name in archive.namelist())


def skipped(relative):
    parts = relative.parts
    return (bool(SKIP_DIRS & set(parts)) or
            (parts[0] == 'design' and parts[1].startswith(SKIP_DESIGN)) or
            parts[:3] == ('tools', 'whisper_bench', 'results'))


def source_entries():
    files = ['core_main.py', 'config.json', 'requirements.txt', 'requirements-build.txt', 'build.ps1',
             'README.md', 'VALIDATION.md', 'ACCEPTANCE.md', 'RELEASE_0.9.6.md', 'RELEASE_0.11.5.md', '.gitignore', 'LICENSE',
             'THIRD_PARTY_PV60.md', 'RELEASING.md', '.gitattributes']
    entries = [(root / name, Path('DotVoice-source') / name) for name in files]
    for folder in ['bridge', 'shell', 'tests', 'tools', 'design', 'licenses', 'integrations', 'installer', 'models', '.github']:
        entries += [(p, Path('DotVoice-source') / p.relative_to(root)) for p in (root / folder).rglob('*')
                    if p.is_file() and not skipped(p.relative_to(root)) and p.suffix not in ('.jsonl', '.log')]
    return entries


def other_versions(folder):
    """Release files (zips, checksums) of every version but this one; nothing else."""
    found = []
    for path in sorted(folder.iterdir()):
        match = RELEASE_FILE.fullmatch(path.name)
        if match and path.is_file() and not path.is_symlink() and (match.group(1) or match.group(2)) != version:
            found.append(path)
    return found


def recycle(paths):
    """Move files to the Recycle Bin: SHFileOperationW with FOF_ALLOWUNDO, without dialogs except
    Windows' own warning should a file be too large to recycle. True when all of them went."""
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _pack_ = 1 if ctypes.sizeof(ctypes.c_void_p) == 4 else 8  # shellapi.h packs it on x86 only
        _fields_ = [('hwnd', wintypes.HWND), ('wFunc', wintypes.UINT), ('pFrom', ctypes.c_void_p),
                    ('pTo', ctypes.c_void_p), ('fFlags', wintypes.WORD), ('fAnyOperationsAborted', wintypes.BOOL),
                    ('hNameMappings', ctypes.c_void_p), ('lpszProgressTitle', wintypes.LPCWSTR)]

    if not paths:
        return True
    fo_delete = 3
    # FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOCONFIRMMKDIR | FOF_NOERRORUI | FOF_WANTNUKEWARNING
    flags = 0x4 | 0x10 | 0x40 | 0x200 | 0x400 | 0x4000
    names = ''.join(str(Path(p).resolve()) + '\0' for p in paths)  # double-NUL terminated with the buffer's NUL
    buffer = (ctypes.c_wchar * (len(names) + 1))(*names)
    operation = SHFILEOPSTRUCTW(None, fo_delete, ctypes.addressof(buffer), None, flags, False, None, None)
    shell32 = ctypes.WinDLL('shell32')
    shell32.SHFileOperationW.argtypes = (ctypes.POINTER(SHFILEOPSTRUCTW),)
    return shell32.SHFileOperationW(ctypes.byref(operation)) == 0 and not operation.fAnyOperationsAborted


def main():
    out.mkdir(exist_ok=True)
    base = root / 'dist' / version / 'DotVoice'
    required = [base / 'DotVoice.exe', base / 'core' / 'dotvoice-core.exe', base / 'installed-files.json',
                base / 'models' / 'sherpa-zh-14M' / 'encoder-epoch-99-avg-1.int8.onnx']
    missing = [str(p) for p in required if not p.is_file()]
    if missing or not (base / 'core' / '_internal').is_dir():
        raise RuntimeError('Release folder is incomplete; refusing to publish: ' + ', '.join(missing))
    write_zip(portable, [(p, Path('DotVoice') / p.relative_to(base)) for p in base.rglob('*')
                         if p.is_file() and p.relative_to(base).parts[0] != 'runtime'] +
              [(root / 'installer' / name, Path(name)) for name in ['Install.cmd', 'install.ps1']])
    with zipfile.ZipFile(portable) as archive:
        assert archive.read('DotVoice/core/dotvoice-core.exe') == (base / 'core' / 'dotvoice-core.exe').read_bytes()
    write_zip(source, source_entries())
    (out / f'SHA256-{version}.txt').write_text('\n'.join(hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name
                                                         for p in [portable, source]) + '\n', encoding='ascii')
    for p in [portable, source]:
        print(f'{p.name}: {p.stat().st_size / 1024 / 1024:.1f} MiB')
    # Only after this version's files are complete: the older ones are recoverable from the Recycle Bin.
    old = other_versions(out)
    recycle(old)
    for p in old:
        print(('Kept (could not recycle): ' if p.exists() else 'Moved to the Recycle Bin: ') + p.name)


if __name__ == '__main__':
    main()
