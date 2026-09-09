import ast
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from colab_daily.config import Config
from colab_daily.storage import Store, StorageError, VALIDATION_CHECKS, digest
from colab_daily.storage.__main__ import main
from colab_daily.storage.files import atomic_write, canonical, read_bytes


def legacy_fixture(root, count=2):
    root.mkdir()
    (root / "attachments").mkdir()
    content = b"\x00synthetic image\xff\n"
    (root / "attachments" / "one.bin").write_bytes(content)
    publish = {"id": 7, "fields": {"CycleID": "legacy-cycle", "Phase": "PHASE_human",
        "Created": 1700000000.0, "Updated": 1700000010.5, "HumanFinished": False,
        "DisplayDate": 1700000000, "WindowSince": 1699900000, "WindowUntil": 1700000000,
        "Title": "Synthetic history", "PhaseDetail": "history only"}}
    records = [{"id": 10 + i, "fields": {"CandidateID": f"legacy-candidate-{i}", "Publish": 7,
        "Title": f"Record {i}", "Created": 1700000000, "Updated": 1700000010.5,
        "PreviewImage": ["L", 4, 4] if i == 0 else None,
        "RecordContent": json.dumps({"source_urls": ["https://example.org/source", "https://arxiv.org/abs/2401.01234v2"]}),
        "PublishContent": "# Detail\n", "Score": 90, "Selected": i == 0,
        "Category": "Paper", "Keywords": "one,two", "Summary": "summary", "GroupRank": i+1,
        "RatingTrack": "Paper", "ScoreScale": "100", "Extra": [None, False, 1, 1.0, "汉字"]}}
        for i in range(count)]
    columns = {"Publish": {"columns": []}, "Records": {"columns": [
        {"id": "Publish", "fields": {"type": "Ref:Publish"}},
        {"id": "PreviewImage", "fields": {"type": "Attachments"}}]}}
    data = {"tables": {"Publish": {"records": [publish]}, "Records": {"records": records}},
        "columns": columns, "attachments": [{"id": 4, "metadata": {"id": 4, "fields": {
            "fileName": "one.bin", "fileSize": len(content), "timeUploaded": 1700000000.0}},
            "path": "attachments/one.bin", "bytes": len(content)}], "attachment_failures": []}
    path = root / "export.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path, data, content


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "storage")

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, count=2):
        return legacy_fixture(self.root / "migration", count)

    def write_export(self, path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def publication(self, cycle="cycle", categories=("Paper",)):
        return {"schema_version": 3, "cycle_id": cycle, "display_date": "2026-01-01",
            "records": [{"candidate_id": f"candidate-{i}", "title": "Synthetic title",
                "category": category, "canonical_url": "https://example.org/source",
                "normalized_arxiv_id": None, "source_identities": ["https://example.org/source"],
                "content": "Full refined publication", "preview_image": "images/test.png"}
                for i, category in enumerate(categories)]}

    def validation(self, publication):
        return {"publication_sha256": digest(publication),
                "checks": dict.fromkeys(VALIDATION_CHECKS, True)}

    def frozen(self, cycle="cycle"):
        self.store.begin_cycle(cycle, {"display_date": "2026-01-01"})
        image = self.root / "image.png"
        image.write_bytes(b"synthetic image bytes")
        publication = self.publication(cycle)
        assets = {"images/test.png": image}
        frozen = self.store.freeze_publication(cycle, publication, assets, self.validation(publication))
        return publication, assets, frozen

    def deployment(self, frozen):
        return {"frozen_sha256": frozen, "reason": "offline mock readback verified",
                "local_verified": True, "push_verified": True, "public_verified": True,
                "artifact_sha256": "a" * 64, "commit_sha": "b" * 40}

    def test_import_exact_fields_ids_metadata_bytes_and_idempotence(self):
        path, data, content = self.fixture()
        result = self.store.import_legacy(path)
        self.assertEqual((result["publish"], result["records"], result["attachments"], result["attachment_relations"]), (1, 2, 1, 2))
        self.assertEqual(self.store.import_legacy(path), result)
        with self.store.connection() as conn:
            for table, payload in data["tables"].items():
                rows = conn.execute("SELECT row_id,fields_json FROM legacy_rows WHERE table_name=? ORDER BY row_id", (table,)).fetchall()
                self.assertEqual([(r[0], r[1]) for r in rows], [(r["id"], canonical(r["fields"])) for r in payload["records"]])
            attachment = conn.execute("SELECT * FROM attachments").fetchone()
            self.assertEqual(read_bytes(self.store.root / attachment["path"]), content)
            self.assertEqual(json.loads(attachment["metadata_json"]), data["attachments"][0]["metadata"])
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 3)
        self.assertEqual(self.store.status("legacy-cycle")["phase"], "Migrated")
        self.assertEqual(self.store.status("legacy-cycle")["source_phase"], "PHASE_human")
        exported = self.root / "copy.json"
        self.store.export_legacy(exported)
        self.assertEqual(exported.read_bytes(), path.read_bytes())

    def test_changed_import_fails_closed(self):
        path, data, _ = self.fixture()
        self.store.import_legacy(path)
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(StorageError, "changed"):
            self.store.import_legacy(path)
        self.assertEqual(self.store.status()["records"], 2)

    def test_anomalous_legacy_preview_archived_not_fabricated(self):
        path, data, _ = self.fixture()
        data["tables"]["Records"]["records"][0]["fields"]["PreviewImage"] = "legacy attachment error.bin"
        self.write_export(path, data)
        result = self.store.import_legacy(path)
        self.assertEqual(result["archived_anomalies"], 1)
        self.assertEqual(result["attachment_relations"], 0)
        with self.store.connection() as conn:
            raw = conn.execute("SELECT raw_json FROM import_anomalies").fetchone()[0]
            self.assertEqual(json.loads(raw), "legacy attachment error.bin")

    def test_invalid_migrations_do_not_mutate_database(self):
        path, original, _ = self.fixture()
        def duplicate_id(data):
            data["tables"]["Records"]["records"][1]["id"] = 10
        def duplicate_candidate(data):
            data["tables"]["Records"]["records"][1]["fields"]["CandidateID"] = "legacy-candidate-0"
        def bad_ref(data):
            data["tables"]["Records"]["records"][0]["fields"]["Publish"] = 999
        def bad_attachment(data):
            data["tables"]["Records"]["records"][0]["fields"]["PreviewImage"] = ["L", 999]
        def bad_list(data):
            data["tables"]["Records"]["records"][0]["fields"]["PreviewImage"] = ["X", 4]
        def duplicate_attachment(data):
            data["attachments"].append(data["attachments"][0])
        def missing_attachment(data):
            data["attachments"][0]["path"] = "attachments/missing.bin"
        def size(data):
            data["attachments"][0]["bytes"] += 1
        def failures(data):
            data["attachment_failures"] = ["synthetic"]
        for mutate in (duplicate_id, duplicate_candidate, bad_ref, bad_attachment, bad_list,
                       duplicate_attachment, missing_attachment, size, failures):
            with self.subTest(mutation=mutate.__name__):
                data = copy.deepcopy(original)
                mutate(data)
                self.write_export(path, data)
                with self.assertRaises((StorageError, FileNotFoundError)):
                    self.store.import_legacy(path)
                self.assertEqual(self.store.status()["publish"], 0)
                with self.store.connection() as conn:
                    self.assertEqual(conn.execute("SELECT count(*) FROM imports").fetchone()[0], 0)

    def test_duplicate_json_key_fails(self):
        path, _, _ = self.fixture()
        path.write_text('{"tables":{},"tables":{}}')
        with self.assertRaisesRegex(StorageError, "duplicate JSON"):
            self.store.import_legacy(path)

    def test_injected_import_failure_rolls_back_all_rows(self):
        path, _, _ = self.fixture()
        with patch.object(self.store, "_verify_legacy", side_effect=StorageError("injected")):
            with self.assertRaises(StorageError):
                self.store.import_legacy(path)
        with self.store.connection() as conn:
            for table in ("publish", "records", "attachments", "legacy_rows", "imports"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
        # Orphan immutable bytes are intentionally retained; retry is safe.
        self.assertTrue(self.store.import_legacy(path)["verified"])

    def test_operational_import_constraints_and_legacy_immutability(self):
        path, _, _ = self.fixture()
        self.store.import_legacy(path)
        with self.assertRaises(StorageError):
            self.store.begin_cycle("legacy-cycle", {})
        with self.assertRaises(StorageError):
            self.store.release("legacy-cycle")
        with self.store.connection(write=True) as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO records(publish_id,candidate_id,fields_json,identity_json) VALUES (7,'legacy-candidate-0','{}','{}')")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO records(publish_id,candidate_id,fields_json,identity_json) VALUES (999,'x','{}','{}')")
        other = Store(self.root / "other")
        other.begin_cycle("new", {})
        with self.assertRaisesRegex(StorageError, "empty"):
            other.import_legacy(path)

    def test_begin_cycle_idempotent_concurrent_changed_input(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(pool.map(lambda _: self.store.begin_cycle("same", {"window": [1, 2]}), range(12)))
        self.assertEqual(len({row["id"] for row in rows}), 1)
        with self.assertRaisesRegex(StorageError, "different"):
            self.store.begin_cycle("same", {"window": [1, 3]})
        self.assertEqual(self.store.status()["publish"], 1)

    def test_freeze_idempotence_changed_input_and_cleanup_recovery(self):
        publication, assets, frozen = self.frozen()
        self.assertEqual(self.store.freeze_publication("cycle", publication, assets, self.validation(publication)), frozen)
        changed = copy.deepcopy(publication)
        changed["records"][0]["content"] = "changed"
        with self.assertRaisesRegex(StorageError, "changed"):
            self.store.freeze_publication("cycle", changed, assets, self.validation(changed))
        original = self.store.export_publication("cycle")
        assets["images/test.png"].unlink()
        self.assertEqual(self.store.export_publication("cycle"), original)
        self.assertEqual(self.store.status()["records"], 1)
        self.assertEqual(self.store.status("cycle")["phase"], "PHASE_release")

    def test_freeze_asset_change_fails_and_corruption_detected(self):
        publication, assets, _ = self.frozen()
        assets["images/test.png"].write_bytes(b"different")
        with self.assertRaisesRegex(StorageError, "changed"):
            self.store.freeze_publication("cycle", publication, assets, self.validation(publication))
        export = self.store.export_publication("cycle")
        durable = self.store.root / export["assets"]["images/test.png"]["path"]
        durable.write_bytes(b"corrupted")
        with self.assertRaisesRegex(StorageError, "asset changed"):
            self.store.export_publication("cycle")
        with self.assertRaises(StorageError):
            self.store.backup(self.root / "backup")

    def test_freeze_requires_hash_bound_validations_and_unique_candidates(self):
        self.store.begin_cycle("cycle", {})
        pub = self.publication()
        with self.assertRaises(StorageError):
            self.store.freeze_publication("cycle", pub, {}, {})
        validation = self.validation(pub)
        validation["checks"]["refine"] = False
        with self.assertRaises(StorageError):
            self.store.freeze_publication("cycle", pub, {}, validation)
        pub["records"] *= 2
        with self.assertRaisesRegex(StorageError, "duplicate"):
            self.store.freeze_publication("cycle", pub, {}, self.validation(pub))
        self.assertEqual(self.store.status()["records"], 0)

    def test_new_production_quotas_not_relaxed(self):
        self.store.begin_cycle("cycle", {})
        for categories in (("Paper",) * 11, ("Policy",) * 4,
                           ("News",) * 8 + ("Policy",) * 3,
                           ("Paper",) * 10 + ("News",) * 11, ("unknown",)):
            with self.subTest(categories=categories):
                pub = self.publication(categories=categories)
                with self.assertRaises(StorageError):
                    self.store.freeze_publication("cycle", pub, {}, self.validation(pub))
        pub = self.publication(categories=("Paper",)*10 + ("News",)*7 + ("Policy",)*3)
        self.store.freeze_publication("cycle", pub, {}, self.validation(pub))
        self.assertEqual(self.store.status()["records"], 20)

    def test_freeze_transaction_rollback(self):
        self.store.begin_cycle("cycle", {})
        pub = self.publication()
        image = self.root / "image"
        image.write_bytes(b"asset")
        with patch.object(self.store, "_blob", side_effect=StorageError("injected")):
            with self.assertRaises(StorageError):
                self.store.freeze_publication("cycle", pub, {"image": image}, self.validation(pub))
        self.assertEqual(self.store.status()["records"], 0)
        self.assertIsNone(self.store.status("cycle")["frozen_sha256"])

    def test_prior_snapshot_whitelist_all_history_no_selected_filter(self):
        path, _, _ = self.fixture(count=21)
        self.store.import_legacy(path)
        self.store.begin_cycle("next", {})
        prior = self.store.prior_identity("next", "run")
        snapshot = prior["snapshot"]
        self.assertEqual(len(snapshot["records"]), 21)
        self.assertEqual(snapshot["before"], snapshot["after"])
        self.assertEqual(snapshot["previous_publish"]["phase"], "Migrated")
        self.assertEqual(set(snapshot["previous_publish"]), {"row_id", "cycle_id", "phase", "updated"})
        expected = {"row_id", "candidate_id", "title", "publish_row_id", "updated", "normalized_arxiv_id", "canonical_url", "source_identities"}
        for record in snapshot["records"]:
            self.assertEqual(set(record), expected)
            self.assertEqual(record["normalized_arxiv_id"], "2401.01234")
        self.assertEqual(prior["decisions"], [])

    def test_prior_original_coordinator_exact_shape_bridge(self):
        # Extract only the pure validator from the copied coordinator. No agent,
        # source discovery, network or workflow code is executed.
        source = Path(__file__).parents[3] / ".agents/skills/rating-filter-organize/scripts/coordinate_three_tracks.py"
        tree = ast.parse(source.read_text())
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_validate_prior_snapshot")
        namespace = {"COMPLETED_PRIOR_PHASES": {"Migrated", "Released"},
                     "FORBIDDEN_PRIOR_FIELDS": {"Score", "Summary", "Selected", "Category", "PublishContent", "RecordContent"}}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "coordinator-validator", "exec"), namespace)
        path, _, _ = self.fixture()
        self.store.import_legacy(path)
        self.store.begin_cycle("next", {})
        report = namespace["_validate_prior_snapshot"](self.store.prior_identity("next", "run"))
        self.assertEqual(report, {"first_cycle": False, "prior_record_count": 2})

    def test_first_cycle_and_successful_new_cycles_only(self):
        _, _, frozen = self.frozen()
        first = self.store.prior_identity("cycle", "run")["snapshot"]
        self.assertEqual(first, {"first_cycle": True, "previous_publish": None, "before": None, "after": None, "records": []})
        self.store.begin_cycle("next", {})
        self.assertTrue(self.store.prior_identity("next", "run")["snapshot"]["first_cycle"])
        self.store.start_delivery("cycle", "deployment", "deploy", {"frozen_sha256": frozen})
        self.store.record_delivery("cycle", "deployment", "deploy", "confirmed", self.deployment(frozen))
        self.store.release("cycle")
        self.assertTrue(self.store.prior_identity("next", "run")["snapshot"]["first_cycle"])
        snapshot = self.store.prior_identity("next", "run-after-release")["snapshot"]
        self.assertEqual(snapshot["previous_publish"]["phase"], "Released")
        self.assertEqual(len(snapshot["records"]), 1)

    def test_delivery_intent_concurrency_and_changed_input(self):
        _, _, frozen = self.frozen()
        request = {"frozen_sha256": frozen}
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.store.start_delivery("cycle", "deployment", "attempt", request), range(12)))
        self.assertEqual(sum(r["created_now"] for r in results), 1)
        with self.assertRaisesRegex(StorageError, "input changed"):
            self.store.start_delivery("cycle", "deployment", "attempt", {**request, "different": True})
        with self.assertRaisesRegex(StorageError, "reconcile"):
            self.store.start_delivery("cycle", "deployment", "other", request)

    def test_unknown_deployment_reconcile_and_release_gate(self):
        _, _, frozen = self.frozen()
        request = {"frozen_sha256": frozen}
        with self.assertRaises(StorageError):
            self.store.release("cycle")
        with self.assertRaises(StorageError):
            self.store.start_delivery("cycle", "notification", "note", request)
        self.store.start_delivery("cycle", "deployment", "attempt", request)
        evidence = {**request, "reason": "mock timeout"}
        self.store.record_delivery("cycle", "deployment", "attempt", "unknown", evidence)
        with self.assertRaises(StorageError):
            self.store.release("cycle")
        with self.assertRaises(StorageError):
            self.store.start_delivery("cycle", "deployment", "retry", request)
        bad = self.deployment(frozen)
        bad["public_verified"] = False
        with self.assertRaises(StorageError):
            self.store.record_delivery("cycle", "deployment", "attempt", "confirmed", bad)
        self.assertEqual(self.store.status("cycle")["phase"], "PHASE_release")
        self.store.record_delivery("cycle", "deployment", "attempt", "confirmed", self.deployment(frozen))
        self.assertEqual(self.store.release("cycle"), "Released")
        self.assertEqual(self.store.release("cycle"), "Released")
        with self.assertRaises(StorageError):
            self.store.record_delivery("cycle", "deployment", "attempt", "failed", evidence)
        with self.assertRaises(StorageError):
            self.store.start_delivery("cycle", "deployment", "retry", request)

    def test_failed_delivery_requires_absence_proof_and_new_attempt(self):
        _, _, frozen = self.frozen()
        request = {"frozen_sha256": frozen}
        self.store.start_delivery("cycle", "deployment", "first", request)
        evidence = {**request, "reason": "offline absence readback"}
        with self.assertRaises(StorageError):
            self.store.record_delivery("cycle", "deployment", "first", "failed", evidence)
        self.store.record_delivery("cycle", "deployment", "first", "failed", {**evidence, "side_effect_absent": True})
        self.assertTrue(self.store.start_delivery("cycle", "deployment", "second", request)["created_now"])

    def test_delivery_history_and_intended_artifact_binding(self):
        _, _, frozen = self.frozen()
        request = {"frozen_sha256": frozen, "artifact_sha256": "a" * 64}
        self.store.start_delivery("cycle", "deployment", "attempt", request)
        self.store.record_delivery("cycle", "deployment", "attempt", "unknown",
                                   {"frozen_sha256": frozen, "reason": "mock timeout"})
        evidence = self.deployment(frozen)
        evidence["artifact_sha256"] = "c" * 64
        with self.assertRaisesRegex(StorageError, "intended"):
            self.store.record_delivery("cycle", "deployment", "attempt", "confirmed", evidence)
        self.store.record_delivery("cycle", "deployment", "attempt", "confirmed", self.deployment(frozen))
        history = self.store.delivery_history("cycle", "deployment")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["request"], request)
        self.assertEqual([e["state"] for e in history[0]["events"]], ["pending", "unknown", "confirmed"])
        self.assertEqual(self.store.delivery_history("cycle", "notification"), [])

    def test_notification_independent_no_duplicate_on_unknown(self):
        _, _, frozen = self.frozen()
        request = {"frozen_sha256": frozen}
        self.store.start_delivery("cycle", "deployment", "deploy", request)
        self.store.record_delivery("cycle", "deployment", "deploy", "confirmed", self.deployment(frozen))
        self.store.release("cycle")
        self.store.start_delivery("cycle", "notification", "note", request)
        evidence = {**request, "reason": "offline timeout"}
        self.store.record_delivery("cycle", "notification", "note", "unknown", evidence)
        self.assertEqual(self.store.release("cycle"), "Released")
        with self.assertRaises(StorageError):
            self.store.start_delivery("cycle", "notification", "retry", request)
        self.store.record_delivery("cycle", "notification", "note", "confirmed", {**evidence, "accepted": True})
        self.assertFalse(self.store.start_delivery("cycle", "notification", "note", request)["created_now"])

    def test_backup_restore_full_legacy_and_frozen_recovery(self):
        path, _, _ = self.fixture()
        self.store.import_legacy(path)
        _, assets, _ = self.frozen()
        exported = self.store.export_publication("cycle")
        backup = self.root / "backup"
        result = self.store.backup(backup)
        self.assertEqual(result["files"], 3)
        restored = Store.restore(backup, self.root / "restored")
        self.assertTrue(restored.verify_legacy(path)["verified"])
        assets["images/test.png"].unlink()
        self.assertEqual(restored.export_publication("cycle"), exported)
        with self.assertRaises(StorageError):
            self.store.backup(backup)
        with self.assertRaises(StorageError):
            Store.restore(backup, self.root / "restored")

    def test_restore_corrupted_or_omitted_assets_fails_closed(self):
        self.frozen()
        backup = self.root / "backup"
        self.store.backup(backup)
        manifest = json.loads((backup / "manifest.json").read_text())
        asset = next(name for name in manifest["files"] if name.startswith("assets/"))
        original = (backup / asset).read_bytes()
        (backup / asset).write_bytes(b"corrupted")
        with self.assertRaises(StorageError):
            Store.restore(backup, self.root / "restored")
        self.assertFalse((self.root / "restored").exists())
        (backup / asset).write_bytes(original)
        del manifest["files"][asset]
        (backup / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaises(StorageError):
            Store.restore(backup, self.root / "restored")

    def test_paths_traversal_symlinks_and_private_permissions(self):
        self.assertEqual(self.store.root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.store.db.stat().st_mode & 0o777, 0o600)
        link = self.root / "link"
        link.symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaisesRegex(StorageError, "symlink"):
            Store(link)
        with self.assertRaisesRegex(StorageError, "traversal"):
            Store(self.root / "child" / ".." / "escape")
        path, data, _ = self.fixture()
        data["attachments"][0]["path"] = "../outside"
        self.write_export(path, data)
        with self.assertRaises(StorageError):
            self.store.import_legacy(path)
        destination = self.root / "output"
        destination.symlink_to(self.root / "unrelated")
        with self.assertRaises(StorageError):
            atomic_write(destination, b"data")
        with self.assertRaises(StorageError):
            self.store.backup(link / "backup")

    def test_symlink_asset_and_wal_rejected(self):
        pub, assets, _ = self.frozen()
        image = assets["images/test.png"]
        image.unlink()
        image.symlink_to(self.store.db)
        with self.assertRaises(StorageError):
            self.store.freeze_publication("cycle", pub, assets, self.validation(pub))
        wal = Path(str(self.store.db) + "-wal")
        self.assertFalse(wal.exists())
        wal.symlink_to(self.root / "unrelated")
        with self.assertRaises(StorageError):
            self.store.status()

    def test_run_ownership_checkpoints_immutable_completion_and_recovery(self):
        self.store.begin_cycle("cycle", {})
        context = {"context_id": "isolated-paper-agent", "counts": {"candidates": 1}, "paths": ["rating/input.json"]}
        self.assertTrue(self.store.claim_run("cycle", "run", "rating:paper", "owner", context)["created_now"])
        self.assertFalse(self.store.claim_run("cycle", "run", "rating:paper", "owner", context)["created_now"])
        # Wall-clock age is not an ownership lease or permission to take over.
        with self.store.connection(write=True) as conn:
            conn.execute("UPDATE runs SET created='2000-01-01T00:00:00+00:00' WHERE run_id='run'")
        with self.assertRaises(StorageError):
            self.store.claim_run("cycle", "other-run", "rating:paper", "other-owner", {})
        with self.assertRaises(StorageError):
            self.store.checkpoint_stage("run", "other-owner", "rating", "op", 0, "running", {})
        self.assertEqual(self.store.checkpoint_stage("run", "owner", "rating", "op-1", 0, "running", {"context_id": "persisted"}), 1)
        self.assertEqual(self.store.checkpoint_stage("run", "owner", "rating", "op-1", 0, "running", {"context_id": "persisted"}), 1)
        with self.assertRaises(StorageError):
            self.store.checkpoint_stage("run", "owner", "rating", "op-1", 0, "running", {"context_id": "changed"})
        with self.assertRaises(StorageError):
            self.store.checkpoint_stage("run", "owner", "rating", "op-2", 0, "failed", {})
        self.store.checkpoint_stage("run", "owner", "rating", "op-2", 1, "failed", {"reason": "mock"})
        self.store.checkpoint_stage("run", "owner", "rating", "op-3", 2, "complete", {"counts": {"records": 0}, "paths": ["rating/output.json"]})
        with self.assertRaises(StorageError):
            self.store.checkpoint_stage("run", "owner", "rating", "op-4", 3, "running", {})
        restored_store = Store(self.store.root)
        self.assertEqual(restored_store.run_state("run")["context"], context)
        self.assertEqual(restored_store.run_state("run")["stages"]["rating"]["revision"], 3)
        self.assertEqual(restored_store.run_state("run")["stages"]["rating"]["payload"], {"counts": {"records": 0}, "paths": ["rating/output.json"]})
        prior = self.store.prior_identity("cycle", "run")
        self.store.backup(self.root / "run-backup")
        restored = Store.restore(self.root / "run-backup", self.root / "run-restored")
        self.assertEqual(restored.run_state("run"), self.store.run_state("run"))
        self.assertEqual(restored.prior_identity("cycle", "run"), prior)

    def test_source_cursor_events_cas_idempotence_no_emitted_regression(self):
        self.assertEqual(self.store.source_state("arxiv")["revision"], 0)
        event = {"key": "2401.01234:new_or_cross", "status": "pending", "source_id": "2401.01234", "version": "v1"}
        cursor = {"schema_version": 1, "retention_days": 30, "last_successful_sync_at": None}
        self.assertEqual(self.store.update_source_state("arxiv", "op-1", 0, cursor, [event]), 1)
        self.assertEqual(self.store.update_source_state("arxiv", "op-1", 0, cursor, [event]), 1)
        with self.assertRaises(StorageError):
            self.store.update_source_state("arxiv", "op-1", 0, {"changed": True}, [event])
        with self.assertRaises(StorageError):
            self.store.update_source_state("arxiv", "op-2", 0, cursor, [event])
        emitted = {**event, "status": "emitted", "cycle_id": "cycle"}
        self.store.update_source_state("arxiv", "op-2", 1, cursor, [emitted])
        with self.assertRaises(StorageError):
            self.store.update_source_state("arxiv", "op-3", 2, cursor, [event])
        with self.assertRaises(StorageError):
            self.store.update_source_state("arxiv", "op-3", 2, cursor, [{**emitted, "cycle_id": "other"}])
        self.assertEqual(self.store.source_state("arxiv")["revision"], 2)
        self.store.update_source_state("arxiv", "op-3", 2, {**cursor, "last_successful_sync_at": "2026-01-01"}, [])
        self.assertEqual(self.store.source_state("arxiv")["events"], [emitted])
        self.store.backup(self.root / "source-backup")
        restored = Store.restore(self.root / "source-backup", self.root / "source-restored")
        self.assertEqual(restored.source_state("arxiv"), self.store.source_state("arxiv"))

    def test_source_pending_to_emitted_rolls_back_cursor_and_operation_on_failure(self):
        pending = {"key": "synthetic-event", "status": "pending"}
        self.store.update_source_state("arxiv", "pending-op", 0, {"position": 1}, [pending])
        with self.store.connection(write=True) as conn:
            conn.execute("""CREATE TRIGGER reject_emission BEFORE INSERT ON source_events
                            WHEN NEW.status='emitted'
                            BEGIN SELECT RAISE(ABORT, 'injected emission failure'); END""")
        emitted = {**pending, "status": "emitted", "cycle_id": "synthetic-cycle"}
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.update_source_state("arxiv", "emit-op", 1, {"position": 2}, [emitted], receipt={"paths": ["arxiv/record.md"], "counts": {"records": 1}})
        self.assertEqual(self.store.source_state("arxiv"), {
            "source": "arxiv", "revision": 1, "cursor": {"position": 1}, "events": [pending]})
        with self.store.connection(write=True) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM source_operations WHERE operation_id='emit-op'").fetchone()[0], 0)
            conn.execute("DROP TRIGGER reject_emission")
        self.assertIsNone(self.store.source_batch("arxiv", "emit-op"))
        receipt = {"paths": ["arxiv/record.md"], "counts": {"records": 1}}
        self.assertEqual(self.store.update_source_state("arxiv", "emit-op", 1, {"position": 2}, [emitted], receipt=receipt), 2)
        self.assertEqual(self.store.source_batch("arxiv", "emit-op"), {"revision": 2, "receipt": receipt})
        self.assertEqual(self.store.source_state("arxiv")["events"], [emitted])
        self.assertEqual(self.store.source_state("arxiv")["cursor"], {"position": 2})

    def test_source_state_concurrent_cas_has_single_winner(self):
        def update(index):
            try:
                return self.store.update_source_state("arxiv", f"op-{index}", 0, {}, [])
            except StorageError:
                return None
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(update, range(12)))
        self.assertEqual(results.count(1), 1)
        self.assertEqual(self.store.source_state("arxiv")["revision"], 1)

    def test_schema_v1_upgrade_preserves_legacy_data(self):
        path, _, _ = self.fixture()
        self.store.import_legacy(path)
        with self.store.connection(write=True) as conn:
            for table in ("stage_operations", "run_stages", "runs", "prior_snapshots", "source_operations", "source_events", "source_cursors"):
                conn.execute(f"DROP TABLE {table}")
            conn.execute("DELETE FROM schema_migrations WHERE version=2")
            conn.execute("PRAGMA user_version=1")
        upgraded = Store(self.store.root)
        self.assertTrue(upgraded.verify_legacy(path)["verified"])
        self.assertEqual(upgraded.status()["schema_version"], 3)
        self.assertEqual(upgraded.source_state("arxiv")["revision"], 0)

    def test_schema_version_rejected(self):
        with self.store.connection(write=True) as conn:
            conn.execute("PRAGMA user_version=999")
        with self.assertRaisesRegex(StorageError, "schema version"):
            Store(self.store.root)

    def test_project_config_and_cli_paths_not_cwd(self):
        project = self.root / "project"
        project.mkdir()
        (project / ".env").write_text("COLAB_STORAGE_DIR=private/data\nCOLAB_SITE_DIR=private/site\n")
        config = Config.load(project, {"COLAB_SITE_DIR": "override/site"})
        self.assertEqual(config.storage_dir, project / "private/data")
        self.assertEqual(config.site_dir, project / "override/site")
        self.assertEqual(config.working_dir, project / "working_tmp")
        self.assertEqual(Config.load(self.root, {}).storage_dir, self.root / "state/storage")
        (project / "metadata.json").write_text("{}")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(main(["--project-root", str(project), "begin-cycle", "--cycle", "synthetic-cycle", "--metadata", "metadata.json"]), 0)
            self.assertEqual(main(["--project-root", str(project), "status"]), 0)
            self.assertEqual(main(["--project-root", str(project), "release", "--cycle", "synthetic-cycle"]), 2)
        self.assertTrue((project / "private/data/storage.sqlite3").is_file())
        self.assertNotIn(str(project), stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("synthetic-cycle", stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
