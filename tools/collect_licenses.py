"""Copy notices from the exact installed/locked build dependencies; never fetch licenses.

Run with .venv/Scripts/python.exe after pnpm install --frozen-lockfile and cargo fetch.
Existing hand-maintained licenses remain untouched. The manifest records missing upstream
notices explicitly; an SPDX label is not substituted for a license's original text.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import ssl
import subprocess
import sys
import pyexpat
import zlib

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'licenses' / 'dependencies'
PREFIXES = ('license', 'licence', 'copying', 'copyright', 'notice', 'unlicense')


def notices(folder):
    return sorted(p for p in folder.iterdir() if p.is_file() and p.name.lower().startswith(PREFIXES))


def record(records, ecosystem, name, version, origin, files, license_id=None, **extra):
    key = re.sub(r'[^A-Za-z0-9._-]', '-', f'{ecosystem}-{name}-{version}')
    entries = []
    for source, name_in_package in files:
        target = OUTPUT / key / name_in_package
        target.parent.mkdir(parents=True, exist_ok=True)
        data = source.read_bytes()
        target.write_bytes(data)
        entries.append({'file': target.relative_to(ROOT / 'licenses').as_posix(),
                        'source_file': name_in_package, 'sha256': hashlib.sha256(data).hexdigest()})
    records.append({'ecosystem': ecosystem, 'name': name, 'version': version,
                    'source': origin, 'license': license_id, 'notices': entries, **extra})


def collect_python(records):
    requested = dict(re.findall(r'^([\w-]+)==([^\s]+)',
                                (ROOT / 'requirements.txt').read_text(), re.M))
    requested['pyinstaller'] = importlib.metadata.version('pyinstaller')  # shipped bootloader
    for name, version in requested.items():
        distribution = importlib.metadata.distribution(name)
        if distribution.version != version:
            raise RuntimeError(f'{name}: installed version differs from requirements.txt')
        files = [(distribution.locate_file(p), p.name) for p in distribution.files or ()
                 if p.name.lower().startswith(PREFIXES)]
        record(records, 'python', name, version, f'https://pypi.org/project/{name}/{version}/', files,
               distribution.metadata.get('License-Expression') or distribution.metadata.get('License'))
    version = '.'.join(map(str, sys.version_info[:3]))
    record(records, 'runtime', 'CPython', version, f'CPython {version} Windows build LICENSE.txt',
           [(Path(sys.base_prefix) / 'LICENSE.txt', 'LICENSE.txt')], 'PSF-2.0 and bundled notices',
           native_versions={'OpenSSL': ssl.OPENSSL_VERSION, 'Expat': pyexpat.EXPAT_VERSION,
                            'zlib': zlib.ZLIB_RUNTIME_VERSION},
           note='The copied runtime license includes its Windows redistribution conditions. '
                'Native version inventory does not assert that every native dependency notice is in that file.')


def resolve_js(name, directory):
    # Resolve only actual installed dependencies, including pnpm's symlinked layout.
    for parent in (directory, *directory.parents):
        candidate = parent / 'node_modules' / name
        if (candidate / 'package.json').is_file():
            return candidate.resolve()
    raise RuntimeError(f'Missing installed production dependency: {name}')


def collect_js(records):
    shell = ROOT / 'shell'
    lock = (shell / 'pnpm-lock.yaml').read_text(encoding='utf-8')
    package = json.loads((shell / 'package.json').read_text())
    pending = [resolve_js(name, shell) for name in package['dependencies']]
    seen = set()
    while pending:
        folder = pending.pop()
        meta = json.loads((folder / 'package.json').read_text(encoding='utf-8'))
        name, version = meta['name'], meta['version']
        if (name, version) in seen:
            continue
        seen.add((name, version))
        if f'{name}@{version}' not in lock:
            raise RuntimeError(f'{name}@{version} is absent from pnpm-lock.yaml')
        files = [(p, p.name) for p in notices(folder)]
        record(records, 'npm', name, version, f'https://www.npmjs.com/package/{name}/v/{version}',
               files, meta.get('license'))
        for dependency in meta.get('dependencies', {}):
            pending.append(resolve_js(dependency, folder))
        for dependency in meta.get('optionalDependencies', {}):
            try:
                pending.append(resolve_js(dependency, folder))
            except RuntimeError:
                pass  # an optional dependency absent from this build is not distributed


def collect_rust(records):
    result = subprocess.run(['cargo', 'metadata', '--locked', '--offline', '--filter-platform',
                             'x86_64-pc-windows-msvc', '--format-version', '1'],
                            cwd=ROOT / 'shell' / 'src-tauri', capture_output=True, text=True,
                            encoding='utf-8', check=True,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    metadata = json.loads(result.stdout)
    nodes = {n['id']: n for n in metadata['resolve']['nodes']}
    selected, pending = set(), [metadata['resolve']['root']]
    while pending:
        identity = pending.pop()
        if identity in selected:
            continue
        selected.add(identity)
        pending += [d['pkg'] for d in nodes.get(identity, {}).get('deps', [])
                    if any(kind['kind'] != 'dev' for kind in d['dep_kinds'])]
    for package in metadata['packages']:
        if package['id'] not in selected or not package['source']:
            continue
        folder = Path(package['manifest_path']).parent
        files = notices(folder)
        if package.get('license_file'):
            files.append(folder / package['license_file'])
        files = sorted(set(files))
        vcs_path = folder / '.cargo_vcs_info.json'
        vcs = json.loads(vcs_path.read_text()) if vcs_path.exists() else {}
        original_files = [(p, p.relative_to(folder).as_posix()) for p in files]
        upstream = upstream_sources(package['name'], package['version']) if not files else []
        original_files += [(ROOT / 'licenses' / item['file'], Path(item['file']).name) for item in upstream]
        record(records, 'cargo', package['name'], package['version'], package['source'],
               original_files, package.get('license'),
               repository=package.get('repository'), vcs=vcs,
               upstream_notices=[item['url'] for item in upstream],
               note='Windows target normal/build dependency selected by locked offline Cargo metadata; dev dependencies excluded.')


def upstream_sources(name=None, version=None):
    manifest = ROOT / 'licenses' / 'UPSTREAM-SOURCES.json'
    values = json.loads(manifest.read_text(encoding='utf-8'))['files'] if manifest.exists() else []
    selected = [r for r in values if name is None or (r['name'], r['version']) == (name, version)]
    for item in selected:
        path = ROOT / 'licenses' / item['file']
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['sha256']:
            raise RuntimeError('Upstream notice hash mismatch: ' + item['file'])
    return selected


def collect_native(records):
    names = {'OpenSSL': 'Apache-2.0', 'Expat': 'MIT', 'zlib': 'Zlib',
             'libffi': 'MIT', 'xz-liblzma': 'Public domain / 0BSD (see notice)'}
    groups = {}
    for item in upstream_sources():
        if item['name'] in names:
            groups.setdefault((item['name'], item['version']), []).append(item)
    for (name, version), items in sorted(groups.items()):
        binaries = {'OpenSSL': ['DLLs/libcrypto-3-x64.dll', 'DLLs/libssl-3-x64.dll'],
                    'Expat': ['DLLs/pyexpat.pyd'], 'zlib': ['python312.dll'],
                    'libffi': ['DLLs/libffi-8.dll'], 'xz-liblzma': ['DLLs/_lzma.pyd']}[name]
        hashes = {path: hashlib.sha256((Path(sys.base_prefix) / path).read_bytes()).hexdigest()
                  for path in binaries}
        record(records, 'native', name, version, [item['url'] for item in items],
               [(ROOT / 'licenses' / item['file'], Path(item['file']).name) for item in items], names[name],
               interpreter_binary_sha256=hashes,
               note='OpenSSL/Expat/zlib versions are reported by the actual build interpreter. '
                    'libffi/liblzma do not expose a version in this binary; their source notices are '
                    'retained with explicit provenance, not asserted as identification of the binary version.')


def main():
    records = []
    collect_python(records)
    collect_js(records)
    collect_rust(records)
    collect_native(records)
    records.sort(key=lambda item: (item['ecosystem'], item['name'], item['version']))
    manifest = ROOT / 'licenses' / 'DEPENDENCY-SOURCES.json'
    manifest.write_text(json.dumps({'schema': 1, 'collector': 'tools/collect_licenses.py',
                                    'dependencies': records}, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    missing = [r for r in records if not r['notices']]
    print(f'Collected {sum(len(r["notices"]) for r in records)} notices for {len(records)} dependency records.')
    for item in missing:
        print(f'MISSING original notice: {item["ecosystem"]} {item["name"]} {item["version"]} ({item["license"]})')
    if missing:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
