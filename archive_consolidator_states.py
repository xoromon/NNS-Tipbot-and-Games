#!/usr/bin/env python3
"""Bundle old confirmed consolidator snapshots into verified monthly ZIPs.

Preview by default. Stop the consolidator timer and wait for its service to
finish before --apply. Never touches the current state.json or wallet RPC.
"""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

PATTERN = re.compile(r'^state\.(\d{8}T\d{6}Z)\.([0-9a-f]{64})\.confirmed\.json$')


def candidates(directory, keep_days, confirmations):
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=keep_days)
    groups = {}
    for path in sorted(directory.iterdir()):
        match = PATTERN.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            continue
        stamp = dt.datetime.strptime(match[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=dt.timezone.utc)
        if stamp >= cutoff:
            continue
        raw = path.read_bytes()
        try:
            data = json.loads(raw)
            valid = (data.get('status') == 'broadcast_seen' and data.get('txid') == match[2]
                     and int(data.get('confirmations', 0)) >= confirmations)
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            print('SKIP (unverified confirmed state): ' + path.name)
            continue
        groups.setdefault(stamp.strftime('%Y-%m'), []).append((path, raw))
    return groups


def bundle(directory, month, entries):
    archive_dir = directory / 'confirmed-archives'
    if archive_dir.is_symlink():
        raise ValueError('Archive directory must not be a symlink')
    archive_dir.mkdir(mode=0o700, exist_ok=True)
    target = archive_dir / (month + '.zip')
    if target.is_symlink():
        raise ValueError('Archive must not be a symlink')
    contents = {}
    if target.exists():
        with zipfile.ZipFile(target) as old:
            for info in old.infolist():
                if info.filename in contents:
                    raise ValueError('Duplicate ZIP entry: ' + info.filename)
                contents[info.filename] = old.read(info)  # validates existing CRC
    for path, raw in entries:
        if path.name in contents and contents[path.name] != raw:
            raise ValueError('Archive entry differs from source: ' + path.name)
        contents[path.name] = raw
    fd, temporary = tempfile.mkstemp(prefix=month + '.', suffix='.zip.tmp', dir=archive_dir)
    temp = Path(temporary)
    try:
        with os.fdopen(fd, 'w+b') as handle:
            with zipfile.ZipFile(handle, 'w', compression=zipfile.ZIP_DEFLATED) as out:
                for name, raw in contents.items():
                    out.writestr(name, raw)
            handle.flush()
            os.fsync(handle.fileno())
        with zipfile.ZipFile(temp) as verify:
            for name, raw in contents.items():
                if verify.read(name) != raw:
                    raise ValueError('Archive verification failed: ' + name)
        os.replace(temp, target)
        # Persist the directory entry before removing source snapshots on Linux.
        directory_fd = os.open(str(archive_dir), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        with zipfile.ZipFile(target) as verify:
            for path, raw in entries:
                if verify.read(path.name) != raw:
                    raise ValueError('Installed archive verification failed: ' + path.name)
                if path.is_symlink() or path.read_bytes() != raw:
                    raise ValueError('Source changed during archival: ' + path.name)
                path.unlink()
        print(f'Archived {len(entries)} confirmed snapshots into {target}')
    finally:
        if temp.exists():
            temp.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--keep-days', type=int, default=30, help='Keep recent individual files (default: 30 days)')
    parser.add_argument('--required-confirmations', type=int, default=6)
    parser.add_argument('--apply', action='store_true', help='Write verified ZIPs and remove only their matching source snapshots')
    args = parser.parse_args()
    if args.keep_days < 0 or args.required_confirmations < 1:
        parser.error('keep-days must be nonnegative; required-confirmations must be positive')
    if not args.state_dir.is_dir():
        parser.error('state-dir must be an existing directory')
    # Coordinate concurrent archive tools; the consolidator itself has no such lock.
    # Preview does not create a lock or any other files.
    lock = None
    try:
        if args.apply:
            import fcntl
            lock = (args.state_dir / '.archive.lock').open('a')
            os.chmod(lock.name, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        groups = candidates(args.state_dir, args.keep_days, args.required_confirmations)
        for month, entries in groups.items():
            if args.apply:
                bundle(args.state_dir, month, entries)
            else:
                print(f'PREVIEW: {len(entries)} files -> confirmed-archives/{month}.zip')
        if not groups:
            print('No eligible confirmed snapshots.')
        return 0
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        print('Archive stopped: ' + str(exc))
        return 1
    finally:
        if lock is not None:
            lock.close()


if __name__ == '__main__':
    raise SystemExit(main())
