#!/usr/bin/env python3
"""Download public weights; verify every byte against a pinned official HF manifest.

The transport is Qwen's public ModelScope repository. A mutable mirror revision
is safe here only because size and pinned HF digest are both verified. No model
code is executed, credentials used, old model removed, or inference started.
"""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import urllib.request
import urllib.parse

ROOT = Path('/root/rivermind-data/agent-erratum')
MANIFEST = Path(__file__).with_name('qwen4b-manifest.json')

def verify(path, row):
    h = hashlib.sha256()
    g = hashlib.sha1(f"blob {row['size']}\0".encode())
    size = 0
    with path.open('rb') as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            size += len(chunk)
            h.update(chunk)
            g.update(chunk)
    expected = row['sha256'] or row['git_blob_sha1']
    observed = h.hexdigest() if row['sha256'] else g.hexdigest()
    if size != row['size'] or observed != expected:
        raise ValueError(f"Pinned identity mismatch: {path.name}, bytes={size}")
    return {'name': path.name, 'bytes': size, 'sha256': h.hexdigest(),
            'git_blob_sha1': g.hexdigest(), 'pinned_identity_passed': True}

def main():
    if sys.platform != 'linux' or not ROOT.is_dir():
        raise RuntimeError('Authorized Linux project only')
    manifest = json.loads(MANIFEST.read_text())
    destination = ROOT / 'models' / ('Qwen3-4B-Instruct-2507-' + manifest['revision'][:8])
    destination.mkdir(parents=True, exist_ok=True)
    needed = sum(x['size'] for x in manifest['files'] if not (destination / x['name']).exists())
    free = shutil.disk_usage(destination).free
    if free < needed + 2 * 1024**3:
        raise RuntimeError(f'Insufficient disk: free={free}, need={needed}+2GiB; no automatic deletion')
    def fetch(row):
        name = row['name']
        if Path(name).name != name:
            raise ValueError('Unexpected path')
        path = destination / name
        if path.exists():
            return verify(path, row)
        part = path.with_name(name + '.part')
        print('DOWNLOADING ' + name, flush=True)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        url = manifest['download_source'] + urllib.parse.quote(name)
        # Public mirror transfers may end early without raising. Range requests
        # bound each transfer; existing partial bytes are trusted ONLY after the
        # final pinned whole-file digest passes. Never overwrite a verified file.
        start = part.stat().st_size if part.exists() else 0
        if start > row['size'] or part.is_symlink():
            raise ValueError('Unexpected partial file')
        if row['size'] >= 64 * 1024**2:
            with part.open('ab' if part.exists() else 'xb') as out:
                while start < row['size']:
                    end = min(start + 64 * 1024**2, row['size']) - 1
                    req = urllib.request.Request(url, headers={
                        'User-Agent': 'AE-01-public-model-download',
                        'Range': f'bytes={start}-{end}'})
                    with opener.open(req, timeout=60) as response:
                        expected_range = f'bytes {start}-{end}/{row["size"]}'
                        if response.status != 206 or response.headers.get('Content-Range') != expected_range:
                            raise ValueError('Mirror did not honor the exact byte range')
                        count = 0
                        while chunk := response.read(8 * 1024**2):
                            count += len(chunk)
                            if count > end - start + 1:
                                raise ValueError('Range response exceeds expected size')
                            out.write(chunk)
                    if count != end - start + 1:
                        raise ValueError('Truncated range; partial retained for verified continuation')
                    start = end + 1
                    print(f'RECEIVED {name} {start}/{row["size"]}', flush=True)
        elif start != row['size']:
            if start:
                raise ValueError('Small partial file requires inspection')
            req = urllib.request.Request(url, headers={'User-Agent': 'AE-01-public-model-download'})
            with part.open('xb') as out, opener.open(req, timeout=60) as response:
                while chunk := response.read(8 * 1024 * 1024):
                    out.write(chunk)
        report = verify(part, row)
        part.rename(path)
        report['name'] = name
        print('VERIFIED ' + name, flush=True)
        return report
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(fetch, manifest['files']))
    report_path = destination / ('verified-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '.json')
    with report_path.open('x') as out:
        json.dump({'manifest': manifest, 'files': reports, 'model_directory': str(destination),
                   'all_files_verified': True, 'model_loaded': False}, out, indent=2)
    print('DOWNLOAD_VERIFIED ' + str(report_path), flush=True)

if __name__ == '__main__':
    main()
