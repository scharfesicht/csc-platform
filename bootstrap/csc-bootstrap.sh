#!/usr/bin/env bash
# CSC Integration Platform: Docker Hub bootstrap for Ubuntu 24.04 and 26.04 (amd64).
# No Git, Docker socket mounts, automatic upgrades, or source-code installation.
set -Eeuo pipefail
IFS=$'\n\t'
umask 077

PRODUCT='CSC Integration Platform'
VERSION='1.0.0-rc2'
INSTALLER_DEFAULT='docker.io/scharfesicht/csc-platform-installer@sha256:02c9f5fd519175be5a7cb7951f9552cbb62e57604ca7daf8938d7243b1f362f5'
INSTALLER="$INSTALLER_DEFAULT"
INSTALL_DIR='/opt/csc-platform'
REGISTRY_USER=''
HOSTNAME_CSC=''
ADMIN_EMAIL=''
TLS_CERT=''
TLS_KEY=''
CA_CERT=''
SUBNET=''
BIND_ADDRESS=''
WITH_CACHE=1
INSTALL_DOCKER=0
STAGE_ONLY=0
ACTION='install'
TMP_STAGE=''
TMP_CONFIG=''
CONTAINER_ID=''

info() { printf '[CSC bootstrap] %s\n' "$*"; }
die() { printf '[CSC bootstrap] STOPPED: %s\n' "$*" >&2; exit 1; }
cleanup() {
  if [[ -n "$CONTAINER_ID" ]]; then docker rm -f "$CONTAINER_ID" >/dev/null 2>&1 || true; fi
  if [[ -n "$TMP_STAGE" && -d "$TMP_STAGE" ]]; then rm -rf -- "$TMP_STAGE"; fi
  if [[ -n "$TMP_CONFIG" && -d "$TMP_CONFIG" ]]; then rm -rf -- "$TMP_CONFIG"; fi
}
trap cleanup EXIT

usage() {
cat <<'USAGE'
CSC Integration Platform - Ubuntu 24.04/26.04 Docker Hub bootstrap

Usage:
  sudo bash csc-bootstrap.sh check [--install-dir /opt/csc-platform]
  sudo bash csc-bootstrap.sh install \
    --hostname csc-lab.example.internal \
    --admin-email administrator@example.internal \
    --tls-cert /secure/fullchain.pem --tls-key /secure/privkey.pem \
    --registry-user CUSTOMER_READONLY_DOCKERHUB_USER [--install-docker]
  sudo bash csc-bootstrap.sh status [--install-dir /opt/csc-platform]

Options for install:
  --installer DIGEST_REF     Supplier-issued pinned installer digest (default: 1.0.0-rc1)
  --install-dir DIRECTORY   Destination for a NEW installation (default: /opt/csc-platform)
  --registry-user USER      READ-ONLY Docker Hub username; access token prompted securely
  --install-docker          Install official Docker Engine/Compose on CLEAN Ubuntu 24.04 or 26.04
  --hostname FQDN           Management HTTPS hostname
  --admin-email ADDRESS     Initial administrator email
  --tls-cert PATH           Certificate chain PEM, file already on server
  --tls-key PATH            Matching private key PEM, file already on server
  --ca-cert PATH            Optional enterprise root CA certificate
  --subnet CIDR             Optional unused Docker private IPv4 /24
  --bind-address IP         Optional listening address (default: 0.0.0.0)
  --no-cache                Disable optional Valkey cache
  --stage-only              Install and initialize, but do not start services

Security and recovery:
  - Always review the downloaded script and verify its published SHA-256 checksum.
  - Token is read from /dev/tty; never passed as a CLI argument or written to project files.
  - Private registry credentials exist only in a temporary root-owned Docker config.
  - Existing installations are NEVER overwritten. Upgrades require a separate audited procedure.
  - Sync remains paused by the application until mapping, CSC and licensing commissioning.
USAGE
}

if [[ $# -gt 0 ]]; then
  case "$1" in
    install|check|status) ACTION="$1"; shift ;;
    --help|-h|help) usage; exit 0 ;;
  esac
fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --installer|--install-dir|--registry-user|--hostname|--admin-email|--tls-cert|--tls-key|--ca-cert|--subnet|--bind-address)
      [[ $# -ge 2 && -n "$2" ]] || die "Missing value for $1"
      case "$1" in
        --installer) INSTALLER="$2" ;;
        --install-dir) INSTALL_DIR="$2" ;;
        --registry-user) REGISTRY_USER="$2" ;;
        --hostname) HOSTNAME_CSC="$2" ;;
        --admin-email) ADMIN_EMAIL="$2" ;;
        --tls-cert) TLS_CERT="$2" ;;
        --tls-key) TLS_KEY="$2" ;;
        --ca-cert) CA_CERT="$2" ;;
        --subnet) SUBNET="$2" ;;
        --bind-address) BIND_ADDRESS="$2" ;;
      esac
      shift 2 ;;
    --install-docker) INSTALL_DOCKER=1; shift ;;
    --no-cache) WITH_CACHE=0; shift ;;
    --stage-only) STAGE_ONLY=1; shift ;;
    *) die "Unknown argument: $1 (use --help)" ;;
  esac
done

require_root_linux() {
  [[ "$(uname -s)" == 'Linux' ]] || die 'Run on Ubuntu Linux, not Windows/Git Bash.'
  [[ "$(id -u)" -eq 0 ]] || die 'Run with sudo or as root.'
  [[ -f /etc/os-release ]] || die 'Could not identify the Linux distribution.'
  # shellcheck source=/dev/null
  . /etc/os-release
  [[ "${ID:-}" == ubuntu ]] || die 'Only Ubuntu 24.04/26.04 LTS is supported.'
  case "${VERSION_ID:-}" in
    24.04|24.04.*|26.04|26.04.*) ;;
    *) die "Unsupported Ubuntu version: ${VERSION_ID:-unknown}; requires 24.04 or 26.04 LTS." ;;
  esac
  [[ "$(uname -m)" == 'x86_64' ]] || die 'Only linux/amd64 (x86_64) is supported.'
  [[ -s /etc/machine-id ]] || die 'Stable /etc/machine-id is required for licensing.'
  command -v python3 >/dev/null || die 'Python 3 is required.'
  command -v openssl >/dev/null || die 'OpenSSL is required.'
}

docker_ready() {
  command -v docker >/dev/null && docker version --format '{{.Server.Version}}' >/dev/null 2>&1 && docker compose version >/dev/null 2>&1
}

install_docker_engine() {
  if docker_ready; then return; fi
  [[ "$INSTALL_DOCKER" == 1 ]] || die 'Docker Engine/Compose missing. Re-run install with --install-docker, or install Docker manually.'
  if command -v docker >/dev/null || command -v containerd >/dev/null; then
    die 'Partial/existing Docker installation detected. Repair it manually; bootstrap will not replace packages.'
  fi
  command -v apt-get >/dev/null || die 'apt-get is required.'
  command -v systemctl >/dev/null || die 'systemd is required for Docker installation.'
  info 'Installing official Docker Engine and Compose via Docker Ubuntu apt repository.'
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y ca-certificates curl gnupg
  install -m 0755 -d /etc/apt/keyrings
  if [[ -e /etc/apt/keyrings/docker.asc || -e /etc/apt/sources.list.d/docker.sources ]]; then
    die 'Existing Docker apt configuration detected. Inspect it manually; bootstrap will not overwrite it.'
  fi
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod 0644 /etc/apt/keyrings/docker.asc
  local fingerprint
  fingerprint="$(gpg --show-keys --with-colons /etc/apt/keyrings/docker.asc | awk -F: '$1=="fpr" {print $10; exit}')"
  [[ "$fingerprint" == '9DC858229FC7DD38854AE2D88D81803C0EBFCD88' ]] || die 'Docker apt signing-key fingerprint mismatch.'
  # Always use the matching official Docker apt suite, never mix Noble
  # packages into a Resolute server (or vice versa).
  local docker_codename
  case "${VERSION_ID:-}" in
    24.04|24.04.*) docker_codename='noble' ;;
    26.04|26.04.*) docker_codename='resolute' ;;
    *) die "Unsupported Docker package suite for Ubuntu ${VERSION_ID:-unknown}." ;;
  esac
  cat >/etc/apt/sources.list.d/docker.sources <<SOURCES
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${docker_codename}
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
SOURCES
  DEBIAN_FRONTEND=noninteractive apt-get update
  DEBIAN_FRONTEND=noninteractive apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  docker_ready || die 'Docker/Compose failed preflight after installation.'
}

valid_ref() {
  [[ "$INSTALLER" =~ ^docker\.io/scharfesicht/csc-platform-installer@sha256:[0-9a-f]{64}$ ]]
}

validate_install() {
  valid_ref || die 'Installer must be pinned to a sha256 digest under docker.io/scharfesicht/csc-platform-installer.'
  [[ "$INSTALL_DIR" == /* && "$INSTALL_DIR" != '/' ]] || die 'Install directory must be a non-root absolute path.'
  [[ "$INSTALL_DIR" != *'..'* && "$INSTALL_DIR" != *$'\n'* ]] || die 'Unsafe install directory.'
  local ancestor="$INSTALL_DIR"
  while [[ "$ancestor" != '/' ]]; do
    [[ ! -L "$ancestor" ]] || die 'Symlink in installation path is not allowed.'
    ancestor="$(dirname -- "$ancestor")"
  done
  [[ ! -e "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]] || die "Installation directory already exists: $INSTALL_DIR. Do not overwrite; use status."
  [[ -n "$HOSTNAME_CSC" && -n "$ADMIN_EMAIL" && -n "$TLS_CERT" && -n "$TLS_KEY" ]] || die 'Provide --hostname, --admin-email, --tls-cert and --tls-key.'
  [[ "$REGISTRY_USER" =~ ^[A-Za-z0-9_.-]{1,100}$ ]] || die 'Provide --registry-user with a dedicated read-only Docker Hub account.'
  [[ -f "$TLS_CERT" && -r "$TLS_CERT" && -f "$TLS_KEY" && -r "$TLS_KEY" ]] || die 'TLS certificate/key must exist on the host and be readable by root.'
  [[ -z "$CA_CERT" || ( -f "$CA_CERT" && -r "$CA_CERT" ) ]] || die 'CA certificate is missing.'
  if [[ -n "$SUBNET" ]]; then [[ "$SUBNET" != *$'\n'* ]] || die 'Invalid subnet'; fi
  if [[ -n "$BIND_ADDRESS" ]]; then [[ "$BIND_ADDRESS" != *$'\n'* ]] || die 'Invalid bind address'; fi
  [[ -r /dev/tty && -w /dev/tty ]] || die 'Interactive terminal required for secure token and admin-password prompts.'
  # Fail before pulling private images if TLS material is obviously invalid.
  openssl x509 -in "$TLS_CERT" -noout -checkhost "$HOSTNAME_CSC" >/dev/null || die 'TLS certificate does not match hostname.'
  openssl x509 -in "$TLS_CERT" -noout -checkend 604800 >/dev/null || die 'TLS certificate expires within 7 days.'
  [[ "$(openssl x509 -in "$TLS_CERT" -noout -pubkey)" == "$(openssl pkey -in "$TLS_KEY" -pubout)" ]] || die 'TLS certificate and private key do not match.'
}

prompt_registry_login() {
  TMP_CONFIG="$(mktemp -d /tmp/csc-docker-credentials.XXXXXXXX)"
  chmod 0700 "$TMP_CONFIG"
  export DOCKER_CONFIG="$TMP_CONFIG"
  local token=''
  IFS= read -r -s -p 'Read-only Docker Hub access token (hidden): ' token </dev/tty || die 'Could not read token from terminal.'
  printf '\n' >/dev/tty
  [[ -n "$token" ]] || die 'Docker Hub token cannot be empty.'
  if ! printf '%s\n' "$token" | docker login --username "$REGISTRY_USER" --password-stdin >/dev/null; then
    unset token
    die 'Docker Hub read-only login failed. Check repository access and token scope.'
  fi
  unset token
  info 'Authenticated to private Docker Hub with temporary credentials.'
}

verify_extracted_manifest() {
  local path="$1"
  python3 - "$path" <<'PY'
import json,re,sys
from pathlib import Path
p=Path(sys.argv[1])
for entry in p.rglob('*'):
    if entry.is_symlink():raise SystemExit('STOPPED: Symlink in installer payload: '+str(entry))
manifest=json.loads((p/'release.json').read_text(encoding='utf-8'))
expected=('api','worker','migrator','gateway','postgres','cache')
if manifest.get('product')!='CSCIntegrationPlatform' or manifest.get('distribution')!='dockerhub' or manifest.get('registryNamespace')!='scharfesicht':
    raise SystemExit('STOPPED: Installer release manifest does not match the product/registry.')
if set(manifest.get('images',{}))!=set(expected):
    raise SystemExit('STOPPED: Installer release manifest is missing runtime images.')
for service in expected:
    info=manifest['images'][service]
    name=info.get('name','')
    if not re.fullmatch(r'docker\.io/scharfesicht/csc-platform-'+service+r'@sha256:[0-9a-f]{64}',name):
        raise SystemExit('STOPPED: Unpinned or unexpected runtime image: '+service)
    if not re.fullmatch('sha256:[0-9a-f]{64}',info.get('id','')):
        raise SystemExit('STOPPED: Invalid expected runtime image ID: '+service)
for name in ('deploy/cscctl.py','deploy/compose.yaml','deploy/nginx.conf'):
    if not (p/name).is_file():raise SystemExit('STOPPED: Missing installer controller: '+name)
print('Validated pinned CSC installer payload and all six image references.')
PY
}

do_install() {
  require_root_linux
  validate_install
  install_docker_engine
  info "Selected immutable installer: $INSTALLER"
  # Never share or retain the supplier's registry write token on customer hosts.
  prompt_registry_login
  docker pull --platform linux/amd64 "$INSTALLER"
  local parent
  parent="$(dirname -- "$INSTALL_DIR")"
  mkdir -p -- "$parent"
  [[ ! -L "$parent" ]] || die 'Install parent directory must not be a symlink.'
  TMP_STAGE="$(mktemp -d "${INSTALL_DIR}.bootstrap.XXXXXXXX")"
  CONTAINER_ID="$(docker create --platform linux/amd64 "$INSTALLER")"
  docker cp "$CONTAINER_ID:/bundle/." "$TMP_STAGE/"
  docker rm "$CONTAINER_ID" >/dev/null
  CONTAINER_ID=''
  verify_extracted_manifest "$TMP_STAGE"
  info 'Pulling and verifying all six runtime images before changing the target directory.'
  python3 "$TMP_STAGE/deploy/cscctl.py" pull
  [[ ! -e "$INSTALL_DIR" && ! -L "$INSTALL_DIR" ]] || die 'Installation directory appeared during staging; refusing overwrite.'
  mv -- "$TMP_STAGE" "$INSTALL_DIR"
  TMP_STAGE=''
  info 'Runtime files installed. Initializing this customer installation.'
  local -a args=(init --hostname "$HOSTNAME_CSC" --admin-email "$ADMIN_EMAIL" --tls-cert "$TLS_CERT" --tls-key "$TLS_KEY")
  [[ -z "$CA_CERT" ]] || args+=(--ca-cert "$CA_CERT")
  [[ -z "$SUBNET" ]] || args+=(--subnet "$SUBNET")
  [[ -z "$BIND_ADDRESS" ]] || args+=(--bind-address "$BIND_ADDRESS")
  [[ "$WITH_CACHE" == 1 ]] || args+=(--no-cache)
  python3 "$INSTALL_DIR/deploy/cscctl.py" "${args[@]}" || die "Initialization failed; files remain at $INSTALL_DIR. Do NOT rerun install over this directory."
  if [[ "$STAGE_ONLY" == 1 ]]; then
    info "STAGED. Services have not been started. Run: sudo python3 $INSTALL_DIR/deploy/cscctl.py up"
    return
  fi
  info 'Starting the CSC Platform using the existing deployment controller.'
  python3 "$INSTALL_DIR/deploy/cscctl.py" up || die "Startup incomplete. Inspect: sudo python3 $INSTALL_DIR/deploy/cscctl.py status"
  python3 "$INSTALL_DIR/deploy/cscctl.py" status
  info "INSTALLATION COMPLETE. Open https://$HOSTNAME_CSC . Synchronization remains paused pending commissioning."
}

require_root_linux
case "$ACTION" in
  check)
    info "$PRODUCT bootstrap $VERSION preflight"
    if docker_ready; then info 'Docker Engine + Compose: ready'; else info 'Docker Engine/Compose: missing (use install --install-docker on a clean host)'; fi
    info "Ubuntu ${VERSION_ID} LTS, linux/amd64, Python 3, OpenSSL and machine-id: ready"
    ;;
  status)
    [[ -f "$INSTALL_DIR/release.json" ]] || die "No customer installation at $INSTALL_DIR"
    docker_ready || die 'Docker Engine and Compose are required for status.'
    python3 "$INSTALL_DIR/deploy/cscctl.py" status
    ;;
  install) do_install ;;
  *) die 'Unrecognized action' ;;
esac
