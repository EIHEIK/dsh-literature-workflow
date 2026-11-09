"""Capture code and a consistent legacy database without changing production data.

Snapshots belong outside this public repository. No profile or credential files
are copied. The existing publication/artifact files remain in their original
location and their hashes are recorded for subsequent migration checks.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import zipfile


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(repo, installed, data, destination):
    repo, installed, data, destination = map(lambda p: Path(p).resolve(),
                                            (repo, installed, data, destination))
    if destination.is_relative_to(repo):
        raise ValueError('Private baseline must be outside the public repository')
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {'schema': 1, 'code': {}, 'legacyArtifacts': [], 'database': None}
    entries = ['package.json', 'index.js', 'cordis.patch.yml', 'lib', 'runtime',
               'skills', 'tests', 'scripts', 'docs', 'README.md', 'config.example.json']
    for name, root in [('worktree', repo), ('installed', installed)]:
        files = []
        for entry in entries:
            path = root / entry
            if path.is_file(): files.append(path)
            elif path.is_dir():
                files.extend(p for p in path.rglob('*') if p.is_file()
                             and '__pycache__' not in p.parts and p.suffix != '.pyc')
        with zipfile.ZipFile(destination / (name + '.zip'), 'x', zipfile.ZIP_DEFLATED) as archive:
            rows = []
            for path in sorted(set(files)):
                relative = path.relative_to(root).as_posix()
                archive.write(path, relative)
                rows.append({'path': relative, 'sha256': sha(path)})
        manifest['code'][name] = rows
    diff = subprocess.run(['git', 'diff', '--binary', '--no-ext-diff'], cwd=repo,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    (destination / 'preexisting.patch').write_bytes(diff.stdout)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()
    manifest['head'] = head
    source_db = data / 'state.sqlite3'
    if source_db.is_file():
        with sqlite3.connect(source_db.as_uri() + '?mode=ro', uri=True) as source:
            with sqlite3.connect(destination / 'state.sqlite3') as target:
                source.backup(target)
        manifest['database'] = {'source': str(source_db),
                                'backupSha256': sha(destination / 'state.sqlite3')}
    for path in sorted(data.rglob('*')):
        if path.is_file() and path.name not in ('state.sqlite3', 'state.sqlite3-wal', 'state.sqlite3-shm'):
            manifest['legacyArtifacts'].append({'path': path.relative_to(data).as_posix(),
                                                'sha256': sha(path), 'bytes': path.stat().st_size})
    (destination / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'destination': str(destination), 'head': head,
            'artifacts': len(manifest['legacyArtifacts']),
            'databaseCopied': manifest['database'] is not None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    for name in ('repo', 'installed', 'data', 'destination'): parser.add_argument('--' + name, required=True)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.repo, args.installed, args.data, args.destination), ensure_ascii=False))
