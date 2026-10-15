"""Hash the application payload, including Python.NET's required runtime."""
import hashlib
import json
from pathlib import Path
import sys

base = Path(sys.argv[1]).resolve()
entries = []
for path in sorted(base.rglob('*')):
    relative = path.relative_to(base)
    if path.is_file() and relative.parts[0] != 'runtime' and path.name not in ('installed-files.json', 'installed.json'):
        entries.append({'path': relative.as_posix(), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
(base / 'installed-files.json').write_text(json.dumps(entries, indent=2), encoding='utf-8')
print(f'Installer manifest: {len(entries)} files')
