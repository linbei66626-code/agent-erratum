#!/usr/bin/env python3
"""Quarantine verified, untracked AppleDouble sidecars from our vendor upload."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True, type=Path)
    args = parser.parse_args()
    project = args.project.resolve(strict=True)
    source = project / 'vendor/letta-v1'
    if source.is_symlink() or not (source / '.git').is_dir():
        raise RuntimeError('Expected our non-symlinked vendor git checkout')
    tracked = set(subprocess.check_output(
        ['git', 'ls-files', '-z'], cwd=source).decode().split('\0'))
    candidates = sorted(source.rglob('._*'))
    checked = []
    for path in candidates:
        relative = path.relative_to(source).as_posix()
        if path.is_symlink() or not path.is_file() or relative in tracked:
            raise RuntimeError(f'Refuse ambiguous/tracked sidecar: {relative}')
        with path.open('rb') as stream:
            magic = stream.read(4)
        if magic != bytes.fromhex('00051607'):
            raise RuntimeError(f'Not AppleDouble: {relative}')
        checked.append((path, relative))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    output = project / 'deployment/quarantine' / (stamp + '-' + secrets.token_hex(3))
    output.mkdir(parents=True, exist_ok=False)
    with (output / 'manifest.json').open('x') as stream:
        json.dump({'source': str(source), 'count': len(checked),
                   'paths': [relative for _, relative in checked]}, stream, indent=2)
    for path, relative in checked:
        destination = output / 'files' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise RuntimeError('Quarantine destination already exists')
        shutil.move(str(path), str(destination))
    print(json.dumps({'quarantined': len(checked), 'manifest': str(output / 'manifest.json'),
                      'recovery': 'files retain their original relative paths'}))


if __name__ == '__main__':
    main()
