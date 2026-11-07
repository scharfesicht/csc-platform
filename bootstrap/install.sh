#!/usr/bin/env bash
# CSC standalone public launcher. It downloads the COMPLETE public bootstrap;
# customer runtime still comes only from the approved digest-pinned private installer.
set -Eeuo pipefail
set +x
umask 077
export GIT_TERMINAL_PROMPT=0

fail() { printf '[CSC setup] ERROR: %s\n' "$*" >&2; exit 1; }
notice() { printf '%s\n' "$*"; }
[ "${EUID:-$(id -u)}" -eq 0 ] || fail 'Run this launcher with sudo.'
[ -t 0 ] && [ -t 1 ] || fail 'Interactive terminal is required for secure credential prompts.'
command -v curl >/dev/null || fail 'curl is required (sudo apt install curl ca-certificates).'
command -v tar >/dev/null || fail 'tar is required (sudo apt install tar).'
command -v python3 >/dev/null || fail 'Python 3 is required.'
[[ "$OSTYPE" == linux* ]] || fail 'Ubuntu Linux required.'

cleanup() {
  unset GH_TOKEN HUB_TOKEN || true
  if [ -n "${work:-}" ] && [ -d "$work" ]; then rm -rf -- "$work"; fi
  exec 3<&- 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

notice '================================================'
notice '       CSC Integration Platform Setup'
notice '                ScharfeSicht'
notice '================================================'
notice ''
notice 'STEP 1 — GitHub Authentication'
notice '--------------------------------'
read -r -p 'GitHub username: ' GH_USER
[[ "$GH_USER" =~ ^[A-Za-z0-9-]{1,39}$ ]] || fail 'Invalid GitHub username.'
read -r -s -p 'GitHub Personal Access Token (hidden): ' GH_TOKEN; printf '\n'
[[ "$GH_TOKEN" =~ ^[A-Za-z0-9_-]{10,512}$ ]] || fail 'Invalid GitHub token format.'
notice 'Authenticating with GitHub...'
# The PAT is sent via curl stdin configuration, not CLI argv, URLs or files.
GITHUB_LOGIN="$(printf 'header = "Authorization: Bearer %s"\n' "$GH_TOKEN" | curl -fsS --retry 2 --connect-timeout 15 --max-time 35 --config - -H 'Accept: application/vnd.github+json' -H 'X-GitHub-Api-Version: 2022-11-28' https://api.github.com/user | python3 -c 'import json,sys; print(json.load(sys.stdin).get("login", ""))')" || fail 'GitHub authentication unsuccessful.'
[[ "${GITHUB_LOGIN,,}" == "${GH_USER,,}" ]] || fail 'GitHub token does not belong to the supplied GitHub user.'
unset GH_TOKEN GITHUB_LOGIN
notice 'GitHub authentication successful.'
notice ''

notice 'STEP 2 — Docker Hub Authentication'
notice '--------------------------------'
read -r -p 'Docker Hub username: ' HUB_USER
[[ "$HUB_USER" =~ ^[A-Za-z0-9_.-]{1,100}$ ]] || fail 'Invalid Docker Hub username.'
read -r -s -p 'Docker Hub read-only access token (hidden): ' HUB_TOKEN; printf '\n'
[ -n "$HUB_TOKEN" ] || fail 'Docker Hub token cannot be empty.'
notice 'Docker Hub credentials collected. Registry login will be verified before downloading private images.'
notice ''

notice 'STEP 3 — Installer Download'
notice '--------------------------------'
notice 'Retrieving verified public CSC bootstrap...'
work="$(mktemp -d /tmp/csc-launch.XXXXXXXX)"
chmod 700 "$work"
# Archive is public, so the GitHub PAT is not sent to codeload or other hosts.
curl -fLsS --retry 3 --connect-timeout 15 --max-time 120 --proto '=https' --tlsv1.2 \
  -o "$work/public.tar.gz" 'https://codeload.github.com/scharfesicht/csc-platform/tar.gz/refs/heads/main' || fail 'Cannot retrieve public bootstrap archive.'
# Reject entries outside the expected archive root, links and special files.
python3 - "$work/public.tar.gz" <<'PY'
import sys,tarfile
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    names=set()
    for item in archive:
        name=item.name.rstrip('/')
        if name.startswith('/') or '\\' in name or any(part in ('..','') for part in name.split('/')):
            raise SystemExit('Unsafe archive path')
        if not name.startswith('csc-platform-main/') and name != 'csc-platform-main':
            raise SystemExit('Unexpected archive root')
        if item.size > 4 * 1024 * 1024: raise SystemExit('Oversized public bootstrap entry')
        if not (item.isfile() or item.isdir()):
            raise SystemExit('Links and special archive entries are prohibited')
        if name in names: raise SystemExit('Duplicate archive entry')
        names.add(name)
PY
mkdir -p "$work/public"
tar --no-same-owner --no-same-permissions -xzf "$work/public.tar.gz" -C "$work/public" || fail 'Could not unpack verified bootstrap.'
public="$work/public/csc-platform-main"
[ -f "$public/BOOTSTRAP-SHA256SUMS" ] || fail 'Public bootstrap checksum manifest missing.'
(cd "$public" && sha256sum --check --status BOOTSTRAP-SHA256SUMS) || fail 'Public bootstrap checksum verification failed.'
[ -f "$public/install.sh" ] && [ -f "$public/bootstrap/csc_setup.py" ] || fail 'Required bootstrap files absent.'
notice 'Public bootstrap verified. Downloading approved private installer through the coordinator.'
# An inherited open FD transfers the secret without process arguments, environment,
# GitHub repository files or disk writes. The coordinator closes and consumes it once.
exec 3<<<"$HUB_TOKEN"
unset HUB_TOKEN
notice ''
# The coordinator has existing ownership checks, Docker provisioning, backups,
# first-run wizard, digest validation and resumable installation transitions.
bash "$public/install.sh" --install-docker --registry-user "$HUB_USER" --registry-token-fd 3 "$@"
notice ''
notice '================================================'
notice 'CSC INSTALLATION COMPLETED SUCCESSFULLY'
notice 'Next: activate license and configure integrations.'
notice '================================================'
