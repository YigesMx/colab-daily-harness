import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "coordinate_dual_track.py"
SPEC = importlib.util.spec_from_file_location("coordinate_dual_track_legacy", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class LegacyCoordinatorStubTest(unittest.TestCase):
    def test_obsolete_dual_track_entrypoint_fails_closed(self):
        with self.assertRaises(SystemExit):
            MODULE.main()


if __name__ == "__main__":
    unittest.main()
