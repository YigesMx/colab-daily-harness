from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr, redirect_stdout

from colab_daily.config import Config
from colab_daily.lifecycle import Lifecycle, daily_window, main
from colab_daily.adapters import source_command, rating_command
from colab_daily.storage import StorageError, digest, VALIDATION_CHECKS


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.life = Lifecycle(Config.load(self.root, {}))

    def tearDown(self):
        self.temp.cleanup()

    def begin(self, day="2026-01-02", owner="owner"):
        return self.life.begin("manual", owner, day, context={"session_id": "isolated"})

    def release(self, cycle):
        publication = {"schema_version": 3, "cycle_id": cycle, "display_date": cycle[6:], "records": [
            {"candidate_id": "candidate", "title": "Synthetic", "category": "Paper", "source_identities": []}]}
        frozen = self.life.store.freeze_publication(cycle, publication, {}, {
            "publication_sha256": digest(publication), "checks": dict.fromkeys(VALIDATION_CHECKS, True)})
        self.life.store.start_delivery(cycle, "deployment", "mock", {"frozen_sha256": frozen})
        self.life.store.record_delivery(cycle, "deployment", "mock", "confirmed", {
            "frozen_sha256": frozen, "reason": "offline mock", "local_verified": True,
            "push_verified": True, "public_verified": True, "commit_sha": "a" * 40, "artifact_sha256": "b" * 64})
        self.life.store.release(cycle)

    def test_shanghai_scheduled_window_frozen_once_and_retry_not_today(self):
        observed = datetime(2026, 1, 1, 17, tzinfo=timezone.utc)
        state = self.life.begin("scheduled", "owner", now=observed)
        self.assertEqual(state["cycle_id"], "daily-2026-01-02")
        self.assertEqual(state["input"]["window_since"], "2026-01-01T00:00:00+08:00")
        self.assertEqual(state["input"]["window_until"], "2026-01-02T00:00:00+08:00")
        with patch("colab_daily.lifecycle.datetime", wraps=datetime) as clock:
            retry = self.life.begin("retry", "owner")
            clock.now.assert_not_called()
        self.assertEqual(retry, state)

    def test_manual_explicit_window_and_changed_begin_fail_closed(self):
        with self.assertRaises(StorageError):
            self.life.begin("manual", "owner")
        state = self.begin()
        self.assertEqual(self.begin(), state)
        self.assertEqual(self.life.begin("retry", "owner", since="2025-12-31T16:00:00Z", until="2026-01-01T16:00:00Z"), state)
        for kwargs in ({"context": {"session_id": "changed"}}, {"since": "2026-01-01T00:00:00"},
                       {"since": "2026-01-01T00:00:00Z", "until": "2026-01-02T00:00:00Z"},
                       {"display_date": "2026-01-03"}):
            with self.assertRaises((StorageError, ValueError)):
                self.life.begin("retry", "owner", **kwargs)
        self.assertEqual(self.life.store.status()["publish"], 1)

    def test_one_workspace_cross_cycle_and_owner_conflicts_are_atomic(self):
        self.begin()
        with self.assertRaises(StorageError):
            self.life.begin("manual", "other", "2026-01-03")
        with self.assertRaises(StorageError):
            self.life.begin("manual", "other", "2026-01-02")
        self.assertEqual(self.life.store.status()["publish"], 1)
        with self.life.store.connection(write=True) as conn:
            conn.execute("UPDATE workspace_claims SET created='2000-01-01'")
        with self.assertRaises(StorageError):
            self.life.begin("retry", "other", cycle_id="daily-2026-01-02")

    def test_concurrent_different_cycle_claim_has_one_winner(self):
        def begin(index):
            try:
                return self.life.begin("manual", f"owner-{index}", f"2026-01-{index+1:02d}")["cycle_id"]
            except StorageError:
                return None
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(begin, range(4)))
        self.assertEqual(sum(r is not None for r in results), 1)
        self.assertEqual(self.life.store.status()["publish"], 1)

    def test_unowned_payload_symlink_and_output_escape_fail_closed(self):
        self.life.working.mkdir()
        (self.life.working / "unowned").write_text("preserve")
        with self.assertRaises(StorageError):
            self.begin()
        self.assertEqual(self.life.store.status()["publish"], 0)
        self.assertTrue((self.life.working / "unowned").exists())
        with self.assertRaises(StorageError):
            self.life.workspace_path("state/escape")
        with self.assertRaises(StorageError):
            self.life.workspace_path("working_tmp/../state")

    def test_cleanup_only_released_owner_no_other_cycle_deletion(self):
        self.begin()
        payload = self.life.working / "temporary"
        payload.write_bytes(b"transient")
        for owner in ("other", "owner"):
            with self.assertRaises(StorageError):
                self.life.cleanup(owner, "daily-2026-01-02")
        self.release("daily-2026-01-02")
        outside = self.root / "outside"
        outside.write_bytes(b"preserve")
        link = self.life.working / "link"
        link.symlink_to(outside)
        with self.assertRaises(StorageError):
            self.life.cleanup("owner", "daily-2026-01-02")
        link.unlink()
        self.life.cleanup("owner", "daily-2026-01-02")
        self.assertFalse(self.life.working.exists())
        self.assertTrue(outside.exists())
        self.begin("2026-01-03", "next-owner")
        (self.life.working / "next").write_text("keep")
        self.life.cleanup("owner", "daily-2026-01-02")
        self.assertTrue((self.life.working / "next").exists())
        with self.assertRaises(StorageError):
            self.life.require("owner")

    def test_cleanup_interruption_resumes_without_new_owner(self):
        self.begin()
        self.release("daily-2026-01-02")
        with patch("colab_daily.lifecycle.shutil.rmtree", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.life.cleanup("owner", "daily-2026-01-02")
        with self.assertRaises(StorageError):
            self.life.begin("manual", "next", "2026-01-03")
        self.life.cleanup("owner", "daily-2026-01-02")
        self.assertFalse(self.life.working.exists())

    def test_prior_reexport_and_actual_coordinator_accept_migrated_only(self):
        # Reuse the source-named synthetic legacy fixture, never live migration.
        from test_store import legacy_fixture
        export, data, _ = legacy_fixture(self.root / "migration", count=21)
        self.life.store.import_legacy(export)
        state = self.begin()
        prior = self.life.prior("owner", state["cycle_id"], "rating-run", "working_tmp/prior.json")
        (self.life.working / "prior.json").unlink()
        self.assertEqual(self.life.prior("owner", state["cycle_id"], "rating-run", "working_tmp/prior.json"), prior)
        script = Path(__file__).parents[3] / ".agents/skills/rating-filter-organize/scripts/coordinate_three_tracks.py"
        spec = importlib.util.spec_from_file_location("coordinator_bridge", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module._validate_prior_snapshot(prior)["prior_record_count"], 21)
        for phase in ("Released", "PHASE_human", "PHASE_publish"):
            changed = json.loads(json.dumps(prior))
            changed["snapshot"]["previous_publish"]["phase"] = phase
            with self.assertRaises(ValueError):
                module._validate_prior_snapshot(changed)

    def test_cli_root_paths_context_and_stage_persist_outside_cwd(self):
        (self.root / "context.json").write_text('{"session_id":"synthetic"}')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--project-root", str(self.root), "begin", "--trigger", "manual", "--display-date", "2026-01-02", "--owner", "owner", "--context", "context.json", "--output", "working_tmp/context.json"]), 0)
            self.assertEqual(main(["--project-root", str(self.root), "claim-run", "--owner", "owner", "--run", "run", "--kind", "rating:paper", "--context", "context.json"]), 0)
            self.assertEqual(main(["--project-root", str(self.root), "checkpoint", "--owner", "owner", "--run", "run", "--stage", "refine", "--operation", "op", "--revision", "0", "--status", "complete", "--input", "context.json"]), 0)
        self.assertEqual(self.life.store.run_state("run")["stages"]["refine"]["payload"], {"session_id": "synthetic"})
        self.assertTrue((self.life.working / "context.json").exists())

    def test_source_success_reuses_existing_files_but_missing_files_fail(self):
        self.begin()
        args = Namespace(project_root=self.root, cycle_id="daily-2026-01-02", owner="owner", since=None, until=None, output_dir=None)
        def execute(args):
            directory = args.output_dir / "synthetic"
            directory.mkdir()
            (directory / "record.md").write_text("complete source content")
            return 0
        self.assertEqual(source_command(args, execute, "synthetic"), 0)
        self.assertEqual(source_command(args, lambda _: self.fail("must not fetch"), "synthetic"), 0)
        (self.life.working / "synthetic/record.md").unlink()
        with self.assertRaisesRegex(StorageError, "missing or changed"):
            source_command(args, lambda _: self.fail("must not blindly fetch"), "synthetic")

    def test_interrupted_source_running_intent_recovers_partial_bytes(self):
        self.begin()
        args = Namespace(project_root=self.root, cycle_id="daily-2026-01-02", owner="owner", since=None, until=None, output_dir=None)
        def interrupted(args):
            directory = args.output_dir / "synthetic"
            directory.mkdir()
            (directory / "partial.md").write_text("partial source bytes")
            raise KeyboardInterrupt("simulated abrupt interruption")
        with self.assertRaises(KeyboardInterrupt):
            source_command(args, interrupted, "synthetic")
        run = self.life.store.run_state("daily-2026-01-02:source:synthetic")
        self.assertEqual(run["stages"]["crawl"]["status"], "running")
        def recover(args):
            directory = args.output_dir / "synthetic"
            directory.mkdir()
            (directory / "record.md").write_text("complete")
            return 0
        self.assertEqual(source_command(args, recover, "synthetic"), 0)
        self.assertFalse((self.life.working / "synthetic/partial.md").exists())
        with self.life.store.connection() as conn:
            requests = [json.loads(row[0]) for row in conn.execute("SELECT request_json FROM stage_operations")]
        archives = list((self.life.working / ".source-attempts/synthetic").glob("*/partial.md"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(archives[0].read_text(), "partial source bytes")
        self.assertTrue(any(len(request["payload"].get("paths", [])) == 2 for request in requests))
        self.assertNotIn("partial source bytes", json.dumps(requests))

    def test_inventory_control_receipt_durable_but_documents_remain_temporary(self):
        from colab_daily.inventory_adapter import inventory_command
        self.begin()
        receipt = self.life.working / ".phase/source-discovery.json"
        receipt.parent.mkdir()
        receipt.write_text('{"sources":[],"cycle_id":"daily-2026-01-02"}')
        output = self.life.working / "inventory.json"
        args = Namespace(owner="owner", cycle_id="daily-2026-01-02", run_id="rating-run",
                         crawl_root=self.life.working, source_receipt=receipt, output=output)
        def execute(args):
            args.output.write_text('{"records":[]}')
            return 0
        with patch("colab_daily.inventory_adapter.Config.load", return_value=self.life.config):
            self.assertEqual(inventory_command(args, execute), 0)
            self.assertEqual(inventory_command(args, lambda _: self.fail("must reuse validated local inventory")), 0)
            receipt.write_text('{"changed":true}')
            with self.assertRaises(StorageError):
                inventory_command(args, execute)
            receipt.unlink()
            output.unlink()
            with self.assertRaisesRegex(StorageError, "missing/changed"):
                inventory_command(args, lambda _: self.fail("must not pretend to recover from DB"))
        payload = self.life.store.run_state("rating-run:inventory")["stages"]["inventory"]["payload"]
        self.assertEqual(payload["counts"], {"sources": 0, "records": 0})
        self.assertNotIn("inventory", payload)

    def test_rating_control_receipt_persisted_and_missing_input_fails(self):
        self.begin()
        run_dir = self.life.working / "rating_filter_organize/runs/rating-run"
        (run_dir / "shared").mkdir(parents=True)
        (run_dir / "shared/routing.json").write_text(json.dumps({"cycle_id": "daily-2026-01-02"}))
        args = Namespace(run_dir=run_dir, owner="owner", command="prepare-track", track="paper")
        input_path = run_dir / "tracks/paper/input.json"
        def execute():
            input_path.parent.mkdir(parents=True)
            input_path.write_text('{"context_id":"isolated-paper"}')
            return {"status": "prepared"}
        with patch("colab_daily.adapters.Config.load", return_value=self.life.config):
            result = rating_command(args, execute)
            self.assertEqual(rating_command(args, lambda: self.fail("must reuse local result")), result)
            input_path.unlink()
            with self.assertRaisesRegex(StorageError, "missing/changed"):
                rating_command(args, lambda: self.fail("must not recover document bytes from DB"))
        payload = self.life.store.run_state("rating-run")["stages"]["prepare:paper"]["payload"]
        self.assertIn(input_path.relative_to(self.life.working).as_posix(), payload["paths"])
        self.assertNotIn("files", payload)

    def test_source_guard_no_fetch_for_foreign_owner_or_changed_window(self):
        self.begin()
        args = Namespace(project_root=self.root, cycle_id="daily-2026-01-02", owner="other", since=None, until=None, output_dir=None)
        with self.assertRaises(StorageError):
            source_command(args, lambda _: self.fail("must not fetch"), "synthetic")
        args.owner = "owner"
        args.since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        args.until = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaises(StorageError):
            source_command(args, lambda _: self.fail("must not fetch"), "synthetic")


if __name__ == "__main__":
    unittest.main()
