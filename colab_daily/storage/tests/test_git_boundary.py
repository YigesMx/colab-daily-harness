"""Check actual Git ignore semantics without creating a repository or index."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest


class GitBoundaryTests(unittest.TestCase):
    def test_public_storage_code_and_private_runtime_boundary(self):
        root = Path(__file__).parents[3]
        git = shutil.which("git")
        # A source checkout normally has .git. During migration the root is not
        # initialized; borrow only the existing deployment repository metadata
        # for read-only check-ignore, with an explicit source-root work tree.
        metadata = next((p for p in (root / ".git", root / ".local/site/.git") if p.is_dir()), None)
        if git is None or metadata is None:
            self.skipTest("Git and existing repository metadata required; never initialize a test repository")
        private = [
            "state/storage/storage.sqlite3", "state/storage/storage.sqlite3-wal",
            "state/storage/storage.sqlite3-shm", "state/storage/storage.sqlite3-journal",
            "state/storage/assets/aa/synthetic-blob", "state/backups/example/manifest.json", "state/workspace.lock",
            "state/migration/export.json", "state/migration/attachments/example.png",
            ".local/migration/export.json", ".local/site/docs/public/example.png",
            "working_tmp/assembly.json", "example.db", "example.db-wal",
            "example.db-shm", "example.db-journal", "nested/example.sqlite3-journal",
            "nested/example.jsonl", ".env", ".repo_private_key",
        ]
        public = [
            "colab_daily/config.py", "colab_daily/lifecycle.py", "colab_daily/adapters.py",
            "colab_daily/inventory_adapter.py", "colab_daily/RUNTIME.md", "colab_daily/storage/store.py",
            "colab_daily/storage/schema.py", "colab_daily/storage/runtime.py", "colab_daily/storage/metadata.py",
            "colab_daily/storage/README.md", "colab_daily/storage/tests/test_store.py",
            "colab_daily/storage/tests/test_git_boundary.py", "colab_daily/storage/tests/test_metadata_boundary.py",
            ".env.example", ".gitignore",
        ]
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1")
        result = subprocess.run(
            [git, "--git-dir", str(metadata), "--work-tree", str(root),
             "-c", "core.excludesFile=/dev/null", "check-ignore", "--no-index", "-z", "--stdin"],
            cwd=root, env=env, input="\0".join(private + public) + "\0",
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, "read-only Git ignore check failed")
        ignored = set(result.stdout.rstrip("\0").split("\0"))
        self.assertEqual(ignored, set(private))
