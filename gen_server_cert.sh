#!/usr/bin/env bash
set -euo pipefail
# Trusted host-administrator entry point; all signing shares the API lock/catalog.
exec python3 "$(dirname "${BASH_SOURCE[0]}")/scripts/local_cert.py" server "$@"
