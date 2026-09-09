"""SQLite-intent deployment; exact-SHA retry, scoped Git and public artifact readback."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
from urllib.parse import urlsplit

from dotenv import dotenv_values
import requests

from ..config import Config
from ..storage import Store, StorageError, digest
from ..storage.files import atomic_write, read_bytes, relative_name, safe_path, sha256
from .render import render
from .validation import public_url, require
from scripts.public_tree_scan import scan_file_map

TEMPLATE_FILES = (
    '.gitignore', 'package.json', 'package-lock.json', 'docs/index.md',
    'docs/.vitepress/config.mts', 'docs/.vitepress/data/daily.data.ts',
    'docs/.vitepress/theme/index.ts', 'docs/.vitepress/theme/custom.css',
    'docs/.vitepress/theme/components/ArticleMetadata.vue', 'docs/.vitepress/theme/components/DailyIndexPage.vue',
    'scripts/validate-built-site.mjs', 'scripts/artifact-manifest.mjs', '.github/workflows/deploy-pages.yml',
)


class Settings:
    def __init__(self, config, environ=None):
        self.config = config
        self.environ = dict(os.environ if environ is None else environ)
        env = {**dotenv_values(config.project_root / '.env'), **self.environ}
        self.remote = env.get('COLAB_SITE_REMOTE', 'origin')
        self.branch = env.get('COLAB_SITE_BRANCH', 'main')
        self.base = env.get('COLAB_SITE_BASE', '/')
        self.url = env.get('COLAB_SITE_URL', '')
        key = Path(env.get('COLAB_DAILY_DEPLOY_KEY') or '.repo_private_key')
        self.key = safe_path(key if key.is_absolute() else config.project_root / key)
        require(re.fullmatch(r'[A-Za-z0-9_-]+', self.remote) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_/-]*', self.branch) and
                '..' not in self.branch and '//' not in self.branch and not self.branch.endswith('/'), 'invalid deployment ref')
        require(re.fullmatch(r'/(?:[A-Za-z0-9._~-]+/)*', self.base) and all(x not in ('.', '..') for x in self.base.split('/')), 'invalid site base')
        public_url(self.url)
        parsed = urlsplit(self.url)
        require(parsed.scheme == 'https' and parsed.path == self.base and not parsed.query and not parsed.fragment, 'public site URL must be HTTPS with exact configured base')
        require(self.key.is_file() and self.key.stat().st_mode & 0o777 == 0o600, 'dedicated deployment key must be a regular 0600 file')
        require(b'PRIVATE KEY' in read_bytes(self.key), 'configured deployment key is not a private key')
        site = safe_path(config.site_dir)
        require(site != config.project_root and site not in (config.working_dir, config.storage_dir) and
                config.working_dir not in site.parents and config.storage_dir not in site.parents and site not in self.key.parents,
                'deployment checkout/key/state layout overlaps')

    def environment(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') and k not in {'SSH_AUTH_SOCK', 'SSH_AGENT_PID', 'GH_TOKEN', 'GITHUB_TOKEN'}}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull,
                   VITEPRESS_BASE=self.base,
                   GIT_SSH_COMMAND='ssh -F /dev/null -i ' + shlex.quote(str(self.key)) +
                   ' -o IdentitiesOnly=yes -o IdentityAgent=none -o BatchMode=yes -o StrictHostKeyChecking=yes')
        return env

    def fingerprint(self):
        return digest({'remote': self.remote, 'branch': self.branch, 'base': self.base, 'url': self.url,
                       'key_sha256': sha256(read_bytes(self.key)), 'remote_repository_sha256': getattr(self, 'remote_repository_sha256', None)})


class Runner:
    def __init__(self, settings):
        self.settings = settings

    def run(self, args, *, binary=False):
        try:
            result = subprocess.run(args, cwd=self.settings.config.site_dir, env=self.settings.environment(),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=600)
        except (OSError, subprocess.SubprocessError):
            # Never echo remote URLs, stderr, identity or command arguments.
            raise StorageError('site subprocess failed; effects may be unknown, retry the same deployment') from None
        return result.stdout if binary else result.stdout.decode().strip()

    def git(self, *args, binary=False):
        return self.run(['git', '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false',
                         '-c', 'core.sshCommand=' + self.settings.environment()['GIT_SSH_COMMAND'], *args], binary=binary)

    def fetch(self, path):
        relative_name(path)
        require(re.fullmatch(r'[A-Za-z0-9._~/-]+', path), 'unsafe artifact request path')
        session = requests.Session()
        session.trust_env = False  # no personal proxies/netrc authentication
        try:
            response = session.get(self.settings.url + path, timeout=30, allow_redirects=False, stream=True,
                                   headers={'Cache-Control': 'no-cache'})
            require(response.status_code == 200, 'public artifact is not available at its exact URL')
            data = bytearray()
            for chunk in response.iter_content(65536):
                data.extend(chunk)
                require(len(data) <= 64 * 1024 * 1024, 'public artifact response oversized')
            return bytes(data)
        except requests.RequestException:
            raise StorageError('public artifact verification unavailable; deployment remains unknown') from None
        finally:
            session.close()


@contextmanager
def site_lock(config):
    path = safe_path(config.project_root / 'state/site.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def template(config):
    root = safe_path(config.project_root / 'site_template')
    return {name: read_bytes(root / name) for name in TEMPLATE_FILES}


def status_paths(runner, owned_index=None):
    staged = runner.git('diff', '--cached', '--name-only', '-z', binary=True)
    staged_paths = {p.decode() for p in staged.split(b'\0') if p}
    require(not staged_paths or owned_index is not None and staged_paths <= set(owned_index), 'deployment refuses an unowned staged index')
    for name in staged_paths:
        require(sha256(runner.git('show', ':' + name, binary=True)) == owned_index[name]['after'], 'staged bytes differ from SQLite intent')
    changed = runner.git('diff', '--name-only', '-z', binary=True)
    untracked = runner.git('ls-files', '--others', '--exclude-standard', '-z', binary=True)
    return staged_paths | {relative_name(p.decode()) for p in (changed + untracked).split(b'\0') if p}


def file_hash(site, name):
    relative_name(name)
    path = safe_path(site / name)
    require(not path.exists() or path.is_file(), 'managed target is not a regular file')
    return sha256(read_bytes(path)) if path.exists() else None


def preflight(config, runner=None):
    """No network, no database initialization, no Git mutation."""
    settings = Settings(config)
    runner = runner or Runner(settings)
    require((safe_path(config.site_dir) / '.git').is_dir(), 'site must be a separate existing Git checkout')
    require(runner.git('rev-parse', '--show-toplevel') == str(config.site_dir.absolute()), 'site Git root mismatch')
    remote_url = runner.git('remote', 'get-url', settings.remote)
    require(re.fullmatch(r'git@[A-Za-z0-9.-]+:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', remote_url) or
            re.fullmatch(r'ssh://git@[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?', remote_url),
            'site remote must use dedicated-key SSH, without credentials/options')
    settings.remote_repository_sha256 = sha256(remote_url.encode())
    # --get-regexp has exit 1 for absence; enumerate names with an always-successful command instead.
    local_config = runner.git('config', '--local', '--list').lower().splitlines()
    require(not any(line.startswith('url.') or '.pushurl=' in line or line.startswith('core.sshcommand=') for line in local_config),
            'local Git URL/SSH overrides are forbidden')
    template(config)
    return settings, runner


def plan_files(config, runner, files, day=None):
    changed = status_paths(runner)
    require(changed <= set(TEMPLATE_FILES), 'checkout has unowned modifications')
    for name in changed:
        require(file_hash(config.site_dir, name) == sha256(files[name]), 'template has unowned changes')
    if day:
        for relative in (f'docs/daily/{day}', f'docs/public/daily/{day}'):
            require(not safe_path(config.site_dir / relative).exists(), 'managed date collision: existing history/unowned material is preserved')
    return {name: {'before': file_hash(config.site_dir, name), 'after': sha256(data)} for name, data in files.items()}


def apply_files(config, files, plan):
    require(set(files) == set(plan), 'managed write set changed')
    # Validate the entire set before writing anything; retry accepts only old or intended bytes.
    for name, data in files.items():
        require(sha256(data) == plan[name]['after'] and file_hash(config.site_dir, name) in {plan[name]['before'], plan[name]['after']},
                'managed file collision: preserve unowned changes')
    for name, data in files.items():
        if file_hash(config.site_dir, name) != plan[name]['after']:
            atomic_write(config.site_dir / name, data)


def sync_template(config):
    """Offline maintenance operation, fixed framework allowlist only; no date writes or deliveries."""
    with site_lock(config):
        _, runner = preflight(config)
        files = template(config)
        plan = plan_files(config, runner, files)
        apply_files(config, files, plan)
    return len(files)


def artifact_files(config):
    root = safe_path(config.site_dir / 'docs/.vitepress/dist')
    require(root.is_dir(), 'build output is missing')
    files = {}
    for path in sorted(root.rglob('*')):
        safe_path(path)
        if path.is_dir(): continue
        require(path.is_file(), 'nonregular build artifact')
        name = path.relative_to(root).as_posix()
        relative_name(name)
        require(re.fullmatch(r'[A-Za-z0-9._~/-]+', name), 'unsafe build artifact path')
        if name == 'release-artifact.json': continue
        data = read_bytes(path)
        files[name] = {'sha256': sha256(data), 'size': len(data)}
    require('index.html' in files and len(files) <= 20000 and sum(f['size'] for f in files.values()) <= 2_000_000_000,
            'invalid/oversized build artifact closure')
    return files


def remote_sha(runner, settings):
    result = runner.git('ls-remote', '--refs', settings.remote, 'refs/heads/' + settings.branch)
    require(re.fullmatch(r'[a-f0-9]{40,64}\s+refs/heads/' + re.escape(settings.branch), result), 'remote ref readback is absent/ambiguous')
    return result.split()[0]


def verify_commit(config, runner, plan, commit):
    require(re.fullmatch(r'[a-f0-9]{40}|[a-f0-9]{64}', commit), 'invalid site commit identity')
    require(runner.git('rev-parse', commit + '^') == plan['parent_sha'] and
            runner.git('log', '-1', '--format=%B', commit) == plan['message'], 'unknown local commit does not match intended publication')
    changed = {p.decode() for p in runner.git('diff', '--name-only', '-z', plan['parent_sha'], commit, binary=True).split(b'\0') if p}
    require(changed <= set(plan['files']), 'site commit includes unowned paths')
    for name, receipt in plan['files'].items():
        require(sha256(runner.git('show', commit + ':' + name, binary=True)) == receipt['after'], 'committed managed bytes differ from intent')
    return runner.git('rev-parse', commit + '^{tree}')


def deploy(config, cycle_id):
    """Run only in production. Maintenance tests mock Runner Git/build/network effects."""
    with site_lock(config):
        settings, runner = preflight(config)
        store = Store(config.storage_dir)
        frozen = store.export_publication(cycle_id)
        histories = store.delivery_history(cycle_id, 'deployment')
        if histories and histories[-1]['state'] == 'confirmed':
            return store.release(cycle_id)
        files = {**template(config), **render(store, cycle_id)}
        require(not scan_file_map(config.project_root, files, settings.environ), 'managed public files failed private-data scan')
        attempt = 'formal-site-v1'
        if histories:
            last = histories[-1]
            if last['state'] == 'failed':
                # Storage contract: a terminal-failed attempt with verified
                # side_effect_absent permits exactly one new attempt id.
                attempt = 'formal-site-v2' if last['attempt_id'] == 'formal-site-v1' else last['attempt_id'] + '-r'
                histories = []
            else:
                active = [h for h in histories if h['state'] in {'pending', 'unknown'}]
                require(len(active) == 1, 'deployment history requires controlled reconciliation')
                attempt = active[0]['attempt_id']
                plan = active[0]['request']
                require(plan['configuration_sha256'] == settings.fingerprint(), 'deployment config changed during retry')
                require({k: sha256(v) for k, v in files.items()} == {k: v['after'] for k, v in plan['files'].items()}, 'retry template/final inputs changed')
        if not histories:
            parent = runner.git('rev-parse', 'HEAD')
            plan = {'frozen_sha256': frozen['frozen_sha256'], 'configuration_sha256': settings.fingerprint(),
                    'parent_sha': parent, 'message': 'Publish formal daily ' + frozen['frozen_sha256'],
                    'files': plan_files(config, runner, files, frozen['publication']['display_date'])}
            # Durable write intent BEFORE filesystem writes, build, staging, commit or push.
            store.start_delivery(cycle_id, 'deployment', attempt, plan)
        head = runner.git('rev-parse', 'HEAD')
        prior_evidence = histories[0]['evidence'] if histories else None
        commit = prior_evidence.get('commit_sha') if prior_evidence else None
        try:
            if commit:
                require(head == commit, 'local HEAD moved after the intended site commit')
            if head == plan['parent_sha']:
                require(status_paths(runner, plan['files']) <= set(plan['files']), 'checkout has unowned edits during retry')
                apply_files(config, files, plan['files'])
                runner.run(['npm', 'run', 'build'])  # includes real loopback HTTP/content/asset validation
                before_build = digest(artifact_files(config))
                # Only exact intended paths enter an initially clean index.
                runner.git('add', '--', *sorted(plan['files']))
                staged = {p.decode() for p in runner.git('diff', '--cached', '--name-only', '-z', binary=True).split(b'\0') if p}
                require(staged and staged <= set(plan['files']), 'staging scope differs from intent')
                runner.git('-c', 'user.name=Colab Daily Bot', '-c', 'user.email=colab-daily-bot@users.noreply.github.com',
                           'commit', '-m', plan['message'])
                head = runner.git('rev-parse', 'HEAD')
            else:
                before_build = prior_evidence.get('artifact_sha256') if prior_evidence else None
            tree = verify_commit(config, runner, plan, head)
            require(not status_paths(runner), 'committed checkout must be clean before build/deployment')
            runner.run(['npm', 'run', 'build'])
            artifacts = artifact_files(config)
            artifact_hash = digest(artifacts)
            require(before_build is None or before_build == artifact_hash, 'same-source build is not reproducible')
            evidence = {'frozen_sha256': frozen['frozen_sha256'], 'reason': 'local build and committed source verified; remote/public effects pending',
                        'commit_sha': head, 'source_tree_sha': tree, 'artifact_sha256': artifact_hash, 'local_verified': True}
            store.record_delivery(cycle_id, 'deployment', attempt, 'unknown', evidence)
            remote = remote_sha(runner, settings)
            if remote != head:
                require(remote == plan['parent_sha'], 'remote advanced unexpectedly; never force or create a second commit')
                # A failed/unknown push retries precisely the same SHA after remote readback.
                runner.git('push', settings.remote, head + ':refs/heads/' + settings.branch)
            require(remote_sha(runner, settings) == head, 'exact remote commit has not been verified')
            public_manifest = json.loads(runner.fetch('release-artifact.json'))
            # The vitepress local-search index is not byte-reproducible across build
            # machines (concurrent indexing order), and the cumulative site grows with
            # every daily release, so public verification is bounded and commit-bound:
            # the publisher manifest must bind the exact intended commit and source tree,
            # and byte verification covers this release's changed artifacts, the site
            # entry page and a bounded sample; unchanged history is covered by the tree.
            require(public_manifest.get('schema_version') == 2
                    and public_manifest.get('display_date') == frozen['publication']['display_date']
                    and public_manifest.get('commit_sha') == head
                    and public_manifest.get('source_tree_sha') == tree
                    and isinstance(public_manifest.get('files'), dict) and public_manifest['files'],
                    'public commit-bound artifact does not equal locally built committed source')
            public_receipts = public_manifest['files']
            changed = {p.decode() for p in runner.git('diff', '--name-only', '-z', plan['parent_sha'], head, binary=True).split(b'\0') if p}
            bounded = set()
            for name in changed:
                if name.startswith('docs/public/'):
                    bounded.add(name[len('docs/public/'):])
                elif name.startswith('docs/daily/') and name.endswith('.md'):
                    bounded.add(name[len('docs/'):][:-len('.md')] + '.html')
                elif name == 'docs/index.md':
                    bounded.add('index.html')
            bounded.add('index.html')
            bounded &= set(public_receipts)
            sample_pool = sorted(set(public_receipts) - bounded)
            sample = set(sample_pool[:16]) if len(sample_pool) > 16 else set(sample_pool)
            verify_names = bounded | sample
            require(bounded or not changed, 'release changed no verifiable public artifacts')
            for name in sorted(verify_names):
                receipt = public_receipts[name]
                require(re.fullmatch(r'[A-Za-z0-9._~/-]+', name), 'unsafe public artifact path in manifest')
                data = runner.fetch(name)
                require(len(data) == receipt['size'] and sha256(data) == receipt['sha256'], 'public artifact bytes differ from exact committed build')
            require(remote_sha(runner, settings) == head, 'remote moved during public verification')
            store.record_delivery(cycle_id, 'deployment', attempt, 'confirmed',
                                  {**evidence, 'artifact_sha256': public_manifest['artifact_sha256'],
                                   'reason': 'exact local build, remote commit, commit-bound public manifest, and every changed/sampled public artifact byte-verified',
                                   'push_verified': True, 'public_verified': True})
            return store.release(cycle_id)
        except Exception:
            # Never call failed on a timeout and never invent absence. Existing evidence preserves known SHA.
            latest = store.delivery_history(cycle_id, 'deployment')[0]
            if latest['state'] not in {'confirmed', 'failed'}:
                store.record_delivery(cycle_id, 'deployment', attempt, 'unknown', latest['evidence'] or
                                      {'frozen_sha256': frozen['frozen_sha256'], 'reason': 'interrupted deterministic deployment; inspect same intent on retry'})
            raise
