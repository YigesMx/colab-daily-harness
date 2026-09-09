import importlib.util
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_grouped_candidates.py"
SPEC = importlib.util.spec_from_file_location("validate_grouped_candidates", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def candidate(category, number, schema_version=3):
    if schema_version == 2 and category != "Paper":
        return {**candidate(category, number), "score_scale": "news-policy-v2", "rating_track": "news_policy"}
    track = category.lower()
    scale = {"Paper": "paper-v2", "News": "news-v3", "Policy": "policy-v3"}[category]
    return {
        "candidate_id": f"{track}-{number}",
        "title": f"{category} {number}",
        "category": category,
        "group_rank": number,
        "group_score": number,
        "score_scale": scale,
        "rating_track": track,
    }


def artifact(papers=0, news=0, policies=0, schema_version=3):
    contract = "three-track-v3" if schema_version == 3 else "two-track-v2"
    return {
        "schema_version": schema_version,
        "quota_contract": contract,
        "selection_limit": 20 if schema_version == 3 else 15,
        "groups": {
            "Paper": {"candidates": [candidate("Paper", i, schema_version) for i in range(1, papers + 1)]},
            "News": {"candidates": [candidate("News", i, schema_version) for i in range(1, news + 1)]},
            "Policy": {"candidates": [candidate("Policy", i, schema_version) for i in range(1, policies + 1)]},
        },
    }


class ValidateThreeTrackCandidatesTest(unittest.TestCase):
    def test_v3_target_and_independent_track_scales(self):
        report = MODULE.validate_grouped_candidates(artifact(10, 7, 3))
        self.assertTrue(report["valid"])
        self.assertEqual(report["groups"]["Paper"]["emphasis_candidate_ids"], ["paper-1", "paper-2", "paper-3"])
        self.assertEqual(report["groups"]["News"]["emphasis_candidate_ids"], [])

    def test_policy_shortage_uses_news_within_numeric_quota(self):
        self.assertTrue(MODULE.validate_grouped_candidates(artifact(10, 8, 2))["valid"])

    def test_rejects_cross_track_scale(self):
        payload = artifact(news=1)
        payload["groups"]["News"]["candidates"][0]["score_scale"] = "policy-v3"
        report = MODULE.validate_grouped_candidates(payload)
        self.assertFalse(report["valid"])
        self.assertTrue(any("score_scale_mismatch" in item for item in report["violations"]))

    def test_empty_paper_is_valid(self):
        self.assertTrue(MODULE.validate_grouped_candidates(artifact(0, 7, 3))["valid"])

    def test_legacy_v2_contract_remains_valid(self):
        report = MODULE.validate_grouped_candidates(artifact(10, 3, 2, schema_version=2))
        self.assertTrue(report["valid"])
        self.assertEqual(report["quota_contract"], "two-track-v2")


if __name__ == "__main__":
    unittest.main()
