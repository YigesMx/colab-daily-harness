import importlib.util
import io
from contextlib import redirect_stderr
from pathlib import Path
import tempfile
import unittest

from colab_daily.config import Config
from colab_daily.lifecycle import Lifecycle

SCRIPT = Path(__file__).parents[1] / "scripts/finalize_cleanup.py"
SPEC = importlib.util.spec_from_file_location("finalize_cleanup", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FinalizeCleanupTest(unittest.TestCase):
    def test_legacy_json_receipts_cannot_authorize_deletion(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = Path(temp) / "working_tmp/source"
            payload.mkdir(parents=True)
            (payload / "record.md").write_text("record")
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                MODULE.main(["--project-root", temp, "--journal", "old.json", "--delivery-receipt", "old.json", "--delete"])
            self.assertTrue(payload.exists())

    def test_foreign_owner_and_unreleased_cycle_cannot_delete_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            life = Lifecycle(Config.load(temp, {}))
            life.begin("manual", "owner", "2026-01-01")
            (life.working / "record.md").write_text("record")
            for owner in ("foreign", "owner"):
                with redirect_stderr(io.StringIO()):
                    code = MODULE.main(["--project-root", temp, "--cycle", "daily-2026-01-01", "--owner", owner])
                self.assertEqual(code, 2)
                self.assertTrue((life.working / "record.md").exists())


if __name__ == "__main__":
    unittest.main()
