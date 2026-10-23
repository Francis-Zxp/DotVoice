"""Bump the DotVoice version everywhere it is written: python tools/bump_version.py OLD NEW.

Keeps each file's encoding and line endings; fails without writing anything if a
version string is missing, so a half-bumped tree never happens."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def plan(old, new):
    files = {
        'bridge/core.py': [f"VERSION = '{old}'"],
        'build.ps1': [f"$version = '{old}'"],
        'tools/package_release.py': [f"version = '{old}'"],
        'shell/package.json': [f'"version": "{old}"'],
        'shell/src-tauri/tauri.conf.json': [f'"version": "{old}"'],
        'shell/src/lib/mock.ts': [f"version: '{old}-preview'"],
        'installer/install.ps1': [f'安装点声 {old}', f'"version":"{old}"', f"DisplayVersion='{old}'"],
    }
    edits = {}
    for rel, needles in files.items():
        text = (ROOT / rel).read_bytes().decode('utf-8')
        for needle in needles:
            if needle not in text:
                raise SystemExit(f'missing {needle!r} in {rel}')
            text = text.replace(needle, needle.replace(old, new))
        edits[rel] = text
    for rel, pattern, flags in (
            ('shell/src-tauri/Cargo.toml', rf'^(version = )"{re.escape(old)}"', re.M),
            ('shell/src-tauri/Cargo.lock', rf'(name = "dotvoice"\r?\nversion = )"{re.escape(old)}"', 0)):
        text = (ROOT / rel).read_bytes().decode('utf-8')
        text, count = re.subn(pattern, rf'\1"{new}"', text, count=1, flags=flags)
        if count != 1:
            raise SystemExit(f'missing version in {rel}')
        edits[rel] = text
    return edits


def main():
    old, new = sys.argv[1], sys.argv[2]
    edits = plan(old, new)
    for rel, text in edits.items():
        (ROOT / rel).write_bytes(text.encode('utf-8'))
    print(f'bumped {old} -> {new} in {len(edits)} files')


if __name__ == '__main__':
    main()
