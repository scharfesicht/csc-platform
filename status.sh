#!/usr/bin/env bash
# CSC public entry point: all lifecycle details are coordinated internally.
set -Eeuo pipefail
SELF_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null || { echo 'Python 3 is required on Ubuntu.' >&2; exit 1; }
(cd "$SELF_DIR" && sha256sum --check --status BOOTSTRAP-SHA256SUMS) || {
  echo 'Bootstrap integrity check failed. Use the complete reviewed public checkout.' >&2; exit 1;
}
exec python3 "$SELF_DIR/bootstrap/csc_setup.py" status "$@"
