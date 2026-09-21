#!/usr/bin/env python3
"""Start an isolated CornTech API instance for Bun browser tests."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from server import create_server  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--web-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--allow-anonymous", action="store_true", help="enable the isolated legacy fixture mode")
    args = parser.parse_args()

    root = args.root.resolve()
    db_path = root / "out" / "certificates.db"
    if args.allow_anonymous:
        import os
        os.environ["CERT_ALLOW_ANONYMOUS"] = "1"
    httpd = create_server("127.0.0.1", args.port, root, db_path, args.web_dir)
    print(f"READY http://127.0.0.1:{httpd.server_port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.store.close()  # type: ignore[attr-defined]
        httpd.server_close()


if __name__ == "__main__":
    main()
