#!/usr/bin/env python3
"""CSC guided deployment coordinator. No application source or supplier credentials.

The public coordinator verifies and runs the digest-pinned PRIVATE installer.
It never deletes a database/volume, regenerates existing secrets or invokes Git.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import getpass
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import types
import uuid
import urllib.request
from pathlib import Path

BOOTSTRAP_VERSION = '1.0.0-rc5'
HERE = Path(__file__).resolve().parent
SERVICES = ('api', 'worker', 'migrator', 'gateway', 'postgres', 'cache')
DEPLOY_FILES = ('cscctl.py', 'compose.yaml', 'nginx.conf', 'cache-entrypoint.sh',
                'postgres-init.sh', 'runtime-entrypoint.sh')
DOC_FILES = ('CUSTOMER-QUICKSTART.md', 'DOCKERHUB-DEPLOYMENT.md', 'THIRD-PARTY-NOTICES.md')
PAYLOAD_FILES = {'release.json', *('deploy/' + n for n in DEPLOY_FILES), *('docs/' + n for n in DOC_FILES)}
REQUIRED_SECRETS = ('postgres_password', 'migration_password', 'db_password', 'cache_password',
                    'master_key', 'installation_id', 'admin_password', 'runtime_config',
                    'migration_config', 'tls_cert', 'tls_key', 'ca_bundle')
PIN = re.compile(r'docker\.io/scharfesicht/csc-platform-installer@sha256:[0-9a-f]{64}')
VERSION = re.compile(r'(\d+)\.(\d+)\.(\d+)(?:-rc(\d+))?(?:-hf(\d+))?')


class SetupError(RuntimeError):
    pass


def info(message: str) -> None:
    print('[CSC] ' + message, flush=True)


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def safe_path(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if len(path.parts) < 3 or any(c in str(path) for c in ('\n', '\r', '\x00')):
        raise SetupError('Unsafe installation path.')
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SetupError('Symlinks are not allowed in managed paths: ' + str(part))
    return path


def secure_dir(path: Path) -> None:
    safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)


def atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
    safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.csc-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_json(path: Path, value: dict) -> None:
    atomic(path, (json.dumps(value, indent=2) + '\n').encode())


def read_json(path: Path) -> dict:
    if not regular(path) or path.stat().st_size > 2 * 1024 * 1024:
        raise SetupError('Missing or unsafe metadata file: ' + str(path))
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise SetupError('Expected an object in ' + str(path))
    return value


def verify_checkout(root: Path) -> None:
    """Integrity within a trusted public checkout, not a substitute for GitHub trust."""
    sums = root / 'BOOTSTRAP-SHA256SUMS'
    if not regular(sums):
        raise SetupError('Download/clone the complete public bootstrap repository.')
    seen = set()
    for line in sums.read_text().splitlines():
        expected, name = line.split('  ', 1)
        if not re.fullmatch(r'[a-f0-9]{64}', expected) or Path(name).is_absolute() or '..' in Path(name).parts or name in seen:
            raise SetupError('Invalid bootstrap checksum manifest.')
        file = safe_path(root / name)
        if not file.is_relative_to(root.resolve()) or not regular(file) or sha(file) != expected:
            raise SetupError('Bootstrap file integrity check failed: ' + name)
        seen.add(name)
    for name in ('bootstrap/csc_setup.py', 'bootstrap/release.json', 'install.sh', 'upgrade.sh'):
        if name not in seen:
            raise SetupError('Bootstrap checksum manifest is incomplete.')


def load_descriptor(path: Path) -> dict:
    result = read_json(path)
    if result.get('product') != 'CSCIntegrationPlatform' or result.get('schemaVersion') != 1:
        raise SetupError('Unexpected public release descriptor.')
    if not PIN.fullmatch(result.get('installer', '')) or not VERSION.fullmatch(result.get('release', '')):
        raise SetupError('The approved release must use a pinned ScharfeSicht installer.')
    if result.get('platform') != 'linux/amd64':
        raise SetupError('Only the approved linux/amd64 platform is supported.')
    return result


def version_key(value: str) -> tuple:
    m = VERSION.fullmatch(value)
    if not m:
        raise SetupError('Unsupported release version; refusing automatic transition: ' + value)
    major, minor, patch, rc, hf = m.groups()
    return int(major), int(minor), int(patch), int(rc is None), int(rc or 0), int(hf or 0)


def validate_manifest(manifest: dict) -> None:
    if (manifest.get('product'), manifest.get('distribution'), manifest.get('registryNamespace')) != ('CSCIntegrationPlatform', 'dockerhub', 'scharfesicht'):
        raise SetupError('Not an approved ScharfeSicht private distribution.')
    version_key(manifest.get('release', ''))
    if not re.fullmatch('[a-f0-9]{64}', manifest.get('publicKeySha256', '')):
        raise SetupError('Missing product license verification-key fingerprint.')
    images = manifest.get('images', {})
    if set(images) != set(SERVICES):
        raise SetupError('The private release must contain all six runtime images.')
    for name, image in images.items():
        expression = r'docker\.io/scharfesicht/csc-platform-' + name + r'@sha256:[0-9a-f]{64}'
        if not re.fullmatch(expression, image.get('name', '')) or not re.fullmatch('sha256:[0-9a-f]{64}', image.get('id', '')):
            raise SetupError('Unexpected or unpinned runtime image: ' + name)


def validate_bundle(folder: Path, descriptor: dict) -> dict:
    for file in folder.rglob('*'):
        if file.is_symlink() or not (file.is_dir() or file.is_file()):
            raise SetupError('Unsafe entry in private installer payload.')
        if file.is_file() and file.relative_to(folder).as_posix() not in PAYLOAD_FILES:
            raise SetupError('Unexpected private installer file: ' + file.relative_to(folder).as_posix())
    for name in PAYLOAD_FILES:
        if not regular(folder / name):
            raise SetupError('Incomplete private installer: ' + name)
    manifest = read_json(folder / 'release.json')
    validate_manifest(manifest)
    if manifest['release'] != descriptor['release']:
        raise SetupError('Private installer release does not match the approved public pointer.')
    return manifest


def classify(root: Path) -> str:
    if not root.exists():
        return 'fresh'
    if not root.is_dir():
        raise SetupError('Installation path is not a directory.')
    validate_manifest(read_json(root / 'release.json'))
    if not regular(safe_path(root / 'deploy/cscctl.py')):
        raise SetupError('Existing directory is not a CSC installation; nothing will be overwritten.')
    state, env = root / '.deploy', root / 'deploy/.env'
    if not state.exists() and not env.exists():
        return 'uninitialized'
    if not state.is_dir() or not regular(env):
        raise SetupError('Partial secrets/configuration detected. Refusing to regenerate existing identity or passwords.')
    for name in REQUIRED_SECRETS:
        path = safe_path(state / 'secrets' / name)
        if not regular(path):
            raise SetupError('Incomplete customer configuration: ' + name + '. No secrets were replaced.')
    if not regular(safe_path(state / 'hostname')) or not safe_path(state / 'keys').is_dir():
        raise SetupError('Incomplete customer hostname/key storage.')
    return 'started' if (state / 'running-release.json').exists() else 'initialized'


def transition(old: dict, new: dict) -> None:
    validate_manifest(old)
    validate_manifest(new)
    if old['publicKeySha256'] != new['publicKeySha256']:
        raise SetupError('The product license signing trust changed. Automatic update refused.')
    if version_key(new['release']) < version_key(old['release']):
        raise SetupError('Automatic downgrades are not supported.')
    if old['release'] == new['release'] and any(old['images'][s]['name'] != new['images'][s]['name'] for s in SERVICES):
        raise SetupError('The same immutable release cannot contain different runtime images.')


def secret_fingerprints(root: Path) -> dict:
    return {n: sha(root / '.deploy/secrets' / n) for n in REQUIRED_SECRETS}


def password_valid(value: str) -> bool:
    # Match the already published rc2 backend/installer, without silently weakening it.
    return (16 <= len(value) <= 256 and not any(c in value for c in ('\r', '\n', '\x00'))
            and all(re.search(p, value) for p in ('[a-z]', '[A-Z]', '[0-9]', '[^a-zA-Z0-9]')))


def prompt_password(reader=None) -> str:
    reader = reader or getpass.getpass
    info('Choose an administrator password: 16+ characters, uppercase, lowercase, number and symbol.')
    info('Password entry is hidden. Existing installations are never asked to recreate this password.')
    while True:
        value = reader('Administrator password: ')
        if not password_valid(value):
            info('That password does not meet the current policy. Please try again; nothing has been initialized.')
            continue
        if reader('Confirm administrator password: ') == value:
            return value
        info('The two entries did not match. Please try again.')


def ask(label: str, default: str = '') -> str:
    answer = input(label + (' [' + default + ']' if default else '') + ': ').strip()
    return answer or default


def consent(label: str, default=False) -> bool:
    return ask(label + (' (Y/n)' if default else ' (y/N)'), 'yes' if default else 'no').lower() in ('yes', 'y')


def module_at(root: Path):
    # The controller comes only from the digest-pinned supplier image.
    spec = importlib.util.spec_from_file_location('_csc_private_controller_' + uuid.uuid4().hex, root / 'deploy/cscctl.py')
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


def parse_os(text: str) -> tuple[str, str]:
    values = {}
    for line in text.splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            values[k] = v.strip().strip('"').strip("'")
    release = values.get('VERSION_ID', '')
    if values.get('ID') != 'ubuntu' or not re.fullmatch(r'(24\.04|26\.04)(?:\.\d+)?', release):
        raise SetupError('Supported hosts: Ubuntu 24.04 or 26.04 LTS.')
    return release, 'noble' if release.startswith('24.04') else 'resolute'


class Coordinator:
    def __init__(self, args, descriptor: dict):
        self.args, self.descriptor = args, descriptor
        self.root = safe_path(Path(args.install_dir))
        identity = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
        self.control = Path('/var/lib/csc-platform-bootstrap') / identity
        self.profile_file = self.control / 'preferences.json'
        self.profile = {}
        self.log = None
        self.env = os.environ.copy()
        # Supplier management credentials are never used by a customer deployment.
        self.env.pop('CSC_DOCKERHUB_TOKEN', None)
        self.env.pop('CSC_DOCKERHUB_IDENTIFIER', None)
        self.authenticated = False
        self.temp_auth = None
        self.stage = None
        self.lock_handle = None
        self.registry_config = self.control / 'registry'

    def preflight(self):
        if sys.platform != 'linux' or os.geteuid() != 0:
            raise SetupError('Run this customer command with sudo on the Ubuntu server.')
        release, self.codename = parse_os(Path('/etc/os-release').read_text())
        if os.uname().machine != 'x86_64' or not Path('/etc/machine-id').read_text().strip():
            raise SetupError('An x86-64 host with a stable machine identity is required.')
        if sys.version_info < (3, 12):
            raise SetupError('Python 3.12+ is required on the customer host.')
        for tool in ('openssl', 'curl'):
            if not shutil.which(tool):
                raise SetupError('Missing prerequisite: ' + tool)
        info('Bootstrap ' + BOOTSTRAP_VERSION + ' | Ubuntu ' + release + ' | approved application ' + self.descriptor['release'])

    @contextlib.contextmanager
    def session(self):
        import fcntl
        secure_dir(self.control)
        for part in (self.control.parent, self.control):
            if part.stat().st_uid != 0:
                raise SetupError('Bootstrap state must be owned by root.')
        lock = self.control / 'operation.lock'
        safe_path(lock)
        self.lock_handle = lock.open('a+')
        lock.chmod(0o600)
        try:
            fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_handle.close()
            raise SetupError('Another CSC operation is already running on this installation.') from None
        self.profile = read_json(self.profile_file) if self.profile_file.exists() else {}
        logs = self.control / 'logs'
        secure_dir(logs)
        self.log = logs / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.log')
        self.log.touch(mode=0o600)
        try:
            yield
        finally:
            if self.stage and self.stage.exists():
                shutil.rmtree(self.stage)
            if self.temp_auth and self.temp_auth.exists():
                shutil.rmtree(self.temp_auth)
            fcntl.flock(self.lock_handle, fcntl.LOCK_UN)
            self.lock_handle.close()

    def run(self, command, *, capture=False, check=True, secret_input=None, timeout=None):
        result = subprocess.run([str(v) for v in command], env=self.env,
                                input=secret_input, text=True, capture_output=True, timeout=timeout)
        # Authentication data and credential-helper output never enter logs.
        if self.log and secret_input is None:
            with self.log.open('a', encoding='utf-8') as stream:
                stream.write('> ' + ' '.join(map(str, command)) + '\n')
                stream.write(result.stdout or '')
                stream.write(result.stderr or '')
        if check and result.returncode:
            raise SetupError('Command failed: ' + Path(str(command[0])).name + '. Details: ' + str(self.log))
        return (result.stdout if capture else result.returncode)

    def docker_ready(self):
        return bool(shutil.which('docker') and self.run(['docker', 'version', '--format', '{{.Server.Version}}'], check=False) == 0
                    and self.run(['docker', 'compose', 'version'], check=False) == 0)

    def ensure_docker(self):
        if self.docker_ready():
            return
        if shutil.which('docker') or shutil.which('containerd'):
            raise SetupError('An existing Docker/containerd installation is not healthy. It will not be replaced.')
        if not self.args.install_docker and not consent('Install Docker Engine and Compose from the official Docker Ubuntu repository?'):
            raise SetupError('Docker is required. No application files were changed.')
        self.run(['apt-get', 'update'])
        self.run(['apt-get', 'install', '-y', 'ca-certificates', 'curl', 'gnupg'])
        key, sources = Path('/etc/apt/keyrings/docker.asc'), Path('/etc/apt/sources.list.d/docker.sources')
        if key.exists() or sources.exists():
            raise SetupError('Existing Docker package configuration found; refusing to overwrite it.')
        key.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='docker-key-', dir=self.control) as temp:
            candidate = Path(temp) / 'docker.asc'
            self.run(['curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https', '--tlsv1.2',
                      'https://download.docker.com/linux/ubuntu/gpg', '-o', str(candidate)])
            rows = self.run(['gpg', '--show-keys', '--with-colons', str(candidate)], capture=True).splitlines()
            fingerprints = [row.split(':')[9] for row in rows if row.startswith('fpr:')]
            if not fingerprints or fingerprints[0] != '9DC858229FC7DD38854AE2D88D81803C0EBFCD88':
                raise SetupError('Docker package signing-key fingerprint mismatch.')
            atomic(key, candidate.read_bytes(), 0o644)
        atomic(sources, ('Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: ' + self.codename +
                        '\nComponents: stable\nArchitectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n').encode(), 0o644)
        self.run(['apt-get', 'update'])
        self.run(['apt-get', 'install', '-y', 'docker-ce', 'docker-ce-cli', 'containerd.io', 'docker-buildx-plugin', 'docker-compose-plugin'])
        self.run(['systemctl', 'enable', '--now', 'docker'])
        if not self.docker_ready():
            raise SetupError('Docker did not become ready after installation.')

    def login(self, refresh=False):
        if self.authenticated and not refresh:
            return
        config_file = self.registry_config / 'config.json'
        if not refresh and regular(config_file):
            secure_dir(self.registry_config)
            config_file.chmod(0o600)
            self.env['DOCKER_CONFIG'] = str(self.registry_config)
            self.authenticated = True
            return
        info('Private image access: use a customer READ-ONLY registry token, not supplier publishing credentials.')
        username = self.args.registry_user or self.profile.get('registryUser', '')
        if not username:
            username = ask('Docker Hub read-only username / token owner')
        else:
            info('Registry account: ' + username)
        if not re.fullmatch('[A-Za-z0-9_.-]{1,100}', username):
            raise SetupError('Invalid registry username.')
        fd = getattr(self.args, 'registry_token_fd', None)
        if fd is None:
            token = getpass.getpass('Read-only Docker Hub token (hidden): ').strip()
        else:
            if fd < 3 or fd > 1024:
                raise SetupError('Invalid registry credential descriptor.')
            try:
                with os.fdopen(os.dup(fd), 'rb', closefd=True) as secret_stream:
                    token = secret_stream.readline(4097).decode('utf-8').strip()
            finally:
                os.close(fd)
                self.args.registry_token_fd = None
            if len(token) > 4096:
                raise SetupError('Registry credential exceeds accepted length.')
        if not token:
            raise SetupError('Registry token cannot be empty.')
        if self.temp_auth:
            shutil.rmtree(self.temp_auth, ignore_errors=True)
        self.temp_auth = Path(tempfile.mkdtemp(prefix='registry-', dir=self.control))
        self.temp_auth.chmod(0o700)
        self.env['DOCKER_CONFIG'] = str(self.temp_auth)
        try:
            code = self.run(['docker', 'login', '--username', username, '--password-stdin'], secret_input=token + '\n', check=False)
        finally:
            token = ''
        if code:
            raise SetupError('Read-only registry login failed. No registry token was logged or saved.')
        keep = self.args.remember_registry or consent('Remember this read-only login for future updates on THIS server?')
        if keep:
            # Docker's config auth is base64, NOT encryption. Protect with root-only filesystem access.
            info('Registry login will be stored in a root-only Docker config (not encrypted). Never use a supplier write token here.')
            secure_dir(self.registry_config)
            atomic(config_file, (self.temp_auth / 'config.json').read_bytes())
            self.env['DOCKER_CONFIG'] = str(self.registry_config)
        self.profile['registryUser'] = username
        save_json(self.profile_file, self.profile)
        self.authenticated = True

    def local_image(self, name: str):
        text = self.run(['docker', 'image', 'inspect', name], capture=True, check=False)
        try:
            data = json.loads(text)[0]
        except (TypeError, ValueError, IndexError, KeyError):
            return None
        if data.get('Os') != 'linux' or data.get('Architecture') != 'amd64':
            raise SetupError('Wrong image platform; expected linux/amd64.')
        return data

    def ensure_image(self, name: str, label: str):
        if self.local_image(name):
            info(label + ': already available and pinned.')
            return
        self.login()
        info('Downloading ' + label + '...')
        command = ['docker', 'pull', '--platform', 'linux/amd64', name]
        if self.run(command, check=False) != 0:
            info('Image download failed. Renew registry access once; network details are in the diagnostic log.')
            self.login(refresh=True)
            self.run(command)
        if not self.local_image(name):
            raise SetupError('Downloaded image could not be verified: ' + label)

    def fetch_bundle(self):
        installer = self.descriptor['installer']
        self.ensure_image(installer, 'private installer')
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.stage = Path(tempfile.mkdtemp(prefix='.' + self.root.name + '-stage-', dir=self.root.parent))
        self.stage.chmod(0o700)
        cid = self.run(['docker', 'create', '--platform', 'linux/amd64', installer], capture=True).strip()
        if not re.fullmatch('[0-9a-f]{12,64}', cid):
            raise SetupError('Docker did not return a valid installer container ID.')
        try:
            self.run(['docker', 'cp', cid + ':/bundle/.', str(self.stage)])
        finally:
            self.run(['docker', 'rm', cid], check=False)
        manifest = validate_bundle(self.stage, self.descriptor)
        for service in SERVICES:
            self.ensure_image(manifest['images'][service]['name'], service)
        return manifest

    def https_port(self):
        # CLI override, persisted configuration, then the historical default.
        chosen = getattr(self.args, 'https_port', None)
        if chosen is None and (self.root / 'deploy/.env').is_file():
            for line in (self.root / 'deploy/.env').read_text().splitlines():
                if line.startswith('CSC_HTTPS_PORT='):
                    chosen = line.partition('=')[2].strip().strip('\"')
                    break
        if chosen is None:
            chosen = self.profile.get('installation', {}).get('https_port', 443)
        try:
            value = int(chosen)
        except (TypeError, ValueError):
            raise SetupError('HTTPS port must be 443 or between 1024 and 65535.') from None
        if not 1 <= value <= 65535 or (value < 1024 and value != 443):
            raise SetupError('HTTPS port must be 443 or between 1024 and 65535.')
        return value

    @staticmethod
    def gateway_ingress_compose(source):
        # Keep control internal: publish gateway HTTPS only through an independent
        # non-internal ingress bridge. Never attach PostgreSQL/cache to ingress.
        gateway = '      control: {ipv4_address: "${CSC_PROXY_IP:?Required}"}\n'
        gateway_with_ingress = gateway + '      ingress: {}\n'
        ingress_definition = '  ingress: {driver: bridge, internal: false}\n'
        egress = '  egress: {}\n'
        if source.count(gateway_with_ingress) == 1 and source.count(ingress_definition) == 1:
            return source
        if (source.count(gateway) != 1 or source.count(egress) != 1 or
                '  ingress:' in source):
            raise SetupError('Unknown CSC Compose network topology; refusing unsafe ingress modification.')
        return source.replace(gateway, gateway_with_ingress, 1).replace(
            egress, egress + ingress_definition, 1)

    def approved_compose(self):
        # Compare against exact approved supplier payload plus managed HTTPS port
        # and managed ingress-network adaptations. Reject all unrelated edits.
        local = self.root / 'deploy/compose.yaml'
        approved = self.stage / 'deploy/compose.yaml'
        if not regular(local) or not regular(approved):
            return False
        current = local.read_text(encoding='utf-8')
        original = approved.read_text(encoding='utf-8')
        port_token = '${CSC_BIND_ADDRESS:-0.0.0.0}:${CSC_HTTPS_PORT:-443}:8443'
        original_token = '${CSC_BIND_ADDRESS:-0.0.0.0}:443:8443'
        try:
            ingress = self.gateway_ingress_compose(original)
        except SetupError:
            return False
        variants = (original, ingress)
        return any(current == variant or (
            current.count(port_token) == 1 and
            current.replace(port_token, original_token, 1) == variant
        ) for variant in variants)

    def ensure_https_port(self):
        # Modify only the approved gateway port expression, not app source or secrets.
        port = self.https_port()
        filename = self.root / 'deploy/compose.yaml'
        if not regular(filename):
            raise SetupError('CSC Compose configuration is unavailable.')
        source = filename.read_text(encoding='utf-8')
        legacy = '${CSC_BIND_ADDRESS:-0.0.0.0}:443:8443'
        managed = '${CSC_BIND_ADDRESS:-0.0.0.0}:${CSC_HTTPS_PORT:-443}:8443'
        if source.count(managed) != 1:
            if source.count(legacy) != 1:
                raise SetupError('Unrecognized CSC gateway port mapping; refusing to modify Compose.')
            source = source.replace(legacy, managed, 1)
        source = self.gateway_ingress_compose(source)
        env_controller = module_at(self.root)
        values = env_controller.env_values()
        previous = int(values.get('CSC_HTTPS_PORT', '443'))
        bind = values.get('CSC_BIND_ADDRESS', '0.0.0.0')
        if previous != port:
            with socket.socket() as sock:
                try:
                    sock.bind((bind, port))
                except OSError:
                    raise SetupError('HTTPS port ' + str(port) + ' is occupied. Other services are preserved.') from None
        if filename.read_text(encoding='utf-8') != source:
            atomic(filename, source.encode(), 0o644)
        if str(values.get('CSC_HTTPS_PORT', '')) != str(port):
            values['CSC_HTTPS_PORT'] = str(port)
            env_controller.write_env(values)
        self.profile.setdefault('installation', {})['https_port'] = port
        save_json(self.profile_file, self.profile)
        info('CSC HTTPS port: ' + str(port) + ' (other services unchanged).')

    def compose(self, *command, capture=False, check=True):
        return self.run(['docker', 'compose', '--env-file', str(self.root / 'deploy/.env'),
                         '-f', str(self.root / 'deploy/compose.yaml'), *command], capture=capture, check=check)

    def controller(self, *command):
        return self.run([sys.executable, '-u', str(self.root / 'deploy/cscctl.py'), *command])

    def check_network(self, proposed: str):
        subnet = ipaddress.ip_network(proposed, strict=True)
        if subnet.version != 4 or subnet.prefixlen != 24 or not subnet.is_private:
            raise SetupError('The control network must be an unused private IPv4 /24.')
        ids = self.run(['docker', 'network', 'ls', '-q'], capture=True).split()
        existing = []
        if ids:
            for net in json.loads(self.run(['docker', 'network', 'inspect', *ids], capture=True)):
                for ipam in (net.get('IPAM', {}).get('Config') or []):
                    if ipam.get('Subnet'):
                        existing.append(ipaddress.ip_network(ipam['Subnet'], strict=False))
        if shutil.which('ip'):
            for row in json.loads(self.run(['ip', '-j', '-4', 'route'], capture=True)):
                if row.get('dst') and row['dst'] != 'default':
                    existing.append(ipaddress.ip_network(row['dst'], strict=False))
        if any(net.version == 4 and net.overlaps(subnet) for net in existing):
            raise SetupError('Selected control subnet overlaps an existing host/Docker network: ' + proposed)

    def verify_project_owner(self, state):
        ids = self.run(['docker', 'ps', '-a', '--filter', 'label=com.docker.compose.project=csc-platform', '--format', '{{.ID}}'], capture=True).split()
        if ids:
            records = json.loads(self.run(['docker', 'inspect', *ids], capture=True))
            expected = {str(self.root), str(self.root / 'deploy')}
            for record in records:
                labels = record.get('Config', {}).get('Labels') or {}
                if labels.get('com.docker.compose.project.working_dir') not in expected:
                    raise SetupError('Another CSC Compose installation uses this project name. No containers were changed.')
        if state in ('fresh', 'uninitialized'):
            code = self.run(['docker', 'volume', 'inspect', 'csc-platform_pgdata'], check=False)
            if code == 0:
                raise SetupError('A CSC database volume already exists but customer configuration is absent. Refusing to generate replacement secrets or initialize over existing data.')

    def choices(self):
        info('STEP 4 — Server Configuration')
        info('--------------------------------')
        cached = self.profile.get('installation', {})
        hostname = self.args.hostname or ask('Management hostname', cached.get('hostname') or socket.gethostname())
        if len(hostname) > 253 or not all(re.fullmatch('[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', s) for s in hostname.split('.')):
            raise SetupError('Enter a DNS hostname, not a URL.')
        port = self.https_port()
        info('HTTPS port: ' + str(port))
        cert = self.args.tls_cert or cached.get('tls_cert', '/etc/csc-platform/tls/server.crt')
        key = self.args.tls_key or cached.get('tls_key', '/etc/csc-platform/tls/server.key')
        if not (regular(Path(cert)) and regular(Path(key))):
            mode = ask('HTTPS: existing certificate, or self-signed certificate for an isolated lab? (existing/lab)', 'existing')
            if mode == 'lab':
                if not consent('Create a self-signed LAB certificate? Browsers will require explicit trust; not for customer production.'):
                    raise SetupError('Certificate generation cancelled.')
                tls = self.control / 'lab-tls'
                secure_dir(tls)
                cert, key = str(tls / 'server.crt'), str(tls / 'server.key')
                if Path(cert).exists() or Path(key).exists():
                    raise SetupError('Existing lab TLS files are preserved. Supply their paths instead of replacing them.')
                self.run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes', '-days', '90',
                          '-keyout', key, '-out', cert, '-subj', '/CN=' + hostname, '-addext', 'subjectAltName=DNS:' + hostname])
                Path(key).chmod(0o600)
            elif mode == 'existing':
                cert, key = ask('Full certificate-chain PEM path', cert), ask('Matching private-key PEM path', key)
            else:
                raise SetupError('Choose existing or lab.')
        cert, key = Path(cert).expanduser().absolute(), Path(key).expanduser().absolute()
        if not regular(cert) or not regular(key):
            raise SetupError('Certificate and private key must be regular files.')
        self.run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkhost', hostname])
        self.run(['openssl', 'x509', '-in', str(cert), '-noout', '-checkend', '604800'])
        cp = self.run(['openssl', 'x509', '-in', str(cert), '-noout', '-pubkey'], capture=True).strip()
        kp = self.run(['openssl', 'pkey', '-in', str(key), '-passin', 'pass:', '-pubout'], capture=True).strip()
        if cp != kp:
            raise SetupError('Certificate and private key do not match.')
        if not Path('/etc/ssl/certs/ca-certificates.crt').is_file():
            raise SetupError('Install the Ubuntu ca-certificates package before continuing.')
        ca = Path(self.args.ca_cert).absolute() if self.args.ca_cert else None
        if ca:
            if not regular(ca) or b'PRIVATE KEY' in ca.read_bytes():
                raise SetupError('CA input must contain only a certificate.')
            self.run(['openssl', 'x509', '-in', str(ca), '-noout'])
        subnet = self.args.subnet or cached.get('subnet', '172.30.88.0/24')
        try:
            self.check_network(subnet)
        except SetupError:
            if self.args.subnet:
                raise
            subnet = ask('Default subnet is in use. Enter an unused private IPv4 /24')
            self.check_network(subnet)
        bind = self.args.bind_address or cached.get('bind_address', '0.0.0.0')
        if ipaddress.ip_address(bind).version != 4:
            raise SetupError('The HTTPS bind address must be IPv4 for this release.')
        with socket.socket() as check:
            try:
                check.bind((bind, port))
            except OSError:
                raise SetupError('HTTPS port ' + str(port) + ' is already occupied at ' + bind + '. Existing services were not stopped.') from None
        info('STEP 5 — Administrator Account')
        info('--------------------------------')
        email = self.args.admin_email or ask('Administrator email', cached.get('admin_email', ''))
        if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', email):
            raise SetupError('Enter a valid administrator email.')
        choice = dict(hostname=hostname, admin_email=email, tls_cert=str(cert), tls_key=str(key),
                      ca_cert=str(ca) if ca else None, subnet=subnet, bind_address=bind, no_cache=self.args.no_cache, https_port=port)
        self.profile['installation'] = choice
        save_json(self.profile_file, self.profile)
        return choice, prompt_password()

    def initialize_staged(self, choice, password):
        controller = module_at(self.stage)
        options = types.SimpleNamespace(**choice)
        options.tls_cert, options.tls_key = Path(options.tls_cert), Path(options.tls_key)
        options.ca_cert = Path(options.ca_cert) if options.ca_cert else None
        # Feed two already-validated answers IN MEMORY; no CLI/env/plaintext staging file.
        controller.getpass = types.SimpleNamespace(getpass=lambda _prompt: password)
        try:
            with self.log.open('a', encoding='utf-8') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
                controller.initialize(options)
            values = controller.env_values()
            values['CSC_SECRETS_DIR'] = str(self.root / '.deploy/secrets')
            values['CSC_KEYS_DIR'] = str(self.root / '.deploy/keys')
            controller.write_env(values)
        finally:
            controller.getpass = getpass
            password = ''
        classify(self.stage)  # Full initialization must succeed before destination changes.
        journal = self.control / 'prepared-install.json'
        record = {'target': str(self.root), 'stage': str(self.stage), 'release': self.descriptor['release'],
                  'installer': self.descriptor['installer'], 'manifestHash': sha(self.stage / 'release.json')}
        save_json(journal, record)
        self.stage = None  # Prepared secrets are now journaled; retain them if commit is interrupted.
        self.finish_prepared(record)

    def finish_prepared(self, record):
        prepared = safe_path(Path(record['stage']))
        if record.get('target') != str(self.root) or prepared.parent != self.root.parent or not prepared.name.startswith('.' + self.root.name + '-stage-'):
            raise SetupError('Unsafe prepared installation journal.')
        if prepared.exists():
            if classify(prepared) != 'initialized' or sha(prepared / 'release.json') != record['manifestHash']:
                raise SetupError('Prepared installation is incomplete; existing secrets preserved.')
            if self.root.exists():
                if classify(self.root) != 'uninitialized':
                    raise SetupError('Customer state appeared before installation commit; refusing replacement.')
                backup = self.root.parent / (self.root.name + '.preinit-' + uuid.uuid4().hex[:8])
                os.replace(self.root, backup)
            os.replace(prepared, self.root)
        elif not self.root.exists() or classify(self.root) not in ('initialized', 'started') or sha(self.root / 'release.json') != record['manifestHash']:
            raise SetupError('Prepared installation journal could not be recovered safely.')
        self.stage = None
        (self.control / 'prepared-install.json').unlink(missing_ok=True)
        info('Customer configuration committed. Password, keys and identity were generated once.')

    def state_services(self):
        raw = self.compose('ps', '--all', '--format', 'json', capture=True)
        if not raw.strip():
            return []
        try:
            value = json.loads(raw)
            return value if isinstance(value, list) else [value]
        except ValueError:
            return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def expected_services(self):
        conf = read_json(self.root / '.deploy/secrets/runtime_config')
        return ['postgres', 'api', 'worker', 'gateway'] + (['cache'] if conf.get('Cache', {}).get('Enabled') else [])

    def health(self, strict=False):
        states = {row.get('Service'): row for row in self.state_services()}
        bad = []
        for service in self.expected_services():
            row = states.get(service, {})
            state, health = row.get('State', 'not created'), row.get('Health', '')
            info(service.ljust(10) + ': ' + state + (' / ' + health if health else ''))
            if state != 'running' or health in ('unhealthy', 'starting') or (service in ('api', 'postgres') and health != 'healthy'):
                bad.append(service)
        if strict and bad:
            raise SetupError('Services are not ready: ' + ', '.join(bad) + '. Data/configuration preserved. Details: ' + str(self.log))
        if strict and not bad:
            controller = module_at(self.root)
            bind = controller.env_values().get('CSC_BIND_ADDRESS', '0.0.0.0')
            address = '127.0.0.1' if bind == '0.0.0.0' else bind
            hostname = (self.root / '.deploy/hostname').read_text().strip()
            port = self.https_port()
            self.run(['curl', '--fail', '--silent', '--show-error', '--connect-timeout', '5', '--max-time', '30',
                      '--noproxy', '*', '--resolve', hostname + ':' + str(port) + ':' + address,
                      '--cacert', str(self.root / '.deploy/secrets/tls_cert'), 'https://' + hostname + ':' + str(port) + '/health/ready'])
            info('HTTPS gateway readiness passed against the installed certificate.')
        return not bad

    def pg_major(self, manifest):
        data = self.local_image(manifest['images']['postgres']['name'])
        if data is None:
            raise SetupError('PostgreSQL image is unavailable.')
        values = [v.split('=', 1)[1] for v in data.get('Config', {}).get('Env', []) if v.startswith('PG_MAJOR=')]
        if len(values) != 1 or not values[0].isdigit():
            raise SetupError('Cannot establish PostgreSQL major version safely.')
        return values[0]

    def backup(self, maintenance=False):
        if not shutil.which('age'):
            if not consent('Install the age backup-encryption tool from Ubuntu packages?'):
                raise SetupError('Encrypted backup is mandatory before an upgrade.')
            self.run(['apt-get', 'update'])
            self.run(['apt-get', 'install', '-y', 'age'])
        recipient = self.args.backup_recipient or self.profile.get('backupRecipient') or ask('Backup AGE public recipient (private recovery key must be kept elsewhere)')
        if not re.fullmatch('age1[0-9a-z]{58}', recipient):
            raise SetupError('Enter an age X25519 PUBLIC recipient. Do not enter a private identity.')
        self.profile['backupRecipient'] = recipient
        save_json(self.profile_file, self.profile)
        directory = safe_path(Path('/var/backups/csc-platform'))
        secure_dir(directory)
        output = directory / ('csc-' + dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.tar.age')
        services = self.state_services()
        active = [r['Service'] for r in services if r.get('State') == 'running' and r.get('Service') in ('api', 'worker', 'gateway')]
        info('Creating an encrypted database/configuration backup. Application writes are stopped during the snapshot.')
        self.compose('stop', 'gateway', 'api', 'worker')
        # CSC_AUTO_UPGRADE_BOOTSTRAP_V9: a previously started installation may
        # have had only its CSC containers removed. Start ONLY the existing
        # CSC PostgreSQL service from the OLD release before pg_dump; never
        # reinitialize the volume or touch unrelated Docker projects.
        if not any(r.get('Service') == 'postgres' and r.get('State') == 'running' for r in services):
            info('Starting existing CSC PostgreSQL for the required encrypted backup.')
            self.compose('up', '-d', '--wait', 'postgres')
        success = False
        try:
            with tempfile.TemporaryDirectory(prefix='backup-', dir=self.control) as tmp:
                folder = Path(tmp)
                dump = folder / 'database.dump'
                command = ['docker', 'compose', '--env-file', str(self.root / 'deploy/.env'), '-f', str(self.root / 'deploy/compose.yaml'),
                           'exec', '-T', 'postgres', 'pg_dump', '-U', 'postgres', '-d', 'csc', '-Fc']
                with dump.open('wb') as stream, self.log.open('ab') as log:
                    result = subprocess.run(command, env=self.env, stdout=stream, stderr=log)
                dump.chmod(0o600)
                if result.returncode or dump.stat().st_size == 0:
                    raise SetupError('Database backup failed; no upgrade was applied.')
                with self.log.open('ab') as log:
                    process = subprocess.Popen(['age', '-r', recipient, '-o', str(output)], stdin=subprocess.PIPE, stdout=log, stderr=log)
                    try:
                        with tarfile.open(fileobj=process.stdin, mode='w|', dereference=False) as archive:
                            archive.add(dump, arcname='database.dump')
                            for name, path in [('secrets', self.root / '.deploy/secrets'), ('keys', self.root / '.deploy/keys'),
                                               ('release.json', self.root / 'release.json'), ('deploy', self.root / 'deploy')]:
                                if path.is_dir():
                                    for child in path.rglob('*'):
                                        if child.is_symlink() or not (child.is_dir() or child.is_file()):
                                            raise SetupError('Unsafe file in backup input.')
                                archive.add(path, arcname=name)
                            for name in ('hostname', 'running-release.json'):
                                path = self.root / '.deploy' / name
                                if path.exists():
                                    archive.add(path, arcname='state/' + name)
                        process.stdin.close()
                        if process.wait() != 0:
                            raise SetupError('Backup encryption failed.')
                    except BaseException:
                        process.kill()
                        process.wait()
                        raise
            output.chmod(0o600)
            record = {'timestamp': time.time(), 'path': str(output), 'sha256': sha(output),
                      'sourceRelease': read_json(self.root / 'release.json')['release']}
            save_json(self.root / '.deploy/last-backup.json', record)
            atomic(output.with_suffix(output.suffix + '.sha256'), (record['sha256'] + '  ' + output.name + '\n').encode())
            info('Encrypted backup saved: ' + str(output))
            info('Copy the encrypted backup off-server. Its recovery private key must be retained separately.')
            success = True
            return record
        finally:
            if not success:
                output.unlink(missing_ok=True)
            # During upgrades remain in maintenance even on failure; no speculative startup.
            if not maintenance and active:
                self.compose('start', '--wait', *active)

    def overlay(self, manifest, started: bool):
        old = read_json(self.root / 'release.json')
        transition(old, manifest)
        if self.pg_major(old) != self.pg_major(manifest):
            raise SetupError('PostgreSQL major-version upgrades require a separate reviewed data-migration procedure.')
        before = secret_fingerprints(self.root)
        journal_file = self.control / 'upgrade.json'
        journal = read_json(journal_file) if journal_file.exists() else None
        # A never-started site may recover from a failed release using a newer,
        # approved release. A site that has ever started must take the normal
        # backed-up upgrade path; never silently discard its transition journal.
        if journal and journal.get('phase') != 'complete' and (
            journal.get('targetRelease') != manifest['release'] or
            journal.get('installer') != self.descriptor['installer']
        ):
            recovery_allowed = (
                not started and not journal.get('wasStarted') and
                not (self.root / '.deploy/running-release.json').exists() and
                journal.get('phase') in ('prepared', 'staged') and
                isinstance(journal.get('targetRelease'), str) and
                isinstance(journal.get('fromRelease'), str) and
                journal.get('secretHashes') == before and
                old['release'] in (journal['fromRelease'], journal['targetRelease']) and
                version_key(manifest['release']) > version_key(journal['targetRelease'])
            )
            if not recovery_allowed:
                raise SetupError('An interrupted transition targets a different release. Recovery is refused unless the site never started and the newer release preserves its secrets.')
            # Preserve the previous recovery evidence and snapshot. The new
            # journal is only created after this archive has been written.
            history = self.control / 'transition-history'
            secure_dir(history)
            archived = history / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8] + '.json')
            atomic(archived, journal_file.read_bytes())
            journal = None
            info('Superseding unfinished never-started release with newer approved release; prior transition journal archived.')
        if journal and journal.get('phase') != 'complete':
            if journal.get('secretHashes') != before:
                raise SetupError('Customer secrets changed during an incomplete transition; automatic continuation refused.')
            if journal.get('wasStarted'):
                check = Path((journal.get('backup') or {}).get('path', ''))
                if not regular(check) or sha(check) != journal['backup'].get('sha256'):
                    raise SetupError('Required pre-upgrade encrypted backup is missing or changed.')
                if time.time() - journal['backup']['timestamp'] > 86400:
                    raise SetupError('The pre-upgrade backup is older than 24 hours; review before continuing. No data was deleted.')
        else:
            snapshot = self.control / 'snapshots' / (dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8])
            secure_dir(snapshot)
            for name in PAYLOAD_FILES | {'deploy/.env'}:
                source = self.root / name
                if regular(source):
                    dest = snapshot / name
                    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(source, dest)
            backup = self.backup(maintenance=True) if started else None
            journal = {'phase': 'prepared', 'fromRelease': old['release'], 'targetRelease': manifest['release'],
                       'installer': self.descriptor['installer'], 'fileSnapshot': str(snapshot), 'backup': backup,
                       'secretHashes': before, 'wasStarted': started}
            save_json(journal_file, journal)
        self.compose('stop', 'gateway', 'api', 'worker')
        for name in sorted(PAYLOAD_FILES - {'release.json'}):
            target = self.root / name
            atomic(target, (self.stage / name).read_bytes(), 0o755 if name.endswith(('.sh', '.py')) else 0o644)
        # Commit the manifest last. A recorded prepared transition can be reapplied after interruption.
        atomic(self.root / 'release.json', (self.stage / 'release.json').read_bytes(), 0o644)
        if secret_fingerprints(self.root) != before:
            raise SetupError('Customer secret integrity check failed.')
        journal['phase'] = 'staged'
        save_json(journal_file, journal)
        info('Approved release staged. Existing administrator, installation identity, TLS and data are preserved.')

    def start(self):
        desired = read_json(self.root / 'release.json')['release']
        running = self.root / '.deploy/running-release.json'
        args = ['up']
        if running.exists() and read_json(running).get('release') != desired:
            args += ['--ack-upgrade', desired]
        if (getattr(self.args, 'https_port', None) is not None or
                'CSC_HTTPS_PORT=' in (self.root / 'deploy/.env').read_text()):
            self.ensure_https_port()
        info('STEP 6 — Installation')
        info('--------------------------------')
        info('Starting database, applying migrations and starting application services...')
        self.controller(*args)
        self.health(strict=True)
        journal = self.control / 'upgrade.json'
        if journal.exists():
            value = read_json(journal)
            value['phase'] = 'complete'
            save_json(journal, value)
        save_json(self.control / 'installed.json', {'installer': self.descriptor['installer'], 'release': desired})
        hostname = (self.root / '.deploy/hostname').read_text().strip()
        info('READY: https://' + hostname + ':' + str(self.https_port()) + ' | administrator login: administrator')
        info('Commission source mappings, product license and CSC access before enabling synchronization.')

    def execute(self):
        state = classify(self.root)
        action = self.args.action
        if action == 'check':
            info('State: ' + state + ' | Docker/Compose: ' + ('ready' if self.docker_ready() else 'not ready'))
            return
        self.ensure_docker()
        self.verify_project_owner(state)
        pending = self.control / 'prepared-install.json'
        if pending.exists() and action in ('install', 'resume'):
            self.finish_prepared(read_json(pending))
            state = classify(self.root)
        if action in ('status', 'verify', 'backup'):
            if state not in ('initialized', 'started'):
                info('State: ' + state + '. Run install.sh to initialize/resume.')
                if action != 'status':
                    raise SetupError('Installation has not been initialized.')
                return
            if action == 'backup':
                self.backup()
            elif action == 'verify':
                controller = module_at(self.root)
                controller.verify_images()
                self.health(strict=True)
                info('Runtime image/platform and service-readiness checks passed. This is not live CSC acceptance.')
            else:
                info('Installed release: ' + read_json(self.root / 'release.json')['release'])
                self.health()
            return
        if action == 'upgrade' and state not in ('initialized', 'started'):
            raise SetupError('No configured installation to upgrade. Run install.sh.')
        if state == 'started':
            old = read_json(self.root / 'release.json')
            journal = self.control / 'upgrade.json'
            pending_transition = journal.exists() and read_json(journal).get('phase') != 'complete'
            if old['release'] == self.descriptor['release'] and not pending_transition:
                if getattr(self.args, 'https_port', None) is None and self.health():
                    info('Already running the approved release; nothing was reinstalled.')
                    return
            if old['release'] != self.descriptor['release'] and action != 'upgrade':
                journal = self.control / 'upgrade.json'
                in_progress = journal.exists() and read_json(journal).get('phase') != 'complete'
                if not in_progress:
                    raise SetupError('An established installation is preserved. Run upgrade.sh for a backed-up update.')
            if action == 'upgrade' and old['release'] != self.descriptor['release']:
                info('Upgrade: ' + old['release'] + ' -> ' + self.descriptor['release'])
                if not self.args.yes and not consent('Continue with encrypted backup, maintenance and upgrade?'):
                    raise SetupError('Upgrade cancelled; installation is unchanged.')
        manifest = self.fetch_bundle()
        choice = password = None
        if state in ('fresh', 'uninitialized'):
            choice, password = self.choices()
        if state in ('fresh', 'uninitialized'):
            self.initialize_staged(choice, password)
            password = None
        else:
            old = read_json(self.root / 'release.json')
            transition(old, manifest)
            # Any pending journal is replayed, including a crash between individual file updates.
            changed = any(sha(self.root / name) != sha(self.stage / name) for name in PAYLOAD_FILES - {'deploy/compose.yaml'} if regular(self.root / name)) or not self.approved_compose()
            missing = any(not regular(self.root / name) for name in PAYLOAD_FILES)
            journal = self.control / 'upgrade.json'
            incomplete = journal.exists() and read_json(journal).get('phase') != 'complete'
            if changed or missing or incomplete:
                self.overlay(manifest, started=state == 'started')
        self.start()


def arguments():
    parser = argparse.ArgumentParser(description='CSC guided installation and maintenance. Secrets and image digests are handled internally.')
    parser.add_argument('action', nargs='?', default='install', choices=['install', 'resume', 'upgrade', 'status', 'verify', 'backup', 'check'])
    parser.add_argument('--install-dir', default='/opt/csc-platform')
    parser.add_argument('--hostname')
    parser.add_argument('--admin-email')
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    parser.add_argument('--ca-cert')
    parser.add_argument('--registry-user')
    parser.add_argument('--registry-token-fd', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--remember-registry', action='store_true', help='Explicitly consent to root-only, non-encrypted storage of a READ-ONLY registry login')
    parser.add_argument('--no-cache', action='store_true')
    parser.add_argument('--subnet')
    parser.add_argument('--bind-address')
    parser.add_argument('--https-port', type=int, help='Dedicated host HTTPS port, e.g. 18443; persisted after installation')
    parser.add_argument('--install-docker', action='store_true')
    parser.add_argument('--backup-recipient', help='AGE public recipient; never a private identity')
    parser.add_argument('--use-local-release', action='store_true', help='For upgrade: use the reviewed local release pointer instead of refreshing it from public GitHub')
    parser.add_argument('--yes', action='store_true', help='Approve upgrade maintenance; does not bypass credential/password/backup checks')
    return parser.parse_args()


def approved_online_release():
    # Fetch METADATA only. We never download/execute new Python or shell code here.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise SetupError('Unexpected redirect when reading approved release metadata.')
    url = 'https://raw.githubusercontent.com/scharfesicht/csc-platform/main/bootstrap/release.json'
    info('Checking the approved ScharfeSicht release...')
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(url, timeout=30) as response:
            data = response.read(65537)
        if len(data) > 65536:
            raise SetupError('Release metadata is unexpectedly large.')
        value = json.loads(data)
    except Exception as exc:
        raise SetupError('Cannot retrieve approved release metadata. No services were changed. Use --use-local-release only for an intentionally pinned installation.') from exc
    if not isinstance(value, dict):
        raise SetupError('Invalid public release metadata.')
    if (value.get('product'), value.get('schemaVersion'), value.get('platform')) != ('CSCIntegrationPlatform', 1, 'linux/amd64'):
        raise SetupError('Unsupported public release metadata contract.')
    if not PIN.fullmatch(value.get('installer', '')) or not VERSION.fullmatch(value.get('release', '')):
        raise SetupError('Unapproved remote installer reference.')
    if version_key(value.get('minimumBootstrap', BOOTSTRAP_VERSION)) > version_key(BOOTSTRAP_VERSION):
        raise SetupError('This release needs a newer public bootstrap. Update the reviewed public checkout; no services were changed.')
    return value


def main():
    args = arguments()
    os.umask(0o077)
    verify_checkout(HERE.parent)
    descriptor = load_descriptor(HERE / 'release.json')
    # CSC_AUTO_UPGRADE_BOOTSTRAP_V9: the same public installer handles fresh and
    # started installations. The existing upgrade transaction, consent prompt,
    # encrypted backup, release checks and journal remain mandatory.
    if args.action == 'install' and classify(safe_path(Path(args.install_dir))) == 'started':
        info('Existing CSC installation detected: selecting protected upgrade/verify path.')
        args.action = 'upgrade'
    if args.action == 'upgrade' and not args.use_local_release:
        descriptor = approved_online_release()
    coordinator = Coordinator(args, descriptor)
    coordinator.preflight()
    with coordinator.session():
        coordinator.execute()


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print('[CSC] STOPPED: ' + (str(exc) or 'Cancelled.') + '\nNo database volumes were deleted. Existing identity and passwords were not reset.', file=sys.stderr)
        sys.exit(1)
