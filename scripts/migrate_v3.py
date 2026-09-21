#!/usr/bin/env python3
"""Back up and apply the v3 single-machine CA+agent+certificate migration.

The API applies the SQLite migration on startup. This command wraps that
behavior with a restorable copy of the complete ``out/`` volume so upgrades
can be rehearsed and rolled back without regenerating the CA.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import CertificateStore  # noqa: E402


def stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def backup(root: Path, destination: Path) -> Path:
    out = (root / 'out').resolve()
    if not out.is_dir():
        raise SystemExit(f'out directory does not exist: {out}')
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / f'out-{stamp()}'
    shutil.copytree(out, target, symlinks=True)
    return target


def restore(root: Path, source: Path) -> None:
    out = (root / 'out').resolve()
    source = source.resolve()
    if not source.is_dir() or source.name.startswith('.'):
        raise SystemExit(f'invalid backup directory: {source}')
    staged = out.with_name(out.name + '.restore-staging')
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(source, staged, symlinks=True)
    previous = out.with_name(out.name + f'.before-restore-{stamp()}')
    if out.exists():
        out.rename(previous)
    staged.rename(out)
    print(json.dumps({'restored': str(source), 'previous_out': str(previous)}, ensure_ascii=False))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT, help='repository or data root')
    parser.add_argument('--db', type=Path, help='SQLite path (default: <root>/out/certificates.db)')
    parser.add_argument('--backup-dir', type=Path, help='where to write an out/ backup')
    parser.add_argument('--restore', type=Path, help='restore a prior backup directory instead of migrating')
    parser.add_argument('--yes', action='store_true', help='required for restore')
    args = parser.parse_args()
    root = args.root.resolve()
    if args.restore:
        if not args.yes:
            parser.error('--restore requires --yes')
        restore(root, args.restore)
        return 0
    db = (args.db or root / 'out' / 'certificates.db').resolve()
    backup_dir = (args.backup_dir or root / 'out-backups').resolve()
    saved = backup(root, backup_dir)
    store = CertificateStore(root, db)
    try:
        with store.db_lock:
            agents = int(store.db.execute('SELECT COUNT(*) FROM agents').fetchone()[0])
            users = int(store.db.execute('SELECT COUNT(*) FROM users').fetchone()[0])
            certificates = int(store.db.execute('SELECT COUNT(*) FROM certificates').fetchone()[0])
            registrations = int(store.db.execute('SELECT COUNT(*) FROM registrations').fetchone()[0])
            version = int(store.db.execute('PRAGMA user_version').fetchone()[0])
    finally:
        store.close()
    print(json.dumps({'backup': str(saved), 'schema_version': version, 'agents': agents, 'users': users, 'certificates': certificates, 'registrations': registrations}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
