#!/usr/bin/env bash
# Compatibility entry point. Obtain the full public checkout, not this file alone.
set -Eeuo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[[ -f "$HERE/csc_setup.py" ]] || { echo 'Use the complete public csc-platform checkout, then sudo bash install.sh.' >&2; exit 1; }
(cd "$HERE/.." && sha256sum --check --status BOOTSTRAP-SHA256SUMS) || exit 1
exec python3 "$HERE/csc_setup.py" "$@"
