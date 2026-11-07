"""Offline bootstrap orchestration regression tests. No Docker/network/root actions."""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('csc_public_workflow', ROOT / 'bootstrap/csc_setup.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def manifest(version='1.0.0-rc2'):
    return {'product': 'CSCIntegrationPlatform', 'distribution': 'dockerhub', 'registryNamespace': 'scharfesicht',
            'release': version, 'publicKeySha256': 'c' * 64,
            'images': {s: {'name': 'docker.io/scharfesicht/csc-platform-' + s + '@sha256:' + 'a' * 64,
                           'id': 'sha256:' + 'b' * 64} for s in M.SERVICES}}


def fixture(root, version='1.0.0-rc2', initialized=False, started=False):
    root.mkdir(parents=True, exist_ok=True)
    for name in M.PAYLOAD_FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('reviewed fixture ' + name)
    (root / 'release.json').write_text(json.dumps(manifest(version)))
    if initialized:
        (root / '.deploy/secrets').mkdir(parents=True)
        (root / '.deploy/keys').mkdir()
        for name in M.REQUIRED_SECRETS:
            (root / '.deploy/secrets' / name).write_text('preserve-' + name)
        (root / '.deploy/secrets/runtime_config').write_text(json.dumps({'Cache': {'Enabled': True}}))
        (root / '.deploy/hostname').write_text('app-test-server')
        (root / 'deploy/.env').write_text('CSC_SECRETS_DIR=' + str(root / '.deploy/secrets') + '\n')
    if started:
        (root / '.deploy/running-release.json').write_text(json.dumps(manifest(version)))
    return root


def arguments(root, action='install'):
    return argparse.Namespace(install_dir=str(root), action=action, registry_user=None, remember_registry=False,
                              hostname=None, admin_email=None, tls_cert=None, tls_key=None, ca_cert=None,
                              subnet=None, bind_address=None, no_cache=False, install_docker=False,
                              backup_recipient=None, yes=True, use_local_release=True)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.descriptor = {'product': 'CSCIntegrationPlatform', 'schemaVersion': 1, 'release': '1.0.0-rc2',
                           'platform': 'linux/amd64', 'minimumBootstrap': '1.0.0-rc5',
                           'installer': 'docker.io/scharfesicht/csc-platform-installer@sha256:' + 'd' * 64}

    def tearDown(self):
        self.tmp.cleanup()

    def coordinator(self, root, action='install'):
        value = M.Coordinator(arguments(root, action), self.descriptor)
        value.control = self.base / 'control'
        value.control.mkdir(exist_ok=True)
        value.profile_file = value.control / 'preferences.json'
        value.log = value.control / 'test.log'
        value.log.touch()
        value.profile = {}
        value.ensure_docker = mock.Mock()
        value.verify_project_owner = mock.Mock()
        value.compose = mock.Mock(return_value='')
        value.pg_major = mock.Mock(return_value='18')
        value.health = mock.Mock(return_value=True)
        return value

    def test_complete_public_checkout_integrity(self):
        M.verify_checkout(ROOT)

    def test_shell_wrappers_parse(self):
        for path in [*ROOT.glob('*.sh'), ROOT / 'bootstrap/csc-bootstrap.sh']:
            subprocess.run(['bash', '-n', str(path)], check=True)

    def test_help_needs_no_root_or_network(self):
        result = subprocess.run(['bash', str(ROOT / 'install.sh'), '--help'], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('install-dir', result.stdout)

    def test_ubuntu_variants_do_not_overwrite_bootstrap_version(self):
        for release in ('24.04', '24.04.2', '26.04', '26.04.1'):
            parsed, codename = M.parse_os('ID=ubuntu\nVERSION_ID="' + release + '"\nVERSION="Untrusted label"')
            self.assertEqual(parsed, release)
            self.assertEqual(codename, 'noble' if release.startswith('24') else 'resolute')
        self.assertEqual(M.BOOTSTRAP_VERSION, '1.0.0-rc5')
        with self.assertRaises(M.SetupError):
            M.parse_os('ID=debian\nVERSION_ID="26.04"')

    def test_current_published_pointer(self):
        current = M.load_descriptor(ROOT / 'bootstrap/release.json')
        self.assertEqual(current['product'], 'CSCIntegrationPlatform')
        self.assertRegex(current['installer'], M.PIN)
        self.assertRegex(current['release'], M.VERSION)

    def test_manifest_rejects_namespace_tag_and_missing_service(self):
        for mutation in ('namespace', 'tag', 'missing'):
            item = manifest()
            if mutation == 'namespace': item['registryNamespace'] = 'other'
            if mutation == 'tag': item['images']['api']['name'] = 'docker.io/scharfesicht/csc-platform-api:latest'
            if mutation == 'missing': del item['images']['cache']
            with self.assertRaises(M.SetupError): M.validate_manifest(item)

    def test_bundle_allowlist_and_manifest_match(self):
        root = fixture(self.base / 'payload')
        M.validate_bundle(root, self.descriptor)
        (root / 'unexpected.env').write_text('not-allowed')
        with self.assertRaises(M.SetupError): M.validate_bundle(root, self.descriptor)

    def test_symlink_payload_refused(self):
        root = fixture(self.base / 'payload')
        try: (root / 'link').symlink_to(root / 'release.json')
        except OSError: self.skipTest('Symlink privilege not available')
        with self.assertRaises(M.SetupError): M.validate_bundle(root, self.descriptor)

    def test_installation_states(self):
        self.assertEqual(M.classify(self.base / 'fresh'), 'fresh')
        self.assertEqual(M.classify(fixture(self.base / 'uninitialized')), 'uninitialized')
        self.assertEqual(M.classify(fixture(self.base / 'configured', initialized=True)), 'initialized')
        self.assertEqual(M.classify(fixture(self.base / 'started', initialized=True, started=True)), 'started')

    def test_partial_state_never_reinitializes(self):
        root = fixture(self.base / 'partial')
        (root / '.deploy').mkdir()
        with self.assertRaises(M.SetupError): M.classify(root)

    def test_path_symlink_refused(self):
        path = self.base / 'alias'
        try: path.symlink_to(self.base / 'elsewhere', target_is_directory=True)
        except OSError: self.skipTest('Symlink privilege not available')
        with self.assertRaises(M.SetupError): M.safe_path(path / 'child')

    def test_downgrade_trust_change_and_same_release_image_change_refused(self):
        for new in [manifest('1.0.0-rc1'), dict(manifest(), publicKeySha256='d'*64)]:
            with self.assertRaises(M.SetupError): M.transition(manifest(), new)
        new = manifest();new['images']['api']['name'] = new['images']['api']['name'][:-64] + 'e'*64
        with self.assertRaises(M.SetupError): M.transition(manifest(), new)
        M.transition(manifest('1.0.0-rc1-hf1'), manifest('1.0.0-rc2'))

    def test_password_retries_without_creating_files(self):
        strong = 'Test-Only-Passphrase-92!'
        reader = mock.Mock(side_effect=['short', strong, 'mismatch', strong, strong])
        with mock.patch('sys.stdout', new=io.StringIO()): self.assertEqual(M.prompt_password(reader), strong)
        self.assertEqual(reader.call_count, 5)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_customer_environment_excludes_supplier_api_token(self):
        with mock.patch.dict(os.environ, {'CSC_DOCKERHUB_TOKEN': 'supplier-test-secret', 'CSC_DOCKERHUB_IDENTIFIER': 'owner'}):
            value = self.coordinator(self.base / 'install')
        self.assertNotIn('CSC_DOCKERHUB_TOKEN', value.env)
        self.assertNotIn('CSC_DOCKERHUB_IDENTIFIER', value.env)

    def test_initialized_recovery_preserves_secrets_and_starts_approved_release(self):
        root = fixture(self.base / 'install', '1.0.0-rc1', initialized=True)
        stage = fixture(self.base / 'stage')
        before = M.secret_fingerprints(root)
        env = (root / 'deploy/.env').read_bytes()
        value = self.coordinator(root)
        value.stage = stage
        value.fetch_bundle = mock.Mock(return_value=manifest())
        value.choices = mock.Mock(side_effect=AssertionError('Existing site must not ask for a new password'))
        value.backup = mock.Mock(side_effect=AssertionError('Never-started recovery is not a production upgrade'))
        value.controller = mock.Mock()
        value.execute()
        self.assertEqual(M.secret_fingerprints(root), before)
        self.assertEqual((root / 'deploy/.env').read_bytes(), env)
        self.assertEqual(M.read_json(root / 'release.json')['release'], '1.0.0-rc2')
        value.controller.assert_called_once_with('up')
        self.assertEqual(M.read_json(value.control / 'upgrade.json')['phase'], 'complete')

    def test_established_site_install_refuses_implicit_upgrade(self):
        root = fixture(self.base / 'install', '1.0.0-rc1', initialized=True, started=True)
        value = self.coordinator(root)
        value.fetch_bundle = mock.Mock()
        with self.assertRaisesRegex(M.SetupError, 'upgrade.sh'): value.execute()
        value.fetch_bundle.assert_not_called()

    def test_upgrade_backup_precedes_file_replacement(self):
        root = fixture(self.base / 'install', '1.0.0-rc1', initialized=True, started=True)
        stage = fixture(self.base / 'stage')
        value = self.coordinator(root, 'upgrade');value.stage = stage
        value.fetch_bundle = mock.Mock(return_value=manifest())
        value.controller = mock.Mock()
        original = M.secret_fingerprints(root)
        def backed_up(maintenance=False):
            self.assertTrue(maintenance)
            self.assertEqual(M.read_json(root / 'release.json')['release'], '1.0.0-rc1')
            file = self.base / 'backup.age';file.write_bytes(b'encrypted fixture')
            result = {'path': str(file), 'timestamp': time.time(), 'sha256': M.sha(file)}
            M.save_json(root / '.deploy/last-backup.json', result)
            return result
        value.backup = mock.Mock(side_effect=backed_up)
        value.execute()
        value.controller.assert_called_once_with('up', '--ack-upgrade', '1.0.0-rc2')
        self.assertEqual(M.secret_fingerprints(root), original)
        value.backup.assert_called_once_with(maintenance=True)

    def test_failed_backup_does_not_replace_installed_files(self):
        root = fixture(self.base / 'install', '1.0.0-rc1', initialized=True, started=True)
        stage = fixture(self.base / 'stage')
        value = self.coordinator(root, 'upgrade');value.stage = stage
        value.backup = mock.Mock(side_effect=M.SetupError('backup failed'))
        before = (root / 'release.json').read_bytes()
        with self.assertRaises(M.SetupError): value.overlay(manifest(), started=True)
        self.assertEqual((root / 'release.json').read_bytes(), before)

    def test_database_major_change_stops_before_backup_or_overlay(self):
        root = fixture(self.base / 'install', '1.0.0-rc1', initialized=True)
        value = self.coordinator(root);value.stage = fixture(self.base / 'stage')
        value.pg_major = mock.Mock(side_effect=['18', '19']);value.backup = mock.Mock()
        with self.assertRaisesRegex(M.SetupError, 'major-version'): value.overlay(manifest(), started=False)
        value.backup.assert_not_called()
        self.assertEqual(M.read_json(root / 'release.json')['release'], '1.0.0-rc1')

    def test_interrupted_overlay_replays_without_recreating_secret(self):
        root = fixture(self.base / 'install', '1.0.0-rc1', initialized=True)
        value = self.coordinator(root);value.stage = fixture(self.base / 'stage')
        original_atomic = M.atomic
        calls = []
        def interrupt(path, data, mode=0o600):
            if path == root / 'deploy/compose.yaml' and not calls:
                calls.append('interrupted');raise OSError('simulated interruption')
            original_atomic(path, data, mode)
        before = M.secret_fingerprints(root)
        with mock.patch.object(M, 'atomic', side_effect=interrupt):
            with self.assertRaises(OSError): value.overlay(manifest(), started=False)
        self.assertEqual(M.read_json(value.control / 'upgrade.json')['phase'], 'prepared')
        value.overlay(manifest(), started=False)
        self.assertEqual(M.secret_fingerprints(root), before)
        self.assertEqual(M.read_json(value.control / 'upgrade.json')['phase'], 'staged')

    def test_never_started_rc2_failure_can_recover_with_rc3(self):
        root = fixture(self.base / 'install', '1.0.0-rc2', initialized=True)
        stage = fixture(self.base / 'stage', '1.0.0-rc3')
        before = M.secret_fingerprints(root)
        value = self.coordinator(root)
        value.descriptor = dict(self.descriptor, release='1.0.0-rc3', installer='docker.io/scharfesicht/csc-platform-installer@sha256:' + 'e'*64)
        value.stage = stage
        prior = {'phase':'staged', 'fromRelease':'1.0.0-rc1', 'targetRelease':'1.0.0-rc2',
                 'installer':self.descriptor['installer'], 'secretHashes':before, 'wasStarted':False,
                 'fileSnapshot':str(self.base / 'old-snapshot'), 'backup':None}
        M.save_json(value.control / 'upgrade.json', prior)
        value.backup = mock.Mock(side_effect=AssertionError('Never-started customer must not require upgrade backup'))
        value.overlay(manifest('1.0.0-rc3'), started=False)
        self.assertEqual(M.read_json(root / 'release.json')['release'], '1.0.0-rc3')
        self.assertEqual(M.secret_fingerprints(root), before)
        self.assertEqual(M.read_json(value.control / 'upgrade.json')['phase'], 'staged')
        archived = list((value.control / 'transition-history').glob('*.json'))
        self.assertEqual(len(archived),1)
        self.assertEqual(M.read_json(archived[0]), prior)
        value.backup.assert_not_called()

    def test_rc3_recovery_refuses_prior_started_or_changed_secrets(self):
        for reason in ('started', 'marker', 'changed'):
            with self.subTest(reason=reason):
                root = fixture(self.base / ('install-' + reason), '1.0.0-rc2', initialized=True)
                stage = fixture(self.base / ('stage-' + reason), '1.0.0-rc3')
                value = self.coordinator(root); value.stage=stage
                value.descriptor = dict(self.descriptor, release='1.0.0-rc3', installer='docker.io/scharfesicht/csc-platform-installer@sha256:' + 'e'*64)
                old = {'phase':'staged', 'fromRelease':'1.0.0-rc1', 'targetRelease':'1.0.0-rc2',
                       'installer':self.descriptor['installer'], 'secretHashes':M.secret_fingerprints(root),
                       'wasStarted':reason=='started'}
                M.save_json(value.control / 'upgrade.json', old)
                if reason=='marker':
                    (root / '.deploy/running-release.json').write_text('{}')
                if reason=='changed':
                    (root / '.deploy/secrets/installation_id').write_text('changed')
                with self.assertRaisesRegex(M.SetupError, 'Recovery is refused'):
                    value.overlay(manifest('1.0.0-rc3'), started=False)
                self.assertEqual(M.read_json(root / 'release.json')['release'], '1.0.0-rc2')
                self.assertEqual(M.read_json(value.control / 'upgrade.json'), old)

    def test_prepared_initialization_commit_recovers_without_new_identity(self):
        root = fixture(self.base / 'install')
        value = self.coordinator(root)
        stage = fixture(self.base / '.install-stage-fixture', initialized=True)
        original = M.secret_fingerprints(stage)
        record = {'target': str(root), 'stage': str(stage), 'manifestHash': M.sha(stage / 'release.json')}
        M.save_json(value.control / 'prepared-install.json', record)
        value.finish_prepared(record)
        self.assertEqual(M.secret_fingerprints(root), original)
        self.assertFalse((value.control / 'prepared-install.json').exists())

    def test_already_started_current_release_is_noop(self):
        root = fixture(self.base / 'install', initialized=True, started=True)
        value = self.coordinator(root);value.fetch_bundle = mock.Mock()
        value.execute()
        value.fetch_bundle.assert_not_called()

    def test_no_destructive_volume_reset_commands(self):
        source = (ROOT / 'bootstrap/csc_setup.py').read_text()
        self.assertNotIn("'down', '-v'", source)
        self.assertNotIn("'volume', 'rm'", source)
        self.assertNotIn("'system', 'prune'", source)
        self.assertNotIn("'--insecure'", source)
        self.assertNotIn('shell=True', source)

if __name__ == '__main__': unittest.main()

class DedicatedHttpsPortTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = fixture(self.base / 'site', initialized=True, started=True)
        self.control = self.base / 'control'
        self.control.mkdir()
        self.profile = self.control / 'preferences.json'
        self.profile.write_text('{}')
        desc = {'product': 'CSCIntegrationPlatform', 'schemaVersion': 1, 'release': '1.0.0-rc3',
                'platform': 'linux/amd64', 'minimumBootstrap': '1.0.0-rc5',
                'installer': 'docker.io/scharfesicht/csc-platform-installer@sha256:' + 'd' * 64}
        opts = arguments(self.root)
        opts.https_port = 18443
        self.coord = M.Coordinator(opts, desc)
        self.coord.control = self.control
        self.coord.profile_file = self.profile
        self.coord.profile = {}
        (self.root / 'deploy/compose.yaml').write_text('gateway:\n  ports: ["${CSC_BIND_ADDRESS:-0.0.0.0}:443:8443"]\n')

    def tearDown(self):
        self.tmp.cleanup()

    def test_port_persists_and_is_repeatable(self):
        controller = types.SimpleNamespace(
            env_values=lambda: self._values(),
            write_env=lambda v: (self.root / 'deploy/.env').write_text(''.join(k+'='+json.dumps(str(x))+'\n' for k,x in v.items())))
        with mock.patch.object(M, 'module_at', return_value=controller):
            self.coord.ensure_https_port()
            first = (self.root / 'deploy/compose.yaml').read_bytes()
            self.coord.args.https_port = None
            self.assertEqual(self.coord.https_port(), 18443)
            self.coord.ensure_https_port()
            self.assertEqual(first, (self.root / 'deploy/compose.yaml').read_bytes())
            self.assertEqual(self._values()['CSC_HTTPS_PORT'], '18443')

    def _values(self):
        result = {}
        for line in (self.root / 'deploy/.env').read_text().splitlines():
            k, v = line.split('=', 1)
            result[k] = json.loads(v) if v.startswith('"') else v
        return result

    def test_unknown_compose_mapping_refused(self):
        (self.root / 'deploy/compose.yaml').write_text('gateway:\n  ports: ["127.0.0.1:443:8443"]\n')
        with self.assertRaisesRegex(M.SetupError, 'Unrecognized'):
            self.coord.ensure_https_port()

