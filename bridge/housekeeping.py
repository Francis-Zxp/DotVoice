"""Remove leftovers that DotVoice itself wrote into runtime/ and no longer uses.

Only this allow-list is ever touched, and only directly inside runtime/ and
runtime/sounds/ (never deeper, never runtime/smoke/):

- sounds/v<N>-*            cue caches of an older SOUND_VERSION (N < sound_version)
- sounds/v<cur>-<volume>*  cue caches of a volume not in keep_volumes ('-tight' included)
- smoke-*.bmp              window captures of old native self-tests
- ui-smoke.json            an old self-test result (current ones: runtime/smoke/runtime/)
- core.log                 only while empty and not open (the running core writes to it)

A cache folder goes only when it holds nothing but its own .wav/.tmp files. Notes,
prefs, bindings, usage, instances, secrets, status, key and sound logs and anything
unknown are never looked at. A file in use or without permission is simply left.
"""
import os
from pathlib import Path
import re
import stat

_CACHE = re.compile(r'v(\d+)-([a-z]+)(?:-[a-z0-9]+)*', re.IGNORECASE)
_SMOKE_SHOT = re.compile(r'smoke-.*\.bmp', re.IGNORECASE)
_CACHE_FILES = ('.wav', '.tmp')


def _lstat(path):
    try:
        return os.lstat(path)
    except OSError:
        return None


def _plain(info, kind):
    """A real file or folder: not a symlink, junction or other reparse point."""
    if info is None or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        return False
    return kind(info.st_mode)


def _unused(path):
    """True when no process has the file open (Windows: an exclusive open succeeds)."""
    if os.name != 'nt':
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)  # own instance: own prototypes
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # GENERIC_READ, no sharing, OPEN_EXISTING: fails while any other handle is open.
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return False
    kernel.CloseHandle(handle)
    return True


def _remove_file(path, removed):
    try:
        os.remove(path)
    except OSError:
        return False
    removed.append(Path(path))
    return True


def _remove_cache(folder, removed):
    """Its .wav/.tmp files, then the folder; reported as the folder when it is gone,
    else as the files that did go. Anything else inside keeps the folder."""
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    gone = []
    for entry in entries:
        if entry.name.lower().endswith(_CACHE_FILES) and _plain(_lstat(entry.path), stat.S_ISREG):
            _remove_file(entry.path, gone)
    try:
        os.rmdir(folder)
    except OSError:
        removed.extend(gone)
        return
    removed.append(Path(folder))


def clean_runtime(runtime_dir, sound_version, keep_volumes):
    """Remove the allow-listed leftovers in `runtime_dir`; returns the paths removed.

    `sound_version` is bridge.sounds.SOUND_VERSION; `keep_volumes` the cue volumes still
    in use (e.g. {prefs sounds.volume}). Never raises for missing folders or files
    that cannot be removed."""
    runtime = Path(runtime_dir)
    if isinstance(keep_volumes, str):
        keep_volumes = (keep_volumes,)
    keep = {str(volume).lower() for volume in keep_volumes}
    removed = []
    try:
        top = list(os.scandir(runtime))
    except OSError:
        return removed
    for entry in top:
        name = entry.name.lower()
        info = _lstat(entry.path)
        if not _plain(info, stat.S_ISREG):
            continue
        if _SMOKE_SHOT.fullmatch(name) or name == 'ui-smoke.json':
            _remove_file(entry.path, removed)
        elif name == 'core.log' and info.st_size == 0 and _unused(entry.path):
            _remove_file(entry.path, removed)
    try:
        caches = list(os.scandir(runtime / 'sounds'))
    except OSError:
        return removed
    for entry in caches:
        match = _CACHE.fullmatch(entry.name)
        if not match or not _plain(_lstat(entry.path), stat.S_ISDIR):
            continue
        version, volume = int(match.group(1)), match.group(2).lower()
        if version < sound_version or (version == sound_version and volume not in keep):
            _remove_cache(entry.path, removed)
    return removed
