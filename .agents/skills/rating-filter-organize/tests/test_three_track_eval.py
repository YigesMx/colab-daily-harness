import importlib.util
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = SKILL_ROOT / "scripts" / "coordinate_three_tracks.py"
SPEC = importlib.util.spec_from_file_location("coordinate_three_tracks", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class ProductionPartitionTest(unittest.TestCase):
    def setUp(self):
        temporary_parent = PROJECT_ROOT / "working_tmp"
        temporary_parent.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rating-filter-eval-", dir=temporary_parent
        )
        self.rating_root = Path(self.temporary.name)
        self.rating_root_patch = mock.patch.object(
            MODULE, "RATING_ROOT", self.rating_root
        )
        self.rating_root_patch.start()
        runs_root = self.rating_root / "runs"
        runs_root.mkdir(parents=True, exist_ok=True)
        self.run_dir = runs_root / "production-partition-run"
        self.run_dir.mkdir()
        self.run_id = self.run_dir.name
        self.evidence_dir = self.run_dir / "shared" / "canonical_records"
        self.cycle_id = "daily-2026-08-08"
        self.candidates = [
            self.candidate("paper-1", "Paper"),
            self.candidate("paper-2", "Paper"),
            self.candidate("news-1", "News"),
            self.candidate("policy-1", "Policy"),
        ]
        self.write_routing(self.candidates, selection_limit=20)

    def tearDown(self):
        self.rating_root_patch.stop()
        self.temporary.cleanup()

    def candidate(self, candidate_id, category):
        canonical = self.evidence_dir / candidate_id / "canonical_record.md"
        source = self.evidence_dir / candidate_id / "source_record.md"
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(f"# {candidate_id}\n", encoding="utf-8")
        source.write_text(f"source: {candidate_id}\n", encoding="utf-8")
        return {
            "candidate_id": candidate_id,
            "title": candidate_id,
            "category": category,
            "canonical_record_path": canonical.as_posix(),
            "source_record_paths": [source.as_posix()],
            "source_urls": [f"https://example.com/{candidate_id}"],
            "canonical_url": f"https://example.com/{candidate_id}",
            "normalized_arxiv_id": "2601.00001" if category == "Paper" else None,
            "source_identities": [f"https://example.com/{candidate_id}"],
        }

    def write_json(self, path, payload):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def write_shared_manifests(self, candidates):
        source_paths = [
            path
            for candidate in candidates
            for path in candidate["source_record_paths"]
        ]
        shared = self.run_dir / "shared"
        self.write_json(
            shared / "source_inventory.json",
            {
                "schema_version": 2,
                "run_id": self.run_id,
                "cycle_id": self.cycle_id,
                "discovered_sources": ["fixture_source"],
                "sources": [
                    {
                        "source_name": "fixture_source",
                        "status": "success" if source_paths else "success_empty",
                        "error": None,
                        "record_paths": source_paths,
                    }
                ],
                "records": [
                    {
                        "inventory_source_name": "fixture_source",
                        "path": path,
                        "url": candidate["canonical_url"],
                        "normalized_arxiv_id": candidate["normalized_arxiv_id"],
                    }
                    for candidate in candidates
                    for path in candidate["source_record_paths"]
                ],
            },
        )
        self.write_json(
            shared / "canonical_objects.json",
            {
                "schema_version": 2,
                "run_id": self.run_id,
                "cycle_id": self.cycle_id,
                "objects": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "category": candidate.get("category"),
                        "canonical_record_path": candidate["canonical_record_path"],
                        "source_record_paths": candidate["source_record_paths"],
                        "source_urls": candidate.get("source_urls", []),
                        "canonical_url": candidate["canonical_url"],
                        "normalized_arxiv_id": candidate["normalized_arxiv_id"],
                        "source_identities": candidate["source_identities"],
                    }
                    for candidate in candidates
                ],
            },
        )
        self.write_json(
            shared / "prior_cycle_identity.json",
            {
                "schema_version": 2,
                "run_id": self.run_id,
                "cycle_id": self.cycle_id,
                "snapshot": {
                    "first_cycle": True,
                    "previous_publish": None,
                    "records": [],
                    "before": None,
                    "after": None,
                },
                "decisions": [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "decision": "no_match",
                    }
                    for candidate in candidates
                ],
            },
        )

    def write_routing(self, candidates, selection_limit=20, **overrides):
        self.write_shared_manifests(candidates)
        payload = {
            "schema_version": 2,
            "production_input": True,
            "quality_neutral": True,
            "run_id": self.run_id,
            "cycle_id": self.cycle_id,
            "selection_limit": selection_limit,
            "eligible_candidates": candidates,
        }
        payload.update(overrides)
        path = self.run_dir / "shared" / "routing.json"
        self.write_json(path, payload)

    def track_candidates(self, track):
        input_path = self.run_dir / "tracks" / track / "input.json"
        if input_path.is_file():
            return json.loads(input_path.read_text(encoding="utf-8"))["candidates"]
        categories = MODULE.TRACK_CATEGORIES[track]
        return [item for item in self.candidates if item["category"] in categories]

    def score_components(self, track, score):
        remaining = float(score)
        components = {}
        for component, maximum in MODULE.SCORE_COMPONENTS[track].items():
            value = min(remaining, float(maximum))
            components[component] = value
            remaining -= value
        self.assertTrue(math.isclose(remaining, 0.0, abs_tol=1e-9))
        return components

    def write_output(
        self,
        track,
        ordered_ids,
        *,
        output_identity=None,
        decisions=None,
        scores=None,
        **overrides,
    ):
        input_path = self.run_dir / "tracks" / track / "input.json"
        track_input = json.loads(input_path.read_text(encoding="utf-8"))
        candidates = track_input["candidates"]
        evidence_paths = [item["canonical_record_path"] for item in candidates]
        if decisions is None:
            decisions = []
            for index, candidate in enumerate(candidates):
                score = (scores or {}).get(candidate["candidate_id"], 100 - index)
                qualified = candidate["candidate_id"] in ordered_ids
                decisions.append({
                    "candidate_id": candidate["candidate_id"],
                    "category": candidate["category"],
                    "admission_passed": True,
                    "qualified": qualified,
                    "group_score": score,
                    "score_components": self.score_components(track, score),
                    "confidence": "high",
                    "decision_reasons": ["quality evidence"],
                    "evidence_paths": [candidate["canonical_record_path"]],
                    "comparison_reasons": ["same-track comparison"],
                    "cutoff_reason": (
                        "selected within frozen capacity"
                        if qualified
                        else "not selected by track cutoff"
                    ),
                })
        payload = {
            "schema_version": 3,
            "quota_contract": "three-track-v3",
            "production_output": True,
            "isolated_context": True,
            "old_run_judgments_read": False,
            "cross_track_reads": False,
            "canonicalization_performed": False,
            "classification_performed": False,
            "run_id": self.run_id,
            "run_identity": track_input["run_identity"],
            "track": track,
            "context_id": f"{track}-context",
            "output_identity": output_identity or f"{track}-output-1",
            "production_input_path": input_path.as_posix(),
            "consensus_path": track_input["consensus_path"],
            "actual_paths_read": [
                input_path.as_posix(),
                *(
                    contract["path"]
                    for contract in track_input["contract_files"].values()
                ),
                *evidence_paths,
            ],
            "external_urls_read": [],
            "ordered_candidate_ids": ordered_ids,
            "decisions": decisions,
            "under_target_reason": (
                f"fixture has fewer than {MODULE.TRACK_TARGETS[track]} qualified candidates"
                if len(ordered_ids) < MODULE.TRACK_TARGETS[track]
                else None
            ),
        }
        payload.update(overrides)
        output_path = self.run_dir / "tracks" / track / "output.json"
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        return output_path

    def prepare_track(self, track, ordered_ids):
        stage = MODULE.prepare_track(self.run_dir, track)
        output = self.write_output(track, ordered_ids)
        return stage, output

    def test_three_independent_inputs_and_empty_paper_assembly(self):
        partition = MODULE.prepare_shared_partitions(self.run_dir)
        manifest = partition["partition_manifest"]
        self.assertEqual(set(manifest["partitions"]), {"paper", "news", "policy"})
        self.assertEqual(manifest["materialization_order"][:3], ["paper_input", "news_input", "policy_input"])

        stages = {}
        for track, ordered in {
            "paper": [],
            "news": ["news-1"],
            "policy": ["policy-1"],
        }.items():
            stage, _ = self.prepare_track(track, ordered)
            stages[track] = stage
            payload = json.loads(Path(stage["input_path"]).read_text(encoding="utf-8"))
            encoded = json.dumps(payload, sort_keys=True)
            self.assertEqual(payload["track"], track)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["quota_contract"], "three-track-v3")
            for sibling in set(MODULE.TRACK_CATEGORIES) - {track}:
                self.assertNotIn(sibling, encoded)
            self.assertNotIn("group_score", encoded)

        self.assertEqual(stages["paper"]["paper_capacity"], 10)
        self.assertEqual(stages["news"]["news_capacity"], 10)
        self.assertEqual(stages["policy"]["policy_capacity"], 3)

        result = MODULE.assemble_production(self.run_dir)
        grouped = json.loads(Path(result["grouped_selection_path"]).read_text(encoding="utf-8"))
        self.assertEqual(grouped["schema_version"], 3)
        self.assertEqual(grouped["quota_contract"], "three-track-v3")
        self.assertEqual([item["candidate_id"] for item in grouped["groups"]["Paper"]["candidates"]], [])
        self.assertEqual([item["candidate_id"] for item in grouped["groups"]["News"]["candidates"]], ["news-1"])
        self.assertEqual([item["candidate_id"] for item in grouped["groups"]["Policy"]["candidates"]], ["policy-1"])
        self.assertEqual(grouped["coordinator"]["news_final_capacity"], 9)
        self.assertEqual(result["grouped_validation"]["valid"], True)
        current = json.loads((self.rating_root / "current.json").read_text(encoding="utf-8"))
        self.assertEqual(current["schema_version"], 3)
        self.assertEqual(grouped["coordinator"]["under_target_reasons"]["paper"], "fixture has fewer than 10 qualified candidates")

    def test_below_target_requires_nonempty_under_target_reason(self):
        for track, ordered in {"paper": [], "news": ["news-1"], "policy": ["policy-1"]}.items():
            self.prepare_track(track, ordered)
        output_path = self.run_dir / "tracks" / "paper" / "output.json"
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        payload["under_target_reason"] = None
        output_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "below its target needs under_target_reason"):
            MODULE.assemble_production(self.run_dir)

    def test_policy_shortage_uses_news_order_without_cross_track_score(self):
        for track, ordered in {
            "paper": ["paper-1"],
            "news": ["news-1"],
            "policy": [],
        }.items():
            self.prepare_track(track, ordered)
        result = MODULE.assemble_production(self.run_dir)
        grouped = json.loads(Path(result["grouped_selection_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(grouped["groups"]["News"]["candidates"]), 1)
        self.assertEqual(grouped["coordinator"]["news_final_capacity"], 10)
        self.assertFalse(grouped["score_domains_comparable"])

    def test_policy_output_hard_max_is_three(self):
        for candidate_id in ("policy-2", "policy-3", "policy-4"):
            self.candidates.append(self.candidate(candidate_id, "Policy"))
        self.write_routing(self.candidates, selection_limit=20)
        for track, ordered in {
            "paper": ["paper-1"],
            "news": ["news-1"],
            "policy": ["policy-1", "policy-2", "policy-3", "policy-4"],
        }.items():
            self.prepare_track(track, ordered)
        with self.assertRaisesRegex(ValueError, "policy ordered output exceeds its frozen production capacity: 4>3"):
            MODULE.assemble_production(self.run_dir)

    def test_v3_rejects_smaller_selection_limit(self):
        self.write_routing(self.candidates, selection_limit=15)
        with self.assertRaisesRegex(ValueError, "selection_limit must be exactly 20"):
            MODULE.prepare_shared_partitions(self.run_dir)


if __name__ == "__main__":
    unittest.main()
