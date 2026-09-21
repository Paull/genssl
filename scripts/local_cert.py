#!/usr/bin/env python3
"""Trusted host-administrator compatibility entry point; reseller users use certctl."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from server import CertificateError, CertificateStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['root', 'server', 'client'])
    parser.add_argument('names', nargs='*')
    parser.add_argument('--agent', help='Administrator-selected agent ID or name (default: platform)')
    parser.add_argument('--root', type=Path, default=Path(os.environ.get('CERT_ROOT_DIR', str(REPO_ROOT))))
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    root = args.root.resolve()
    store = CertificateStore(root, Path(os.environ.get('CERT_DB', str(root / 'out' / 'certificates.db'))))
    try:
        actor = {'user_id': None, 'role': 'admin', 'username': 'local-admin'}
        password = os.environ.get('ROOTPASS') or os.environ.get('rootpass')
        if args.kind == 'root':
            result = store.initialize_ca(password)
            store.audit(actor, 'ca.initialize', 'ca')
        else:
            if not args.names:
                parser.error('at least one domain/client name is required')
            if args.names[0] == 'root':
                raise CertificateError('root is reserved by the legacy wrapper; use the API for a certificate named root')
            name = args.names[0]
            agents = store.list_agents()
            selected = args.agent or 'platform'
            agent = next((a for a in agents if str(a['id']) == selected or a['name'] == selected), None)
            if not agent or agent['status'] != 'active':
                raise CertificateError('agent must exist and be active')
            payload = {'type': args.kind, 'name': name, 'key_type': os.environ.get('key_type', 'both')}
            if args.kind == 'server':
                payload['sans'] = args.names
            if os.environ.get('days'):
                payload['days'] = os.environ['days']
            os.environ['CERT_LEGACY_DIR'] = str(root / 'out' / name)
            result = store.issue(payload, password, actor, agent['id'])
            store.audit(actor, 'certificate.issue', 'certificate', result['id'], agent['id'], {'entry': 'local-cli'})
            legacy_dir = root / 'out' / name
            legacy_dir.mkdir(parents=True, exist_ok=True)
            output_dir = root / result['output_dir']
            snapshot_name = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M')
            snapshot = legacy_dir / snapshot_name
            if snapshot.exists():
                snapshot = legacy_dir / f'{snapshot_name}-{uuid.uuid4().hex[:8]}'
            snapshot.mkdir(parents=True)
            for filename in result['files']:
                if filename.startswith((f'rsa_{name}.', f'ec_{name}.')):
                    shutil.copy2(output_dir / filename, snapshot / filename)
                    target = legacy_dir / filename
                    target.unlink(missing_ok=True)
                    target.symlink_to(f'./{snapshot.name}/{filename}')
            for algorithm in ('rsa', 'ec'):
                root_link = legacy_dir / f'{algorithm}_root.crt'
                root_link.unlink(missing_ok=True)
                root_link.symlink_to(os.path.relpath(root / 'out' / f'{algorithm}_root.crt', legacy_dir))
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        elif args.kind == 'root':
            print('Root CA ready in', root / 'out')
        else:
            print(f"Certificate #{result['id']} registered to agent #{result['agent_id']}")
            print('Certificates are located in:')
            for file in result['files']:
                print(root / result['output_dir'] / file)
        return 0
    except (CertificateError, ValueError, OSError) as exc:
        print(f'certificate operation failed: {exc}', file=sys.stderr)
        return 1
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
