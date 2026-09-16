#!/usr/bin/env python3
"""User-operated terminal prompt. Never paste the real key into chat or argv."""
import getpass
import os
from pathlib import Path
import stat
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ae_cloud_proxy import validate_key


def save_key(path, key):
    validate_key(key)
    parent=path.parent
    parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    info=parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode)!=0o700
            or info.st_uid!=os.geteuid() or parent.resolve()!=parent.absolute()):
        raise ValueError('private directory must be owned, unlinked and mode 0700')
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as out:
        os.fchmod(out.fileno(),0o600)
        out.write(key+'\n');out.flush();os.fsync(out.fileno())


def main():
    if not sys.stdin.isatty():
        print('Run this interactively in your server terminal; no piped credentials.',file=sys.stderr)
        return 2
    target=ROOT/'deployment/private/siliconflow.key'
    if target.exists() or target.is_symlink():
        print('Credential file already exists; refusing overwrite.',file=sys.stderr)
        return 2
    # No echo and no shell history entry containing the secret.
    key=getpass.getpass('SiliconFlow API key (hidden input): ').strip()
    try:
        save_key(target,key)
    except Exception as exc:
        print('NOT SAVED: '+type(exc).__name__,file=sys.stderr)
        return 2
    print('Saved private credential: '+str(target))
    return 0


if __name__=='__main__':raise SystemExit(main())
