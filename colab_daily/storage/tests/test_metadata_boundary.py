"""The control plane never mirrors temporary documents or binary downloads."""
from argparse import Namespace
import base64
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colab_daily.adapters import source_command, rating_command
from colab_daily.config import Config
from colab_daily.inventory_adapter import inventory_command
from colab_daily.lifecycle import Lifecycle
from colab_daily.storage import StorageError
from colab_daily.storage.metadata import control_metadata

SENTINEL = "SYNTHETIC_INTERMEDIATE_BODY_NOT_FOR_SQLITE_7f93"


class MetadataBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.life = Lifecycle(Config.load(self.root, {}))
        self.life.begin("manual", "owner", "2026-01-02", context={"session_id": "synthetic-session"})

    def tearDown(self):
        self.temp.cleanup()

    def assert_not_persisted(self, binary=None):
        with self.life.store.connection() as conn:
            text = "\n".join(conn.iterdump())
        self.assertNotIn(SENTINEL, text)
        self.assertNotIn(base64.b64encode((SENTINEL * 3).encode()).decode()[:80], text)
        if binary:
            self.assertNotIn(base64.b64encode(binary).decode()[:80], text)
            self.assertNotIn(binary[:128], self.life.store.db.read_bytes())
        self.assertNotIn('"base64"', text)
        self.assertLess(len(text), 30000)

    def test_source_large_intermediates_stay_in_working_tmp(self):
        args = Namespace(project_root=self.root, cycle_id="daily-2026-01-02", owner="owner",
                         since=None, until=None, output_dir=None)
        binary = b"\x00\xfeSYNTHETIC_BINARY_DOWNLOAD_91\x80" * 65536
        def execute(args):
            output = args.output_dir / "synthetic"
            output.mkdir()
            (output / "record.md").write_text(SENTINEL * 10000)
            (output / "large.bin").write_bytes(binary)
            return 0
        self.assertEqual(source_command(args, execute, "synthetic"), 0)
        self.assertEqual(source_command(args, lambda _: self.fail("cached success must not refetch"), "synthetic"), 0)
        self.assertTrue((self.life.working / "synthetic/large.bin").exists())
        self.assert_not_persisted(binary)
        payload = self.life.store.run_state("daily-2026-01-02:source:synthetic")["stages"]["crawl"]["payload"]
        self.assertEqual(payload["counts"]["files"], 2)
        self.assertLess(len(json.dumps(payload)), 1000)

    def test_inventory_and_rating_documents_not_in_checkpoints(self):
        receipt = self.life.working / "source-discovery.json"
        output = self.life.working / "inventory.json"
        receipt.write_text(json.dumps({"sources": [], "detail": SENTINEL * 1000}))
        args = Namespace(owner="owner", cycle_id="daily-2026-01-02", run_id="rating-run",
                         crawl_root=self.life.working, source_receipt=receipt, output=output)
        def inventory(args):
            args.output.write_text(json.dumps({"records": [{"intermediate": SENTINEL * 1000}]}))
            return 0
        with patch("colab_daily.inventory_adapter.Config.load", return_value=self.life.config):
            inventory_command(args, inventory)
        run = self.life.working / "rating_filter_organize/runs/rating-run"
        (run / "shared").mkdir(parents=True)
        (run / "shared/routing.json").write_text('{"cycle_id":"daily-2026-01-02"}')
        rating_args = Namespace(owner="owner", run_dir=run, command="prepare-track", track="paper")
        def rate():
            path = run / "tracks/paper/input.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"candidates": [SENTINEL * 1000]}))
            return {"status": "prepared", "intermediate_result": SENTINEL * 1000}
        with patch("colab_daily.adapters.Config.load", return_value=self.life.config):
            result = rating_command(rating_args, rate)
            self.assertIn(SENTINEL, result["intermediate_result"])
            self.assertEqual(rating_command(rating_args, lambda: self.fail("must use local result")), result)
        self.assert_not_persisted()

    def test_actual_three_track_coordinator_accepts_metadata_adapter(self):
        project = Path(__file__).resolve().parents[3]
        path = project / ".agents/skills/rating-filter-organize/tests/test_three_track_eval.py"
        spec = importlib.util.spec_from_file_location("runtime_three_track_fixture", path)
        fixture_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fixture_module)
        module = fixture_module.MODULE
        fixture = fixture_module.ProductionPartitionTest()
        fixture.rating_root = self.life.working / "rating_filter_organize"
        fixture.run_dir = fixture.rating_root / "runs/rating-run"
        fixture.run_id = "rating-run"
        fixture.cycle_id = "daily-2026-01-02"
        fixture.evidence_dir = fixture.run_dir / "shared/canonical_records"
        fixture.candidates = [fixture.candidate(track + "-1", category)
                              for track, category in (("paper", "Paper"), ("news", "News"), ("policy", "Policy"))]
        fixture.write_routing(fixture.candidates)
        contracts = self.root / "contracts"
        contracts.mkdir()
        def copy_contract(source, name):
            destination = contracts / name
            destination.write_bytes(source.read_bytes())
            return destination
        consensus = {track: copy_contract(source, track + "-consensus.md")
                     for track, source in module.TRACK_CONSENSUS_PATHS.items()}
        prompts = {track: copy_contract(source, track + "-prompt.md")
                   for track, source in module.TRACK_PROMPT_PATHS.items()}
        skill = copy_contract(module.RATING_SKILL_PATH, "rating-skill.md")
        with ExitStack() as stack:
            stack.enter_context(patch("colab_daily.adapters.Config.load", return_value=self.life.config))
            for key, value in {"PROJECT_ROOT": self.root, "RATING_ROOT": fixture.rating_root,
                               "TRACK_CONSENSUS_PATHS": consensus, "TRACK_PROMPT_PATHS": prompts,
                               "RATING_SKILL_PATH": skill}.items():
                stack.enter_context(patch.object(module, key, value))
            for track in ("paper", "news", "policy"):
                args = Namespace(owner="owner", run_dir=fixture.run_dir, command="prepare-track", track=track)
                if track == "paper":
                    original = self.life.store.checkpoint_stage
                    def interrupt(*a, **kw):
                        if a[5] == "complete":
                            raise OSError("completion interrupted")
                        return original(*a, **kw)
                    # The adapter creates its own Store instance.
                    with patch("colab_daily.storage.Store.checkpoint_stage", side_effect=interrupt):
                        with self.assertRaises(OSError):
                            rating_command(args, lambda: module.prepare_track(fixture.run_dir, track))
                rating_command(args, lambda: module.prepare_track(fixture.run_dir, track))
                fixture.write_output(track, [track + "-1"])
            args = Namespace(owner="owner", run_dir=fixture.run_dir, command="assemble",
                             paper_output=None, news_output=None, policy_output=None)
            result = rating_command(args, lambda: module.assemble_production(fixture.run_dir))
            self.assertEqual(rating_command(args, lambda: self.fail("must reuse local assembly")), result)
            for name in ("tracks/news/context.json", "tracks/paper/terminal_output.json", "partition_manifest.json", "shared/canonical_objects.json"):
                path = fixture.run_dir / name
                content = path.read_bytes()
                path.unlink()
                with self.assertRaises(StorageError):
                    rating_command(args, lambda: self.fail("must not fabricate missing artifact"))
                path.write_bytes(content)
                path.write_bytes(content + b" ")
                with self.assertRaises(StorageError):
                    rating_command(args, lambda: self.fail("must detect changed artifact"))
                path.write_bytes(content)
        receipt = self.life.store.run_state("rating-run")["stages"]["assemble"]["payload"]
        for track in ("paper", "news", "policy"):
            self.assertEqual(receipt["identity"][track + "_context_id"], track + "-context")
        self.assertNotIn("files", receipt)

    def test_historical_event_fields_preserved_without_new_request_copy(self):
        event = {"key": "synthetic", "status": "seen"}
        self.life.store.update_source_state("arxiv", "original", 0, {}, [event])
        historical = {**event, "summary": "preserve synthetic historical summary"}
        with self.life.store.connection(write=True) as conn:
            conn.execute("UPDATE source_events SET event_json=? WHERE event_key='synthetic'", (json.dumps(historical),))
        self.life.store.update_source_state("arxiv", "new", 1, {}, [{**event, "status": "pending"}])
        self.assertEqual(self.life.store.source_state("arxiv")["events"][0]["summary"], historical["summary"])
        with self.life.store.connection() as conn:
            request = conn.execute("SELECT request_json FROM source_operations WHERE operation_id='new'").fetchone()[0]
        self.assertNotIn("summary", request)

    def test_control_paths_are_bounded_relative_names(self):
        for paths in (["/absolute"], ["../escape"], ["safe/../escape"], ["a"] * 65):
            with self.assertRaises(StorageError):
                control_metadata({"paths": paths})

    def test_partial_source_is_archived_and_retried(self):
        args = Namespace(project_root=self.root, cycle_id="daily-2026-01-02", owner="owner",
                         since=None, until=None, output_dir=None)
        calls = []
        def execute(args):
            calls.append(1)
            output = args.output_dir / "synthetic"
            output.mkdir()
            (output / "crawl_manifest.json").write_text(json.dumps({"status": "partial" if len(calls) == 1 else "success"}))
            (output / "record.md").write_text("first" if len(calls) == 1 else "first plus recovered feed")
            return 0
        self.assertEqual(source_command(args, execute, "synthetic"), 1)
        self.assertEqual(source_command(args, execute, "synthetic"), 0)
        self.assertEqual(len(calls), 2)
        self.assertTrue(list((self.life.working / ".source-attempts").rglob("record.md")))

    def test_control_schema_supports_real_metadata_and_rejects_documents(self):
        metadata = {"context_id": "isolated-context", "session_id": "session", "status": "running",
                    "identity": {"cycle_id": "daily-2026-01-02", "generation_id": "generation", "attempt": 1},
                    "paths": ["rating/run/input.json", "refine/result.json"],
                    "counts": {"records": 20, "bytes": 50000000}, "checksums": {"input": "a" * 64},
                    "reason": "temporary input verified"}
        self.assertEqual(control_metadata(metadata), metadata)
        self.life.store.claim_run("daily-2026-01-02", "run", "refine:paper", "owner", metadata)
        for payload in ({"content": SENTINEL * 1000}, {"files": {"record.md": SENTINEL}},
                        {"base64": base64.b64encode(SENTINEL.encode()).decode()},
                        {"identity": {"nested": {"body": SENTINEL}}},
                        {"reason": SENTINEL * 1000}):
            with self.subTest(keys=list(payload)):
                with self.assertRaises(StorageError):
                    self.life.store.checkpoint_stage("run", "owner", "refine", "bad", 0, "complete", payload)
        event = {"key": "synthetic", "status": "pending"}
        for cursor, events in (({"body": SENTINEL}, [event]), ({"page": {"summary": SENTINEL}}, [event]),
                               ({}, [{**event, "summary": SENTINEL}]),
                               ({}, [{**event, "source_categories": [{"body": SENTINEL}]}])):
            with self.assertRaises(StorageError):
                self.life.store.update_source_state("arxiv", "bad", 0, cursor, events)
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 0)
        self.assert_not_persisted()


if __name__ == "__main__":
    unittest.main()
