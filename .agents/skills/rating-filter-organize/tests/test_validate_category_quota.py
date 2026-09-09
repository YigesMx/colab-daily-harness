import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_category_quota.py"
SPEC = importlib.util.spec_from_file_location("validate_category_quota", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ThreeTrackQuotaTest(unittest.TestCase):
    def test_default_v3_target_is_valid(self):
        report = MODULE.validate_categories(["Paper"] * 10 + ["News"] * 7 + ["Policy"] * 3)
        self.assertTrue(report["valid"])
        self.assertEqual(report["quota_contract"], "three-track-v3")
        self.assertEqual(report["effective_paper_max"], 10)

    def test_policy_shortage_can_use_news_but_not_paper(self):
        report = MODULE.validate_categories(["Paper"] * 10 + ["News"] * 10 + ["Policy"] * 0)
        self.assertTrue(report["valid"])
        self.assertEqual(report["news_policy_count"], 10)

        self.assertTrue(MODULE.validate_categories(["Paper"] * 10 + ["News"] * 8 + ["Policy"] * 2)["valid"])

        report = MODULE.validate_categories(["Paper"] * 11 + ["News"] * 4 + ["Policy"] * 3)
        self.assertFalse(report["valid"])
        self.assertIn("paper_max_exceeded:11>10", report["violations"])

    def test_each_track_and_total_caps(self):
        self.assertFalse(MODULE.validate_categories(["Policy"] * 4)["valid"])
        self.assertFalse(MODULE.validate_categories(["News"] * 11)["valid"])
        self.assertFalse(MODULE.validate_categories(["Paper"] * 10 + ["News"] * 8 + ["Policy"] * 3)["valid"])

    def test_v3_selection_limit_is_frozen_at_20(self):
        report = MODULE.validate_categories([], selection_limit=15)
        self.assertFalse(report["valid"])
        self.assertIn("invalid_selection_limit:15", report["violations"])

    def test_legacy_v2_contract_still_supported(self):
        report = MODULE.validate_categories(
            ["Paper"] * 10 + ["News"] * 3 + ["Policy"] * 2,
            selection_limit=15,
            quota_contract="two-track-v2",
        )
        self.assertTrue(report["valid"])
        self.assertEqual(report["effective_paper_max"], 10)

    def test_cli_infers_contract(self):
        payload = {
            "schema_version": 3,
            "quota_contract": "three-track-v3",
            "selection_limit": 20,
            "groups": {
                "Paper": {"candidates": [{"category": "Paper"}] * 10},
                "News": {"candidates": [{"category": "News"}] * 7},
                "Policy": {"candidates": [{"category": "Policy"}] * 3},
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "grouped.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--input", str(path)],
                capture_output=True,
                check=False,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["valid"])


if __name__ == "__main__":
    unittest.main()
