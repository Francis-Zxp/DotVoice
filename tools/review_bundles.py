"""Split source files into numbered text chunks (<= LIMIT bytes) for an external reviewer
that can only read attachments. Usage: python tools/review_bundles.py OUT_DIR

Each chunk starts with '===== FILE: path (lines a-b of n) =====' blocks; every line is
prefixed with its line number so findings can cite file:line."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIMIT = 85_000

AREAS = {
    'py-insert': ['bridge/fast_insert.py', 'bridge/engines/cloud.py', 'bridge/engines/hotkey.py', 'bridge/elevation.py',
                  'bridge/engines/volc.py', 'bridge/engines/stream.py', 'bridge/engines/dashscope.py',
                  'bridge/engines/llm.py', 'bridge/engines/__init__.py', 'bridge/backend.py', 'bridge/secrets.py',
                  'bridge/clipboard.py'],
    'py-core': ['core_main.py', 'bridge/core.py', 'bridge/controller.py', 'bridge/prefs.py', 'bridge/notes.py',
                'bridge/audio.py', 'bridge/housekeeping.py', 'bridge/diagnostics.py', 'bridge/rpc.py', 'bridge/updates.py',
                'bridge/upstream.py'],
    'py-input': ['bridge/airmouse.py', 'bridge/keymap.py', 'bridge/voice_dsp.py', 'bridge/rawinput.py',
                 'bridge/speech_meter.py', 'bridge/sounds.py', 'bridge/tidepool.py', 'bridge/instances.py',
                 'bridge/pv60.py', 'bridge/devices.py', 'bridge/battery.py', 'bridge/usage.py'],
    'rust': sorted(f'shell/src-tauri/src/{p.name}' for p in (ROOT / 'shell/src-tauri/src').glob('*.rs')),
}
front = sorted(str(p.relative_to(ROOT)).replace('\\', '/') for p in (ROOT / 'shell/src').rglob('*')
               if p.suffix in ('.ts', '.tsx') and '.test.' not in p.name)
AREAS['front-overlay'] = [f for f in front if '/overlay/' in f or '/lib/' in f or 'stage' in f
                          or f.endswith(('App.tsx', 'Boundary.tsx', 'Chrome.tsx', 'main.tsx'))]
AREAS['front-pages'] = [f for f in front if f not in AREAS['front-overlay']] + ['shell/src/styles.css']


def blocks(rel):
    lines = (ROOT / rel).read_text(encoding='utf-8', errors='replace').splitlines()
    numbered = [f'{i:5d}| {line}' for i, line in enumerate(lines, 1)]
    start = 0
    while start < len(numbered):
        size, end = 0, start
        while end < len(numbered) and size + len(numbered[end].encode('utf-8')) + 1 < LIMIT - 400:
            size += len(numbered[end].encode('utf-8')) + 1
            end += 1
        end = max(end, start + 1)
        yield f'\n===== FILE: {rel} (lines {start + 1}-{end} of {len(lines)}) =====\n' + '\n'.join(numbered[start:end])
        start = end


def main():
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    index = {}
    for area, files in AREAS.items():
        chunks, current = [], ''
        for rel in files:
            if not (ROOT / rel).is_file():
                continue
            for block in blocks(rel):
                if current and len((current + block).encode('utf-8')) > LIMIT:
                    chunks.append(current)
                    current = ''
                current += block
        if current:
            chunks.append(current)
        index[area] = []
        for n, text in enumerate(chunks, 1):
            path = out / f'{area}-{n:02d}.txt'
            path.write_text(text, encoding='utf-8')
            index[area].append(str(path))
    (out / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding='utf-8')
    for area, paths in index.items():
        print(area, len(paths), 'chunks')


if __name__ == '__main__':
    main()
