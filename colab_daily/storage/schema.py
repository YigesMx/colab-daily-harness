VERSION = 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
 version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS workspace_claims (
 publish_id INTEGER PRIMARY KEY REFERENCES publish(id), owner TEXT NOT NULL UNIQUE,
 path TEXT NOT NULL, context_json TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('active','cleaning','closed')),
 created TEXT NOT NULL, updated TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_workspace ON workspace_claims((1))
 WHERE state != 'closed';
CREATE TABLE IF NOT EXISTS runs (
 run_id TEXT PRIMARY KEY, publish_id INTEGER NOT NULL REFERENCES publish(id),
 kind TEXT NOT NULL, owner TEXT NOT NULL, context_json TEXT NOT NULL,
 created TEXT NOT NULL, UNIQUE(publish_id,kind)
);
CREATE TABLE IF NOT EXISTS run_stages (
 run_id TEXT NOT NULL REFERENCES runs(run_id), stage TEXT NOT NULL,
 revision INTEGER NOT NULL, status TEXT NOT NULL CHECK(status IN ('running','failed','complete')),
 payload_json TEXT NOT NULL, updated TEXT NOT NULL, PRIMARY KEY(run_id,stage)
);
CREATE TABLE IF NOT EXISTS stage_operations (
 run_id TEXT NOT NULL REFERENCES runs(run_id), operation_id TEXT NOT NULL,
 request_json TEXT NOT NULL, revision INTEGER NOT NULL, created TEXT NOT NULL,
 PRIMARY KEY(run_id,operation_id)
);
CREATE TABLE IF NOT EXISTS prior_snapshots (
 publish_id INTEGER NOT NULL REFERENCES publish(id), run_id TEXT NOT NULL,
 snapshot_json TEXT NOT NULL, PRIMARY KEY(publish_id,run_id)
);
CREATE TABLE IF NOT EXISTS source_cursors (
 source TEXT PRIMARY KEY, revision INTEGER NOT NULL, cursor_json TEXT NOT NULL,
 updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS source_events (
 source TEXT NOT NULL REFERENCES source_cursors(source), event_key TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('seen','pending','emitted')), event_json TEXT NOT NULL,
 PRIMARY KEY(source,event_key)
);
CREATE TABLE IF NOT EXISTS source_operations (
 source TEXT NOT NULL REFERENCES source_cursors(source), operation_id TEXT NOT NULL,
 request_json TEXT NOT NULL, revision INTEGER NOT NULL, created TEXT NOT NULL,
 PRIMARY KEY(source,operation_id)
);
CREATE TABLE IF NOT EXISTS imports (
 id INTEGER PRIMARY KEY CHECK(id=1), sha256 TEXT NOT NULL, raw_export BLOB NOT NULL,
 imported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_rows (
 table_name TEXT NOT NULL, row_id INTEGER NOT NULL, fields_json TEXT NOT NULL,
 PRIMARY KEY(table_name,row_id)
);
CREATE TABLE IF NOT EXISTS import_anomalies (
 table_name TEXT NOT NULL, row_id INTEGER NOT NULL, field_name TEXT NOT NULL,
 reason TEXT NOT NULL, raw_json TEXT NOT NULL,
 PRIMARY KEY(table_name,row_id,field_name)
);
CREATE TABLE IF NOT EXISTS publish (
 id INTEGER PRIMARY KEY, cycle_id TEXT NOT NULL UNIQUE,
 phase TEXT NOT NULL CHECK(phase IN ('Migrated','PHASE_release','Released')),
 source_phase TEXT, legacy INTEGER NOT NULL DEFAULT 0,
 begin_json TEXT NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
 publication_json TEXT, frozen_sha256 TEXT, validation_json TEXT
);
CREATE TABLE IF NOT EXISTS records (
 id INTEGER PRIMARY KEY, publish_id INTEGER NOT NULL REFERENCES publish(id),
 candidate_id TEXT NOT NULL, fields_json TEXT NOT NULL, identity_json TEXT NOT NULL,
 UNIQUE(publish_id,candidate_id)
);
CREATE TABLE IF NOT EXISTS attachments (
 id INTEGER PRIMARY KEY, metadata_json TEXT NOT NULL, sha256 TEXT NOT NULL,
 size INTEGER NOT NULL CHECK(size>=0), path TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS record_attachments (
 record_id INTEGER NOT NULL REFERENCES records(id), position INTEGER NOT NULL,
 attachment_id INTEGER NOT NULL REFERENCES attachments(id),
 PRIMARY KEY(record_id,position)
);
CREATE TABLE IF NOT EXISTS publication_assets (
 publish_id INTEGER NOT NULL REFERENCES publish(id), name TEXT NOT NULL,
 sha256 TEXT NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL,
 PRIMARY KEY(publish_id,name)
);
CREATE TABLE IF NOT EXISTS deliveries (
 publish_id INTEGER NOT NULL REFERENCES publish(id), channel TEXT NOT NULL
 CHECK(channel IN ('deployment','notification')), attempt_id TEXT NOT NULL,
 request_json TEXT NOT NULL, state TEXT NOT NULL
 CHECK(state IN ('pending','unknown','failed','confirmed')), evidence_json TEXT,
 created TEXT NOT NULL, updated TEXT NOT NULL,
 PRIMARY KEY(publish_id,channel,attempt_id)
);
CREATE TABLE IF NOT EXISTS delivery_events (
 id INTEGER PRIMARY KEY, publish_id INTEGER NOT NULL, channel TEXT NOT NULL,
 attempt_id TEXT NOT NULL, state TEXT NOT NULL, evidence_json TEXT, created TEXT NOT NULL,
 FOREIGN KEY(publish_id,channel,attempt_id) REFERENCES deliveries(publish_id,channel,attempt_id)
);
"""
