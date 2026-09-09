"""Offline publication delivery tests: all remote, push, HTTP and build effects are mocked."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from colab_daily.config import Config
from colab_daily.publication.deployment import (
    TEMPLATE_FILES, Runner, Settings, artifact_files, deploy, file_hash, status_paths, template,
)
from colab_daily.storage import Store, StorageError, VALIDATION_CHECKS, digest

PROJECT = Path(__file__).resolve().parents[3]


class LocalDeliveryRunner(Runner):
    """Use only a temporary local Git repo; emulate build, remote ref and public readback."""
    def __init__(self, settings, store, cycle_id, fail_first_fetch=False):
        super().__init__(settings)
        self.store = store
        self.cycle_id = cycle_id
        self.remote_commit = self.git('rev-parse', 'HEAD')
        self.fail_first_fetch = fail_first_fetch
        self.fetch_failed = False
        self.pushes = 0

    def run(self, args, *, binary=False):
        if args[:3] == ['npm', 'run', 'build']:
            dist = self.settings.config.site_dir / 'docs/.vitepress/dist'
            shutil.rmtree(dist, ignore_errors=True)
            dist.mkdir(parents=True)
            (dist / 'index.html').write_bytes(b'<!doctype html><title>synthetic delivery</title>')
            return b'' if binary else ''
        return super().run(args, binary=binary)

    def git(self, *args, binary=False):
        if args and args[0] == 'push':
            self.pushes += 1
            self.remote_commit = self.git('rev-parse', 'HEAD')
            return b'' if binary else ''
        return super().git(*args, binary=binary)

    def fetch(self, path):
        if self.fail_first_fetch and not self.fetch_failed:
            self.fetch_failed = True
            raise StorageError('mock public readback unavailable')
        files = artifact_files(self.settings.config)
        if path == 'release-artifact.json':
            frozen = self.store.export_publication(self.cycle_id)
            commit = self.git('rev-parse', 'HEAD')
            payload = {
                'schema_version': 2,
                'display_date': frozen['publication']['display_date'],
                'commit_sha': commit,
                'source_tree_sha': self.git('rev-parse', commit + '^{tree}'),
                'artifact_sha256': digest(files),
                'files': files,
            }
            return json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
        return (self.settings.config.site_dir / 'docs/.vitepress/dist' / path).read_bytes()


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(PROJECT / 'site_template', self.root / 'site_template')
        shutil.copytree(PROJECT / 'site_template', self.root / 'site')
        key = self.root / 'deploy_key'
        key.write_text('-----BEGIN ' + 'PRIVATE KEY-----\nsynthetic test only\n-----END PRIVATE KEY-----\n')
        key.chmod(0o600)
        env = {
            'COLAB_SITE_DIR': 'site',
            'COLAB_STORAGE_DIR': 'state/storage',
            'COLAB_WORKING_DIR': 'working_tmp',
            'COLAB_DAILY_DEPLOY_KEY': 'deploy_key',
            'COLAB_SITE_REMOTE': 'origin',
            'COLAB_SITE_BRANCH': 'main',
            'COLAB_SITE_BASE': '/daily/',
            'COLAB_SITE_URL': 'https://example.org/daily/',
        }
        self.config = Config.load(self.root, env)
        self.settings = Settings(self.config, env)
        subprocess.run(['git', 'init', '-q', '-b', 'main'], cwd=self.config.site_dir, check=True)
        subprocess.run(['git', 'add', '.'], cwd=self.config.site_dir, check=True)
        subprocess.run(['git', '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                        'commit', '-q', '-m', 'synthetic base'], cwd=self.config.site_dir, check=True)
        self.settings.remote_repository_sha256 = 'a' * 64
        self.store = Store(self.config.storage_dir)
        self.cycle = 'daily-2026-01-02'
        self.store.begin_cycle(self.cycle, {'display_date': '2026-01-02'})
        publication = {
            'schema_version': 3,
            'cycle_id': self.cycle,
            'display_date': '2026-01-02',
            'records': [{'candidate_id': 'synthetic-1', 'title': 'Synthetic', 'category': 'Paper',
                         'source_identities': [], 'canonical_url': 'https://example.org/source'}],
        }
        self.frozen = self.store.freeze_publication(
            self.cycle, publication, {},
            {'publication_sha256': digest(publication), 'checks': dict.fromkeys(VALIDATION_CHECKS, True)},
        )
        self.rendered = {
            'docs/daily/2026-01-02/paper/01-synthetic.md': b'---\ntitle: "Synthetic"\n---\n\nFinal.\n',
            'docs/daily/2026-01-02/.managed-manifest.json': b'{}\n',
            'docs/public/release-input.json': json.dumps({
                'schema_version': 2, 'display_date': '2026-01-02'
            }).encode(),
        }

    def tearDown(self):
        self.temp.cleanup()

    def preflight(self, runner):
        return self.settings, runner

    def test_unknown_public_readback_retries_same_commit_then_releases(self):
        runner = LocalDeliveryRunner(self.settings, self.store, self.cycle, fail_first_fetch=True)
        with patch('colab_daily.publication.deployment.preflight', side_effect=lambda config: self.preflight(runner)), \
             patch('colab_daily.publication.deployment.render', return_value=self.rendered), \
             patch('colab_daily.publication.deployment.remote_sha', side_effect=lambda r, s: runner.remote_commit):
            with self.assertRaisesRegex(StorageError, 'mock public'):
                deploy(self.config, self.cycle)
            intended_commit = runner.git('rev-parse', 'HEAD')
            self.assertEqual(self.store.status(self.cycle)['phase'], 'PHASE_release')
            history = self.store.delivery_history(self.cycle, 'deployment')
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]['state'], 'unknown')
            self.assertEqual(history[0]['evidence']['commit_sha'], intended_commit)
            self.assertEqual(deploy(self.config, self.cycle), 'Released')
            self.assertEqual(runner.git('rev-parse', 'HEAD'), intended_commit)
            self.assertEqual(runner.git('rev-list', '--count', 'HEAD'), '2')
            self.assertEqual(runner.pushes, 1)
            self.assertEqual(self.store.delivery_history(self.cycle, 'deployment')[0]['state'], 'confirmed')

    def test_managed_render_private_fingerprint_fails_before_intent_or_git_write(self):
        (self.root / '.env').write_text('PRIVATE_DOMAIN=private.example.invalid\n')
        runner = LocalDeliveryRunner(self.settings, self.store, self.cycle)
        malicious = dict(self.rendered)
        malicious['docs/daily/2026-01-02/paper/01-synthetic.md'] = b'private.example.invalid'
        before = runner.git('rev-parse', 'HEAD')
        with patch('colab_daily.publication.deployment.preflight', side_effect=lambda config: self.preflight(runner)), \
             patch('colab_daily.publication.deployment.render', return_value=malicious):
            with self.assertRaisesRegex(StorageError, 'private-data scan'):
                deploy(self.config, self.cycle)
        self.assertEqual(self.store.delivery_history(self.cycle, 'deployment'), [])
        self.assertEqual(runner.git('rev-parse', 'HEAD'), before)

    def test_managed_render_environment_only_fingerprint_fails_before_intent_or_git_write(self):
        self.settings.environ['PRIVATE_DOMAIN'] = 'environment-private.example.invalid'
        runner = LocalDeliveryRunner(self.settings, self.store, self.cycle)
        malicious = dict(self.rendered)
        malicious['docs/daily/2026-01-02/paper/01-synthetic.md'] = b'environment-private.example.invalid'
        before = runner.git('rev-parse', 'HEAD')
        with patch('colab_daily.publication.deployment.preflight', side_effect=lambda config: self.preflight(runner)), \
             patch('colab_daily.publication.deployment.render', return_value=malicious):
            with self.assertRaisesRegex(StorageError, 'private-data scan'):
                deploy(self.config, self.cycle)
        self.assertEqual(self.store.delivery_history(self.cycle, 'deployment'), [])
        self.assertEqual(runner.git('rev-parse', 'HEAD'), before)
        self.assertEqual(status_paths(runner), set())

    def test_managed_render_environment_override_fails_before_intent_or_git_write(self):
        (self.root / '.env').write_text('PRIVATE_DOMAIN=dotenv-private.example.invalid\n')
        self.settings.environ['PRIVATE_DOMAIN'] = 'environment-private.example.invalid'
        runner = LocalDeliveryRunner(self.settings, self.store, self.cycle)
        malicious = dict(self.rendered)
        malicious['docs/daily/2026-01-02/paper/01-synthetic.md'] = b'environment-private.example.invalid'
        before = runner.git('rev-parse', 'HEAD')
        with patch('colab_daily.publication.deployment.preflight', side_effect=lambda config: self.preflight(runner)), \
             patch('colab_daily.publication.deployment.render', return_value=malicious):
            with self.assertRaisesRegex(StorageError, 'private-data scan'):
                deploy(self.config, self.cycle)
        self.assertEqual(self.store.delivery_history(self.cycle, 'deployment'), [])
        self.assertEqual(runner.git('rev-parse', 'HEAD'), before)
        self.assertEqual(status_paths(runner), set())

    def test_dedicated_key_environment_and_scoped_index_rules(self):
        environment = self.settings.environment()
        self.assertNotIn('SSH_AUTH_SOCK', environment)
        self.assertIn('IdentityAgent=none', environment['GIT_SSH_COMMAND'])
        self.assertIn('IdentitiesOnly=yes', environment['GIT_SSH_COMMAND'])
        runner = LocalDeliveryRunner(self.settings, self.store, self.cycle)
        private = self.config.site_dir / 'private.txt'
        private.write_text('must not be staged')
        runner.git('add', '--', 'private.txt')
        with self.assertRaisesRegex(StorageError, 'unowned staged'):
            status_paths(runner)

    def test_managed_target_symlink_is_rejected(self):
        target = self.config.site_dir / 'docs/index.md'
        target.unlink()
        target.symlink_to(self.config.site_dir / 'package.json')
        with self.assertRaisesRegex(StorageError, 'symlink|regular file'):
            file_hash(self.config.site_dir, 'docs/index.md')

    def test_template_allowlist_is_exact(self):
        self.assertEqual(set(template(self.config)), set(TEMPLATE_FILES))
        self.assertFalse(any(name.startswith('docs/daily/') for name in TEMPLATE_FILES))


if __name__ == '__main__':
    unittest.main()
