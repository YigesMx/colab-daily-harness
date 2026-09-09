"""Durable control metadata and source identities; intermediate documents stay temporary."""
from datetime import datetime, timezone
import json
import sqlite3

from .files import StorageError, canonical
from .metadata import control_metadata, identity_event, cursor_metadata


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def require_text(value):
    if not isinstance(value, str) or not value.strip():
        raise StorageError("runtime identity must be nonempty text")


class RuntimeState:
    """Mixin uses Store's transactional connections; no external side effects."""

    def claim_run(self, cycle_id, run_id, kind, owner, context):
        for value in (run_id, kind, owner):
            require_text(value)
        if not isinstance(context, dict):
            raise StorageError("run context must be an object")
        payload = canonical(control_metadata(context))
        with self.connection(write=True) as conn:
            cycle = self._cycle(conn, cycle_id)
            if cycle["legacy"]:
                raise StorageError("cannot create production runs for migrated history")
            previous = conn.execute("SELECT * FROM runs WHERE run_id=? OR (publish_id=? AND kind=?)", (run_id, cycle["id"], kind)).fetchone()
            if previous:
                if (previous["run_id"], previous["publish_id"], previous["kind"], previous["owner"], previous["context_json"]) != (run_id, cycle["id"], kind, owner, payload):
                    raise StorageError("run identity/kind already claimed with different owner/context")
                return {"run_id": run_id, "owner": owner, "created_now": False}
            if cycle["phase"] != "PHASE_release":
                raise StorageError("new run requires PHASE_release")
            conn.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (run_id, cycle["id"], kind, owner, payload, timestamp()))
            return {"run_id": run_id, "owner": owner, "created_now": True}

    def run_state(self, run_id):
        with self.connection() as conn:
            conn.execute("BEGIN")
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise StorageError("run not found")
            return {"run_id": run_id, "publish_id": run["publish_id"], "kind": run["kind"],
                    "owner": run["owner"], "context": json.loads(run["context_json"]),
                    "stages": {s["stage"]: {"revision": s["revision"], "status": s["status"],
                                            "payload": json.loads(s["payload_json"])}
                               for s in conn.execute("SELECT * FROM run_stages WHERE run_id=? ORDER BY stage", (run_id,))}}

    def checkpoint_stage(self, run_id, owner, stage, operation_id, expected_revision, status, payload):
        for value in (run_id, owner, stage, operation_id):
            require_text(value)
        if type(expected_revision) is not int or expected_revision < 0 or status not in ("running", "failed", "complete") or not isinstance(payload, dict):
            raise StorageError("invalid stage checkpoint")
        control_metadata(payload)
        request = canonical({"owner": owner, "stage": stage, "expected_revision": expected_revision,
                             "status": status, "payload": payload})
        with self.connection(write=True) as conn:
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["owner"] != owner:
                raise StorageError("run owner mismatch")
            old_operation = conn.execute("SELECT * FROM stage_operations WHERE run_id=? AND operation_id=?", (run_id, operation_id)).fetchone()
            if old_operation:
                if old_operation["request_json"] != request:
                    raise StorageError("checkpoint operation retry changed input")
                return old_operation["revision"]
            cycle = conn.execute("SELECT phase FROM publish WHERE id=?", (run["publish_id"],)).fetchone()
            if cycle["phase"] != "PHASE_release":
                raise StorageError("Released run checkpoints are immutable")
            old = conn.execute("SELECT * FROM run_stages WHERE run_id=? AND stage=?", (run_id, stage)).fetchone()
            revision = old["revision"] if old else 0
            if revision != expected_revision:
                raise StorageError("stale stage revision; reload SQLite checkpoint")
            if old and old["status"] == "complete":
                raise StorageError("completed stage cannot change; retry the original operation")
            revision += 1
            stamp = timestamp()
            conn.execute("INSERT INTO run_stages VALUES (?,?,?,?,?,?) ON CONFLICT(run_id,stage) DO UPDATE SET revision=excluded.revision,status=excluded.status,payload_json=excluded.payload_json,updated=excluded.updated",
                         (run_id, stage, revision, status, canonical(payload), stamp))
            conn.execute("INSERT INTO stage_operations VALUES (?,?,?,?,?)", (run_id, operation_id, request, revision, stamp))
            return revision

    def source_state(self, source):
        require_text(source)
        with self.connection() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT * FROM source_cursors WHERE source=?", (source,)).fetchone()
            return {"source": source, "revision": row["revision"] if row else 0,
                    "cursor": json.loads(row["cursor_json"]) if row else {},
                    "events": [json.loads(e[0]) for e in conn.execute("SELECT event_json FROM source_events WHERE source=? ORDER BY event_key", (source,))]}

    def source_batch(self, source, operation_id):
        """Return a committed bounded receipt, never document bytes."""
        with self.connection() as conn:
            row = conn.execute("SELECT request_json,revision FROM source_operations WHERE source=? AND operation_id=?", (source, operation_id)).fetchone()
            if row is None:
                return None
            request = json.loads(row["request_json"])
            return {"revision": row["revision"], "receipt": request.get("receipt")}

    def update_source_state(self, source, operation_id, expected_revision, cursor, events, receipt=None):
        """Atomically CAS cursor + event upserts, retaining unmentioned events.

        For arXiv, source='arxiv' and key is source_id:announcement_family. Caller
        owns cursor semantics and event enrichment. No automatic pruning/deletion.
        """
        require_text(source)
        require_text(operation_id)
        if type(expected_revision) is not int or expected_revision < 0 or not isinstance(cursor, dict) or not isinstance(events, list):
            raise StorageError("invalid source update")
        keys = set()
        for event in events:
            if not isinstance(event, dict):
                raise StorageError("source event must be an object")
            require_text(event.get("key"))
            if event["key"] in keys or event.get("status") not in ("seen", "pending", "emitted"):
                raise StorageError("duplicate/invalid source event")
            keys.add(event["key"])
        request_value = {"expected_revision": expected_revision, "cursor": cursor, "events": events}
        if receipt is not None:
            request_value["receipt"] = control_metadata(receipt)
        request = canonical(request_value)
        with self.connection(write=True) as conn:
            operation = conn.execute("SELECT * FROM source_operations WHERE source=? AND operation_id=?", (source, operation_id)).fetchone()
            if operation:
                if operation["request_json"] != request:
                    raise StorageError("source operation retry changed input")
                return operation["revision"]
            # Exact legacy-operation retries above remain immutable. New requests
            # cannot archive source prose/binaries under an event or receipt key.
            cursor_metadata(cursor)
            for event in events:
                identity_event(event)
            previous = conn.execute("SELECT revision FROM source_cursors WHERE source=?", (source,)).fetchone()
            revision = previous[0] if previous else 0
            if revision != expected_revision:
                raise StorageError("stale source revision; reload SQLite source state")
            for event in events:
                old = conn.execute("SELECT status,event_json FROM source_events WHERE source=? AND event_key=?", (source, event["key"])).fetchone()
                if old and old["status"] == "emitted":
                    old_event = json.loads(old["event_json"])
                    if event["status"] != "emitted" or event.get("cycle_id") != old_event.get("cycle_id"):
                        raise StorageError("emitted source identity cannot regress or move cycles")
            revision += 1
            stamp = timestamp()
            conn.execute("INSERT INTO source_cursors VALUES (?,?,?,?) ON CONFLICT(source) DO UPDATE SET revision=excluded.revision,cursor_json=excluded.cursor_json,updated=excluded.updated",
                         (source, revision, canonical(cursor), stamp))
            for event in events:
                old = conn.execute("SELECT event_json FROM source_events WHERE source=? AND event_key=?", (source, event["key"])).fetchone()
                # Preserve previously migrated fields in-place, without copying
                # historical prose into new operation journals.
                saved = {**(json.loads(old[0]) if old else {}), **event}
                conn.execute("INSERT INTO source_events VALUES (?,?,?,?) ON CONFLICT(source,event_key) DO UPDATE SET status=excluded.status,event_json=excluded.event_json",
                             (source, event["key"], event["status"], canonical(saved)))
            conn.execute("INSERT INTO source_operations VALUES (?,?,?,?,?)", (source, operation_id, request, revision, stamp))
            return revision
