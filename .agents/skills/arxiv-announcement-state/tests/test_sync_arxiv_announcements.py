import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timezone
import base64
from pathlib import Path
from unittest.mock import patch

from colab_daily.config import Config
from colab_daily.lifecycle import Lifecycle
from colab_daily.storage import StorageError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "sync_arxiv_announcements.py"
SPEC = importlib.util.spec_from_file_location("sync_arxiv_announcements", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class AnnouncementStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.life = Lifecycle(Config.load(self.root, {}))
        self.life.begin("manual", "owner", "2026-01-02")
        self.args = Namespace(cycle_id="daily-2026-01-02", owner="owner", display_date=None,
                              since=None, until=None, output_dir=None, retention_days=30)
        self.row = {"source_id": "2607.12345", "version": "v1", "announce_type": "new", "announce_types": ["new"],
                    "announcement_at": "2026-01-01T01:00:00+00:00", "title": "Robot learning",
                    "summary": "Synthetic paper", "source_categories": ["cs.RO"], "matched_query_groups": {}}
        self.groups = {"embodied_robotics": ["robot learning"]}
        self.feed = patch.object(module, "feed_rows", return_value=([self.row], [{"status": "success"}])).start()
        self.metadata = patch.object(module, "api_metadata", side_effect=lambda rows: {r["source_id"]: {} for r in rows}).start()
        patch.object(module, "build_record", return_value="# Synthetic refined source\n").start()

    def tearDown(self):
        patch.stopall()
        self.temp.cleanup()

    def run_sync(self):
        return module.run_sync(self.args, self.life, ["cs.RO"], self.groups)

    def test_normalize_id_discards_version(self):
        self.assertEqual(module.normalize_id("oai:arXiv.org:2607.29169v3"), ("2607.29169", "v3"))

    def test_state_key_merges_new_and_cross(self):
        self.assertEqual(module.state_key({"source_id": "2607.12345", "announce_type": "new"}),
                         module.state_key({"source_id": "2607.12345", "announce_type": "cross"}))

    def test_state_round_trip_sqlite_not_legacy_json(self):
        archive = self.root / "state/arxiv/announcement_state.json"
        archive.parent.mkdir(parents=True)
        archive.write_text('{"not":"authority"}')
        self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["events"][0]["status"], "emitted")
        self.assertEqual(archive.read_text(), '{"not":"authority"}')

    def test_window_is_half_open(self):
        since, until = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.assertTrue(module.in_window({"announcement_at": since.isoformat()}, since, until))
        self.assertFalse(module.in_window({"announcement_at": until.isoformat()}, since, until))

    def test_metadata_failure_marks_pending_then_recovery_emits_missing_feed_row(self):
        self.metadata.side_effect = RuntimeError("temporary")
        with self.assertRaises(StorageError):
            self.run_sync()
        state = self.life.store.source_state("arxiv")
        self.assertEqual(state["events"][0]["status"], "pending")
        self.assertFalse((self.life.working / "arxiv").exists())
        self.feed.return_value = ([], [{"status": "success"}])
        self.metadata.side_effect = lambda rows: {r["source_id"]: {} for r in rows}
        self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["events"][0]["status"], "emitted")
        manifest = json.loads((self.life.working / "arxiv/crawl_manifest.json").read_text())
        self.assertEqual(manifest["pending_count"], 0)
        self.assertEqual(manifest["recovered_pending_count"], 1)
        self.assertEqual(manifest["emitted_record_count"], 1)

    def test_post_commit_export_crash_replays_without_network(self):
        with patch.object(module, "materialize_batch", side_effect=OSError("injected export crash")):
            with self.assertRaises(OSError):
                self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)
        self.assertFalse((self.life.working / "arxiv").exists())
        self.feed.side_effect = AssertionError("must replay without network")
        self.metadata.side_effect = AssertionError("must replay without network")
        self.run_sync()
        manifest = json.loads((self.life.working / "arxiv/crawl_manifest.json").read_text())
        self.assertEqual(manifest["source_revision"], 1)
        self.assertEqual(len(manifest["record_sha256"]), 1)
        self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)

    def test_cas_conflict_publishes_no_manifest_and_retry_succeeds(self):
        original = self.life.store.update_source_state
        def concurrent(*args, **kwargs):
            original("arxiv", "concurrent", 0, {"concurrent": True}, [])
            return original(*args, **kwargs)
        with patch.object(self.life.store, "update_source_state", side_effect=concurrent):
            with self.assertRaises(StorageError):
                self.run_sync()
        self.assertFalse((self.life.working / "arxiv").exists())
        self.assertEqual(self.life.store.source_state("arxiv")["events"], [])
        with self.assertRaisesRegex(StorageError, "stale source revision"):
            self.run_sync()
        self.args.retry_uncommitted = True
        self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 2)
        self.assertTrue((self.life.working / ".arxiv-attempts").is_dir())

    def test_duplicate_changed_inputs_and_corrupt_manifest_fail_closed(self):
        self.run_sync()
        self.args.retention_days = 31
        with self.assertRaises(StorageError):
            self.run_sync()
        self.args.retention_days = 30
        (self.life.working / "arxiv/crawl_manifest.json").write_text("{}")
        with self.assertRaises(StorageError):
            self.run_sync()

    def test_foreign_owner_wrong_window_and_cross_cycle_before_fetch(self):
        self.args.owner = "foreign"
        with self.assertRaises(StorageError):
            self.run_sync()
        self.args.owner = "owner"
        self.args.cycle_id = "daily-2026-01-03"
        with self.assertRaises(StorageError):
            self.run_sync()
        self.args.cycle_id = "daily-2026-01-02"
        self.args.since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.args.until = datetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaises(StorageError):
            self.run_sync()
        self.feed.assert_not_called()

    def test_record_and_summary_sentinels_are_temporary_not_in_sqlite(self):
        sentinel = "SYNTHETIC_ARXIV_BODY_NEVER_IN_SQLITE_019b"
        self.row["summary"] = sentinel * 10000
        self.row["title"] = sentinel
        with patch.object(module, "build_record", return_value=sentinel * 10000):
            self.run_sync()
        records = list((self.life.working / "arxiv").glob("*/record.md"))
        self.assertEqual(len(records), 1)
        self.assertIn(sentinel, records[0].read_text())
        with self.life.store.connection() as conn:
            dump = "\n".join(conn.iterdump())
            requests = [row[0] for row in conn.execute("SELECT request_json FROM source_operations")]
        self.assertNotIn(sentinel, dump)
        self.assertNotIn(base64.b64encode((sentinel * 3).encode()).decode()[:80], dump)
        self.assertLess(max(map(len, requests)), 5000)
        self.assertNotIn("summary", self.life.store.source_state("arxiv")["events"][0])
        self.assertNotIn("title", self.life.store.source_state("arxiv")["events"][0])
        records[0].unlink()
        self.args.retry_uncommitted = True
        self.feed.side_effect = AssertionError("a committed batch cannot be refetched")
        with self.assertRaises(StorageError):
            self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)

    def test_interrupted_staging_before_intent_never_marks_emitted(self):
        original = module.durable_write
        def interrupted(path, content, **kwargs):
            original(path, content, **kwargs)
            if Path(path).name == "record.md":
                raise OSError("injected staging interruption")
        with patch.object(module, "durable_write", side_effect=interrupted):
            with self.assertRaises(OSError):
                self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 0)
        self.assertIsNone(self.life.store.source_batch("arxiv", "arxiv-batch:" + self.args.cycle_id))
        with self.assertRaises(StorageError):
            self.run_sync()
        self.args.retry_uncommitted = True
        self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)
        self.assertTrue(list((self.life.working / ".arxiv-attempts").rglob("record.md")))

    def test_directory_fsync_failure_does_not_consume_events(self):
        with patch.object(module, "sync_dir", side_effect=OSError("injected directory fsync failure")):
            with self.assertRaises(OSError):
                self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 0)
        self.feed.side_effect = AssertionError("complete local staging should be verified and reused")
        self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)

    def test_recovery_parent_fsync_precedes_cas(self):
        original_sync = module.sync_dir
        def fail_parent(path):
            if path == self.life.working:
                raise OSError("parent fsync interrupted")
            original_sync(path)
        with patch.object(module, "sync_dir", side_effect=fail_parent):
            with self.assertRaises(OSError): self.run_sync()
            with self.assertRaises(OSError): self.run_sync()
        self.assertIsNone(self.life.store.source_batch("arxiv", "arxiv-batch:" + self.args.cycle_id))
        events = []
        original_cas = self.life.store.update_source_state
        def sync(path):
            original_sync(path)
            if path == self.life.working: events.append("parent")
        def cas(*a, **kw):
            self.assertIn("parent", events)
            events.append("cas")
            return original_cas(*a, **kw)
        with patch.object(module, "sync_dir", side_effect=sync), patch.object(self.life.store, "update_source_state", side_effect=cas):
            self.run_sync()
        self.assertLess(events.index("parent"), events.index("cas"))

    def test_partial_feeds_do_not_finalize_and_recall_pending(self):
        self.feed.return_value = ([self.row], [{"status": "success"}, {"status": "failed"}])
        with self.assertRaisesRegex(StorageError, "partial"):
            module.run_sync(self.args, self.life, ["cs.RO", "cs.AI"], self.groups)
        self.assertIsNone(self.life.store.source_batch("arxiv", "arxiv-batch:" + self.args.cycle_id))
        self.assertEqual(self.life.store.source_state("arxiv")["events"][0]["status"], "pending")
        second = {**self.row, "source_id": "2607.12346"}
        self.feed.return_value = ([second], [{"status": "success"}, {"status": "success"}])
        self.args.retry_uncommitted = True
        module.run_sync(self.args, self.life, ["cs.RO", "cs.AI"], self.groups)
        self.assertEqual(len(self.life.store.source_state("arxiv")["events"]), 2)
        self.assertTrue(all(e["status"] == "emitted" for e in self.life.store.source_state("arxiv")["events"]))
        self.assertEqual(self.feed.call_count, 2)
        self.assertTrue(list((self.life.working / ".arxiv-attempts").rglob("record.md")))

    def test_missing_disk_material_after_intent_blocks_emitted_cas(self):
        original = self.life.store.checkpoint_stage
        def remove_after_intent(*args, **kwargs):
            result = original(*args, **kwargs)
            if args[2] == "batch" and args[5] == "running":
                next(self.life.working.glob(".arxiv-batch-*/output/*/record.md")).unlink()
            return result
        with patch.object(self.life.store, "checkpoint_stage", side_effect=remove_after_intent):
            with self.assertRaises(StorageError):
                self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 0)
        self.assertEqual(self.life.store.source_state("arxiv")["events"], [])
        self.assertIsNone(self.life.store.source_batch("arxiv", "arxiv-batch:" + self.args.cycle_id))
        self.args.retry_uncommitted = True
        self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)

    def test_unknown_commit_result_uses_local_files_without_second_emission(self):
        original = self.life.store.update_source_state
        def unknown(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError("injected unknown commit result")
        with patch.object(self.life.store, "update_source_state", side_effect=unknown):
            with self.assertRaises(OSError):
                self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)
        self.args.retry_uncommitted = True
        self.feed.side_effect = AssertionError("known commit must not refetch")
        self.run_sync()
        self.run_sync()
        with self.life.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM source_operations").fetchone()[0], 1)
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)

    def test_symlink_in_committed_staging_is_not_followed_or_overwritten(self):
        with patch.object(module, "materialize_batch", side_effect=OSError("interrupted rename")):
            with self.assertRaises(OSError):
                self.run_sync()
        record = next(self.life.working.glob(".arxiv-batch-*/output/*/record.md"))
        outside = self.root / "outside.md"
        outside.write_text("preserve external data")
        record.unlink()
        record.symlink_to(outside)
        self.feed.side_effect = AssertionError("must not refetch")
        with self.assertRaises(StorageError):
            self.run_sync()
        self.assertEqual(outside.read_text(), "preserve external data")
        self.assertEqual(self.life.store.source_state("arxiv")["revision"], 1)

    def test_previous_cycle_emission_not_regressed_on_metadata_failure(self):
        key = module.state_key(self.row)
        event = module.event_identity({**self.row, "key": key, "status": "emitted", "cycle_id": "older-cycle"})
        self.life.store.update_source_state("arxiv", "legacy", 0, {}, [event])
        self.metadata.side_effect = RuntimeError("temporary")
        with self.assertRaises(StorageError):
            self.run_sync()
        self.assertEqual(self.life.store.source_state("arxiv")["events"], [event])


if __name__ == "__main__":
    unittest.main()
