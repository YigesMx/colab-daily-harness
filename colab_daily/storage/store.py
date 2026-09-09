"""Transactional storage API; callers perform semantic validation and network I/O."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from .files import (StorageError, atomic_write, canonical, digest, private_dir,
                    read_bytes, read_json, relative_name, safe_path, sha256, sync_dir)
from .schema import SCHEMA, VERSION
from .runtime import RuntimeState

VALIDATION_CHECKS = frozenset({"grouped_selection", "refine", "schema", "taxonomy",
                               "images", "ownership", "quotas", "evidence"})


def now():
    return datetime.now(timezone.utc).isoformat()


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"{name} must be nonempty text")
    return value


def positive_id(value):
    if type(value) is not int or value <= 0:
        raise StorageError("row/reference IDs must be positive integers")
    return value


def stamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    return text(value, "timestamp")


def source_identity(fields):
    """Extract identities only, never scores/selection; raw source remains archived."""
    content = fields.get("RecordContent", "")
    try:
        obj = json.loads(content)
    except (TypeError, ValueError):
        obj = {}
    urls = []
    if isinstance(obj, dict):
        for key in ("source_identities", "source_urls"):
            values = obj.get(key, [])
            if isinstance(values, list):
                urls.extend(v for v in values if isinstance(v, str) and v)
        for key in ("canonical_url", "url", "source_url"):
            if isinstance(obj.get(key), str) and obj[key]:
                urls.append(obj[key])
    # Older records are Markdown, not JSON. Preserve URL spelling, remove only
    # Markdown delimiters. Identity decisions still belong to the agent.
    if not urls:
        urls.extend(re.findall(r"https?://[^\s<>\"\)\]\}]+", content))
    urls = list(dict.fromkeys(urls))
    arxiv = None
    for value in urls + [fields.get("CandidateID", "")]:
        match = re.search(r"(?:arxiv:|arxiv\.org/(?:abs|pdf)/)(\d{4}\.\d{4,5})(?:v\d+)?", value)
        if match:
            arxiv = match.group(1)
            break
    return {"title": fields.get("Title", ""), "updated": stamp(fields["Updated"]),
            "canonical_url": urls[0] if urls else None, "normalized_arxiv_id": arxiv,
            "source_identities": urls}


class Store(RuntimeState):
    """One relocatable directory containing storage.sqlite3 and assets/.

    Each method opens its own connection. Writers use BEGIN IMMEDIATE with a
    30-second busy timeout. There is no network I/O and no hidden human stage.
    """

    def __init__(self, directory):
        self.root = private_dir(directory)
        self.db = safe_path(self.root / "storage.sqlite3")
        self.assets = private_dir(self.root / "assets")
        fd = os.open(self.db, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.db, 0o600)
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, VERSION):
                raise StorageError("unsupported database schema version")
            conn.execute("PRAGMA journal_mode=WAL")
            # executescript includes the transaction: all DDL and version markers
            # commit together; concurrent initializers serialize at BEGIN.
            conn.executescript("BEGIN IMMEDIATE;\n" + SCHEMA +
                               f"\nPRAGMA user_version={VERSION};")
            for migration in range(1, VERSION + 1):
                conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)", (migration, now()))
            conn.commit()

    @contextmanager
    def connection(self, write=False):
        safe_path(self.db)
        for suffix in ("-wal", "-shm", "-journal"):
            safe_path(str(self.db) + suffix)
        conn = sqlite3.connect(self.db, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                conn.execute("BEGIN IMMEDIATE")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _blob(self, data):
        checksum = sha256(data)
        path = f"assets/{checksum[:2]}/{checksum}"
        atomic_write(self.root / path, data, immutable=True)
        return {"sha256": checksum, "size": len(data), "path": path}

    @staticmethod
    def _cycle(conn, cycle_id):
        row = conn.execute("SELECT * FROM publish WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise StorageError("cycle not found")
        return row

    def begin_cycle(self, cycle_id, metadata):
        """Immutable begin metadata, including display_date/window/run identities."""
        text(cycle_id, "cycle_id")
        if not isinstance(metadata, dict):
            raise StorageError("begin metadata must be an object")
        payload = canonical(metadata)
        with self.connection(write=True) as conn:
            previous = conn.execute("SELECT * FROM publish WHERE cycle_id=?", (cycle_id,)).fetchone()
            if previous:
                if previous["legacy"] or previous["begin_json"] != payload:
                    raise StorageError("cycle identity already exists with different input")
                return dict(previous)
            timestamp = now()
            conn.execute("INSERT INTO publish(cycle_id,phase,begin_json,created,updated) VALUES (?, 'PHASE_release',?,?,?)",
                         (cycle_id, payload, timestamp, timestamp))
            return dict(self._cycle(conn, cycle_id))

    def status(self, cycle_id=None):
        with self.connection() as conn:
            if cycle_id is None:
                return {"schema_version": VERSION, **{name: conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
                        for name in ("publish", "records", "attachments", "import_anomalies")},
                        "phases": {r[0]: r[1] for r in conn.execute("SELECT phase,count(*) FROM publish GROUP BY phase")}}
            row = self._cycle(conn, cycle_id)
            return {"cycle_id": row["cycle_id"], "publish_id": row["id"], "phase": row["phase"],
                    "source_phase": row["source_phase"], "frozen_sha256": row["frozen_sha256"],
                    "deliveries": [dict(r) for r in conn.execute(
                        "SELECT channel,attempt_id,state FROM deliveries WHERE publish_id=? ORDER BY created,attempt_id", (row["id"],))]}

    def import_legacy(self, export_path):
        """Import exactly one immutable legacy snapshot, preserving original row IDs.

        Validate all input before mutation. Broken files/references/duplicate IDs
        fail closed. Historic non-reference strings in attachment cells are
        explicitly archived as anomalies, never interpreted as successful images.
        """
        export_path = safe_path(export_path)
        raw = read_bytes(export_path)
        data = read_json(export_path)
        if read_bytes(export_path) != raw:
            raise StorageError("migration export changed during read")
        prepared = self._prepare_legacy(data, export_path.parent)
        with self.connection(write=True) as conn:
            existing = conn.execute("SELECT sha256 FROM imports WHERE id=1").fetchone()
            if existing:
                if existing[0] != sha256(raw):
                    raise StorageError("migration retry changed the original export bytes")
            else:
                if conn.execute("SELECT count(*) FROM publish").fetchone()[0]:
                    raise StorageError("legacy import requires an empty operational database")
                conn.execute("INSERT INTO imports VALUES (1,?,?,?)", (sha256(raw), raw, now()))
                for table, rows in prepared["tables"].items():
                    for row in rows:
                        conn.execute("INSERT INTO legacy_rows VALUES (?,?,?)", (table, row["id"], canonical(row["fields"])))
                for row in prepared["tables"]["Publish"]:
                    f = row["fields"]
                    conn.execute("INSERT INTO publish(id,cycle_id,phase,source_phase,legacy,begin_json,created,updated) VALUES (?,?,'Migrated',?,1,?,?,?)",
                                 (row["id"], f["CycleID"], f["Phase"], canonical(f), stamp(f["Created"]), stamp(f["Updated"])))
                for attachment, content in prepared["attachments"]:
                    blob = self._blob(content)
                    conn.execute("INSERT INTO attachments VALUES (?,?,?,?,?)", (attachment["id"], canonical(attachment["metadata"]),
                                 blob["sha256"], blob["size"], blob["path"]))
                for row in prepared["tables"]["Records"]:
                    f = row["fields"]
                    conn.execute("INSERT INTO records VALUES (?,?,?,?,?)", (row["id"], f["Publish"], f["CandidateID"],
                                 canonical(f), canonical(source_identity(f))))
                    for position, attachment_id in enumerate(prepared["relations"].get(row["id"], [])):
                        conn.execute("INSERT INTO record_attachments VALUES (?,?,?)", (row["id"], position, attachment_id))
                conn.executemany("INSERT INTO import_anomalies VALUES (?,?,?,?,?)", prepared["anomalies"])
            self._verify_legacy(conn, data, prepared, raw)
        return self.verify_legacy(export_path)

    def _prepare_legacy(self, data, source_dir):
        if set(data) != {"tables", "columns", "attachments", "attachment_failures"}:
            raise StorageError("unsupported legacy export envelope; preserve source and inspect schema")
        if data["attachment_failures"]:
            raise StorageError("legacy export reports attachment failures; complete export first")
        if set(data["tables"]) != {"Publish", "Records"} or set(data["columns"]) != {"Publish", "Records"}:
            raise StorageError("unsupported legacy tables/columns")
        tables = {name: value["records"] for name, value in data["tables"].items()}
        ids = {}
        for table, rows in tables.items():
            ids[table] = set()
            identities = set()
            for row in rows:
                rid = positive_id(row["id"])
                if rid in ids[table] or not isinstance(row["fields"], dict):
                    raise StorageError("duplicate legacy row ID or malformed fields; source preserved")
                ids[table].add(rid)
                f = row["fields"]
                key = text(f["CycleID"], "CycleID") if table == "Publish" else (positive_id(f["Publish"]), text(f["CandidateID"], "CandidateID"))
                if key in identities:
                    raise StorageError("duplicate legacy cycle/candidate identity; source preserved")
                identities.add(key)
        attachment_ids = set()
        attachments = []
        for attachment in data["attachments"]:
            aid = positive_id(attachment["id"])
            if aid in attachment_ids or attachment["metadata"].get("id") != aid:
                raise StorageError("duplicate/mismatched attachment ID")
            attachment_ids.add(aid)
            name = relative_name(attachment["path"])
            content = read_bytes(source_dir / name)
            metadata_size = attachment["metadata"].get("fields", {}).get("fileSize")
            if (type(attachment["bytes"]) is not int or len(content) != attachment["bytes"]
                    or (metadata_size is not None and (type(metadata_size) is not int or metadata_size != len(content)))):
                raise StorageError("attachment size mismatch; complete export first")
            attachments.append((attachment, content))
        relations, anomalies = {}, []
        for table, rows in tables.items():
            for column in data["columns"][table]["columns"]:
                field, kind = column["id"], column["fields"]["type"]
                if kind.startswith(("Ref:", "RefList:")):
                    target = kind.split(":", 1)[1]
                    if target not in ids:
                        raise StorageError("unsupported legacy reference target")
                    for row in rows:
                        value = row["fields"].get(field)
                        if kind.startswith("RefList:"):
                            refs = self._attachment_refs(value)
                        else:
                            refs = [] if value in (0, None) else [positive_id(value)]
                        if any(ref not in ids[target] for ref in refs):
                            raise StorageError("dangling legacy reference; source preserved")
                if kind == "Attachments":
                    if table != "Records" or field != "PreviewImage":
                        raise StorageError("unsupported attachment relation column")
                    for row in rows:
                        value = row["fields"].get(field)
                        if isinstance(value, str) and value:
                            anomalies.append((table, row["id"], field,
                                              "legacy non-reference text; preserved, not an attachment relation", canonical(value)))
                            refs = []
                        else:
                            refs = self._attachment_refs(value)
                        if any(ref not in attachment_ids for ref in refs):
                            raise StorageError("dangling attachment reference; complete export first")
                        relations[row["id"]] = refs
        # Required operational reference must be checked even if column metadata
        # was malformed/omitted. Metadata itself is preserved byte-for-byte.
        for row in tables["Records"]:
            if row["fields"]["Publish"] not in ids["Publish"]:
                raise StorageError("dangling Publish reference")
            if row["id"] not in relations:
                raise StorageError("PreviewImage attachment schema is missing")
        return {"tables": tables, "attachments": attachments, "relations": relations, "anomalies": anomalies}

    @staticmethod
    def _attachment_refs(value):
        if value is None or value == "":
            return []
        if not isinstance(value, list) or not value or value[0] != "L":
            raise StorageError("malformed reference list; source preserved")
        return [positive_id(ref) for ref in value[1:]]

    def _verify_legacy(self, conn, data, prepared, raw):
        saved = conn.execute("SELECT raw_export,sha256 FROM imports WHERE id=1").fetchone()
        if not saved or saved[0] != raw or saved[1] != sha256(raw):
            raise StorageError("raw migration archive mismatch")
        total = sum(len(rows) for rows in prepared["tables"].values())
        if conn.execute("SELECT count(*) FROM legacy_rows").fetchone()[0] != total:
            raise StorageError("legacy archive row count mismatch")
        for table, rows in prepared["tables"].items():
            for row in rows:
                archived = conn.execute("SELECT fields_json FROM legacy_rows WHERE table_name=? AND row_id=?", (table, row["id"])).fetchone()
                if not archived or archived[0] != canonical(row["fields"]):
                    raise StorageError("legacy raw fields mismatch")
                if table == "Publish":
                    saved = conn.execute("SELECT * FROM publish WHERE id=?", (row["id"],)).fetchone()
                    f = row["fields"]
                    if not saved or saved["phase"] != "Migrated" or saved["source_phase"] != f["Phase"] or saved["begin_json"] != canonical(f) or saved["cycle_id"] != f["CycleID"] or saved["legacy"] != 1:
                        raise StorageError("legacy Publish provenance mismatch")
                else:
                    saved = conn.execute("SELECT * FROM records WHERE id=?", (row["id"],)).fetchone()
                    f = row["fields"]
                    if not saved or saved["fields_json"] != canonical(f) or saved["publish_id"] != f["Publish"] or saved["candidate_id"] != f["CandidateID"] or saved["identity_json"] != canonical(source_identity(f)):
                        raise StorageError("legacy Records fields/identity mismatch")
                    refs = [r[0] for r in conn.execute("SELECT attachment_id FROM record_attachments WHERE record_id=? ORDER BY position", (row["id"],))]
                    if refs != prepared["relations"].get(row["id"], []):
                        raise StorageError("legacy attachment relations mismatch")
        if conn.execute("SELECT count(*) FROM attachments").fetchone()[0] != len(prepared["attachments"]):
            raise StorageError("legacy attachment count mismatch")
        for attachment, content in prepared["attachments"]:
            saved = conn.execute("SELECT * FROM attachments WHERE id=?", (attachment["id"],)).fetchone()
            if not saved or saved["metadata_json"] != canonical(attachment["metadata"]) or saved["sha256"] != sha256(content) or saved["size"] != len(content) or read_bytes(self.root / relative_name(saved["path"])) != content:
                raise StorageError("legacy attachment metadata/bytes mismatch")
        anomalies = [tuple(r) for r in conn.execute("SELECT * FROM import_anomalies ORDER BY table_name,row_id,field_name")]
        if anomalies != sorted(prepared["anomalies"]):
            raise StorageError("legacy anomaly archive mismatch")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise StorageError("foreign key verification failed")

    def verify_legacy(self, export_path):
        path = safe_path(export_path)
        raw = read_bytes(path)
        data = read_json(path)
        if read_bytes(path) != raw:
            raise StorageError("migration export changed during verification")
        prepared = self._prepare_legacy(data, path.parent)
        with self.connection() as conn:
            conn.execute("BEGIN")
            self._verify_legacy(conn, data, prepared, raw)
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise StorageError("SQLite integrity check failed")
        return {"verified": True, "publish": len(prepared["tables"]["Publish"]),
                "records": len(prepared["tables"]["Records"]), "attachments": len(prepared["attachments"]),
                "attachment_relations": sum(map(len, prepared["relations"].values())),
                "archived_anomalies": len(prepared["anomalies"]), "raw_export_bytes_equal": True,
                "all_fields_ids_relations_metadata_and_file_bytes_equal": True}

    def export_legacy(self, destination):
        with self.connection() as conn:
            row = conn.execute("SELECT raw_export FROM imports WHERE id=1").fetchone()
            if row is None:
                raise StorageError("no legacy archive")
            atomic_write(destination, row[0], immutable=True)

    def freeze_publication(self, cycle_id, publication, assets, validation):
        """Persist final assembly and durable assets atomically before cleanup.

        validation is the integration validator's hash-bound receipt, not a
        replacement for semantic agents or the release schema validator.
        """
        if (not isinstance(publication, dict) or publication.get("cycle_id") != cycle_id
                or type(publication.get("schema_version")) is not int or publication["schema_version"] != 3):
            raise StorageError("publication must have schema_version 3 and matching cycle_id")
        text(publication.get("display_date"), "display_date")
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", publication["display_date"]):
                raise ValueError
            datetime.strptime(publication["display_date"], "%Y-%m-%d")
        except ValueError:
            raise StorageError("display_date must be YYYY-MM-DD") from None
        records = publication.get("records")
        if not isinstance(records, list) or not records:
            raise StorageError("publication must have nonempty records")
        if len(records) > 20:
            raise StorageError("new publication total quota exceeds 20")
        categories = {"Paper": 0, "Policy": 0, "News": 0}
        candidates = set()
        for record in records:
            if not isinstance(record, dict):
                raise StorageError("publication record must be an object")
            category = record.get("category")
            if not isinstance(category, str) or category not in categories:
                raise StorageError("new publication category must be Paper, Policy or News")
            categories[category] += 1
            candidate = text(record.get("candidate_id"), "candidate_id")
            if candidate in candidates:
                raise StorageError("duplicate publication candidate_id")
            candidates.add(candidate)
            text(record.get("title"), "title")
            sources = record.get("source_identities")
            if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
                raise StorageError("source_identities must be a list of nonempty strings")
            for key in ("canonical_url", "normalized_arxiv_id"):
                if record.get(key) is not None and not isinstance(record[key], str):
                    raise StorageError("nullable source identity must be text")
        if categories["Paper"] > 10 or categories["Policy"] > 3 or categories["News"] + categories["Policy"] > 10:
            raise StorageError("new publication exceeds Paper/Policy/News+Policy quotas")
        if not isinstance(validation, dict) or validation.get("publication_sha256") != digest(publication):
            raise StorageError("validation must be bound to exact publication digest")
        checks = validation.get("checks", {})
        if not isinstance(checks, dict) or set(checks) != VALIDATION_CHECKS or any(v is not True for v in checks.values()):
            raise StorageError("all required publication validations must be confirmed")
        if not isinstance(assets, dict):
            raise StorageError("assets must map logical relative names to files")
        blobs = {}
        content_by_name = {}
        for name, path in assets.items():
            relative_name(name)
            content = read_bytes(path)
            checksum = sha256(content)
            blobs[name] = {"sha256": checksum, "size": len(content), "path": f"assets/{checksum[:2]}/{checksum}"}
            content_by_name[name] = content
        frozen = digest({"publication": publication, "assets": blobs, "validation": validation})
        with self.connection(write=True) as conn:
            row = self._cycle(conn, cycle_id)
            if row["legacy"]:
                raise StorageError("historical cycles cannot be republished or mutated")
            if row["frozen_sha256"]:
                if row["frozen_sha256"] != frozen:
                    raise StorageError("frozen cycle retry changed publication/assets/validation")
                self._export_publication(conn, row)
                return frozen
            if row["phase"] != "PHASE_release":
                raise StorageError("cycle is not in PHASE_release")
            begin = json.loads(row["begin_json"])
            if "display_date" in begin and begin["display_date"] != publication["display_date"]:
                raise StorageError("publication display_date differs from begin metadata")
            timestamp = now()
            for record in records:
                identity = {key: record.get(key) for key in ("title", "canonical_url", "normalized_arxiv_id", "source_identities")}
                identity["updated"] = timestamp
                conn.execute("INSERT INTO records(publish_id,candidate_id,fields_json,identity_json) VALUES (?,?,?,?)",
                             (row["id"], record["candidate_id"], canonical(record), canonical(identity)))
            for name, content in content_by_name.items():
                blob = self._blob(content)
                conn.execute("INSERT INTO publication_assets VALUES (?,?,?,?,?)", (row["id"], name, blob["sha256"], blob["size"], blob["path"]))
            conn.execute("UPDATE publish SET publication_json=?,validation_json=?,frozen_sha256=?,updated=? WHERE id=?",
                         (canonical(publication), canonical(validation), frozen, timestamp, row["id"]))
        return frozen

    def _export_publication(self, conn, row):
        if not row["frozen_sha256"]:
            raise StorageError("cycle has no frozen publication")
        assets = {r["name"]: {key: r[key] for key in ("sha256", "size", "path")} for r in conn.execute(
            "SELECT * FROM publication_assets WHERE publish_id=? ORDER BY name", (row["id"],))}
        for blob in assets.values():
            content = read_bytes(self.root / relative_name(blob["path"]))
            if len(content) != blob["size"] or sha256(content) != blob["sha256"]:
                raise StorageError("durable publication asset changed or missing")
        result = {"publication": json.loads(row["publication_json"]), "assets": assets,
                  "validation": json.loads(row["validation_json"])}
        if digest(result) != row["frozen_sha256"]:
            raise StorageError("frozen publication digest mismatch")
        return {**result, "frozen_sha256": row["frozen_sha256"]}

    def export_publication(self, cycle_id, destination=None):
        with self.connection() as conn:
            conn.execute("BEGIN")
            result = self._export_publication(conn, self._cycle(conn, cycle_id))
        if destination is not None:
            atomic_write(destination, (canonical(result) + "\n").encode(), immutable=True)
        return result

    def prior_identity(self, cycle_id, run_id, previous_cycle_id=None):
        """Exact coordinator snapshot whitelist; decisions remain semantic work.

        Latest earlier row in stable Publish ID order, not mutable update time.
        Migrated history and Released new cycles qualify; never Selected-filtered.
        """
        text(run_id, "run_id")
        with self.connection(write=True) as conn:
            current = self._cycle(conn, cycle_id)
            cached = conn.execute("SELECT snapshot_json FROM prior_snapshots WHERE publish_id=? AND run_id=?", (current["id"], run_id)).fetchone()
            if cached:
                result = json.loads(cached[0])
                previous = result["snapshot"]["previous_publish"]
                if previous_cycle_id is not None and (previous is None or previous["cycle_id"] != previous_cycle_id):
                    raise StorageError("frozen prior identity retry changed previous cycle")
                return result
            if previous_cycle_id is not None:
                previous = self._cycle(conn, previous_cycle_id)
                if previous["id"] >= current["id"] or previous["phase"] not in ("Migrated", "Released"):
                    raise StorageError("explicit previous cycle is not eligible history")
            else:
                previous = conn.execute("SELECT * FROM publish WHERE id<? AND phase IN ('Migrated','Released') ORDER BY id DESC LIMIT 1", (current["id"],)).fetchone()
            snapshot = {"first_cycle": previous is None, "previous_publish": None,
                        "records": [], "before": None, "after": None}
            if previous is not None:
                identity = {"row_id": previous["id"], "cycle_id": previous["cycle_id"],
                            "phase": previous["phase"], "updated": previous["updated"]}
                records = []
                for row in conn.execute("SELECT * FROM records WHERE publish_id=? ORDER BY id", (previous["id"],)):
                    records.append({"row_id": row["id"], "candidate_id": row["candidate_id"],
                                    "publish_row_id": previous["id"], **json.loads(row["identity_json"])})
                stable = {"publish": identity, "records": [
                    {key: r[key] for key in ("row_id", "candidate_id", "updated")} for r in records]}
                snapshot.update(previous_publish=identity, records=records, before=stable, after=stable)
            result = {"schema_version": 3, "cycle_id": cycle_id, "run_id": run_id,
                      "snapshot": snapshot, "decisions": []}
            conn.execute("INSERT INTO prior_snapshots VALUES (?,?,?)", (current["id"], run_id, canonical(result)))
            return result

    def start_delivery(self, cycle_id, channel, attempt_id, request):
        """Write intent BEFORE side effect. Pending/unknown forbids new attempts."""
        text(attempt_id, "attempt_id")
        if channel not in ("deployment", "notification") or not isinstance(request, dict):
            raise StorageError("invalid delivery channel/request")
        payload = canonical(request)
        with self.connection(write=True) as conn:
            row = self._cycle(conn, cycle_id)
            self._export_publication(conn, row)
            if channel == "notification" and row["phase"] != "Released":
                raise StorageError("notification requires formal Released publication")
            if request.get("frozen_sha256") != row["frozen_sha256"]:
                raise StorageError("delivery request must bind frozen_sha256")
            key = (row["id"], channel, attempt_id)
            existing = conn.execute("SELECT * FROM deliveries WHERE publish_id=? AND channel=? AND attempt_id=?", key).fetchone()
            if existing:
                if existing["request_json"] != payload:
                    raise StorageError("delivery attempt input changed")
                return {**dict(existing), "created_now": False}
            if row["phase"] == "Released" and channel == "deployment":
                raise StorageError("Released deployment cannot be repeated")
            if conn.execute("SELECT 1 FROM deliveries WHERE publish_id=? AND channel=? AND state!='failed'", key[:2]).fetchone():
                raise StorageError("prior delivery pending/unknown/confirmed; reconcile, do not repeat side effect")
            timestamp = now()
            conn.execute("INSERT INTO deliveries VALUES (?,?,?,?,'pending',NULL,?,?)", (*key, payload, timestamp, timestamp))
            conn.execute("INSERT INTO delivery_events(publish_id,channel,attempt_id,state,created) VALUES (?,?,?,'pending',?)", (*key, timestamp))
            return {**dict(conn.execute("SELECT * FROM deliveries WHERE publish_id=? AND channel=? AND attempt_id=?", key).fetchone()), "created_now": True}

    def delivery_history(self, cycle_id, channel=None):
        """Return immutable intent plus ordered transition evidence for recovery."""
        if channel is not None and channel not in ("deployment", "notification"):
            raise StorageError("invalid delivery channel")
        with self.connection() as conn:
            conn.execute("BEGIN")
            row = self._cycle(conn, cycle_id)
            result = []
            for attempt in conn.execute("SELECT * FROM deliveries WHERE publish_id=? ORDER BY created,channel,attempt_id", (row["id"],)):
                if channel is not None and attempt["channel"] != channel:
                    continue
                result.append({"channel": attempt["channel"], "attempt_id": attempt["attempt_id"],
                               "state": attempt["state"], "request": json.loads(attempt["request_json"]),
                               "evidence": json.loads(attempt["evidence_json"]) if attempt["evidence_json"] else None,
                               "events": [{"state": e["state"], "created": e["created"],
                                           "evidence": json.loads(e["evidence_json"]) if e["evidence_json"] else None}
                                          for e in conn.execute("SELECT * FROM delivery_events WHERE publish_id=? AND channel=? AND attempt_id=? ORDER BY id",
                                                                (row["id"], attempt["channel"], attempt["attempt_id"]))]})
            return result

    @staticmethod
    def _deployment_evidence(evidence, frozen):
        if evidence.get("frozen_sha256") != frozen or any(evidence.get(k) is not True for k in ("local_verified", "push_verified", "public_verified")):
            raise StorageError("deployment confirmation requires exact frozen hash and local/push/public verification")
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", evidence.get("commit_sha", "")) or not re.fullmatch(r"[0-9a-f]{64}", evidence.get("artifact_sha256", "")):
            raise StorageError("deployment confirmation requires commit and public artifact hashes")

    def record_delivery(self, cycle_id, channel, attempt_id, state, evidence):
        if state not in ("unknown", "failed", "confirmed") or not isinstance(evidence, dict):
            raise StorageError("invalid delivery result")
        text(evidence.get("reason"), "delivery evidence reason")
        payload = canonical(evidence)
        with self.connection(write=True) as conn:
            row = self._cycle(conn, cycle_id)
            key = (row["id"], channel, attempt_id)
            old = conn.execute("SELECT * FROM deliveries WHERE publish_id=? AND channel=? AND attempt_id=?", key).fetchone()
            if not old:
                raise StorageError("record delivery intent before side effect/result")
            if old["state"] == state and old["evidence_json"] == payload:
                return state
            if old["state"] in ("failed", "confirmed"):
                raise StorageError("terminal delivery result cannot change")
            if evidence.get("frozen_sha256") != row["frozen_sha256"]:
                raise StorageError("delivery evidence must bind frozen_sha256")
            if state == "failed" and evidence.get("side_effect_absent") is not True:
                raise StorageError("failed delivery requires verified side_effect_absent=true before retry")
            if state == "confirmed" and channel == "deployment":
                self._deployment_evidence(evidence, row["frozen_sha256"])
                request = json.loads(old["request_json"])
                for field in ("artifact_sha256", "commit_sha"):
                    if field in request and evidence.get(field) != request[field]:
                        raise StorageError("deployment result differs from intended artifact/commit")
            if state == "confirmed" and channel == "notification":
                if evidence.get("accepted") is not True:
                    raise StorageError("notification confirmation requires accepted=true")
            timestamp = now()
            conn.execute("UPDATE deliveries SET state=?,evidence_json=?,updated=? WHERE publish_id=? AND channel=? AND attempt_id=?", (state, payload, timestamp, *key))
            conn.execute("INSERT INTO delivery_events(publish_id,channel,attempt_id,state,evidence_json,created) VALUES (?,?,?,?,?,?)", (*key, state, payload, timestamp))
        return state

    def release(self, cycle_id):
        """No release without immutable assets plus confirmed formal deployment."""
        with self.connection(write=True) as conn:
            row = self._cycle(conn, cycle_id)
            self._export_publication(conn, row)
            receipt = conn.execute("SELECT evidence_json FROM deliveries WHERE publish_id=? AND channel='deployment' AND state='confirmed'", (row["id"],)).fetchone()
            if not receipt:
                raise StorageError("release requires confirmed deployment")
            self._deployment_evidence(json.loads(receipt[0]), row["frozen_sha256"])
            if row["phase"] == "Released":
                return "Released"
            if row["legacy"] or row["phase"] != "PHASE_release":
                raise StorageError("cycle cannot transition to Released")
            conn.execute("UPDATE publish SET phase='Released',updated=? WHERE id=?", (now(), row["id"]))
        return "Released"

    def backup(self, destination):
        """SQLite online backup plus all referenced immutable assets, atomic directory.

        Destination must not exist. Interrupted hidden staging directories are
        retained for diagnosis; no source/private data is removed.
        """
        destination = safe_path(destination)
        if destination.exists() or destination == self.root or self.root in destination.parents:
            raise StorageError("backup destination must be new and outside storage")
        private_dir(destination.parent)
        staging = Path(tempfile.mkdtemp(prefix=".backup-", dir=destination.parent))
        database = staging / "storage.sqlite3"
        with self.connection() as source, sqlite3.connect(database) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
        os.chmod(database, 0o600)
        files = {"storage.sqlite3": {"sha256": sha256(read_bytes(database)), "size": database.stat().st_size}}
        with sqlite3.connect(database) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageError("backup database integrity check failed")
            for path, checksum, size in conn.execute("SELECT path,sha256,size FROM attachments UNION SELECT path,sha256,size FROM publication_assets"):
                relative_name(path)
                content = read_bytes(self.root / path)
                if sha256(content) != checksum or len(content) != size:
                    raise StorageError("backup asset verification failed")
                atomic_write(staging / path, content, immutable=True)
                files[path] = {"sha256": checksum, "size": size}
        atomic_write(staging / "manifest.json", (canonical({"schema_version": VERSION, "files": files}) + "\n").encode(), immutable=True)
        with open(database, "rb") as stream:
            os.fsync(stream.fileno())
        sync_dir(staging)
        if destination.exists():
            raise StorageError("backup destination appeared during write")
        os.rename(staging, destination)
        sync_dir(destination.parent)
        return {"verified": True, "files": len(files), "schema_version": VERSION}

    @classmethod
    def restore(cls, backup_dir, destination):
        """Restore a verified bundle into a new directory; never replace live data."""
        source, target = safe_path(backup_dir), safe_path(destination)
        if target.exists():
            raise StorageError("restore destination must not exist")
        manifest = read_json(source / "manifest.json")
        if manifest.get("schema_version") not in (1, 2, VERSION) or "storage.sqlite3" not in manifest.get("files", {}):
            raise StorageError("invalid backup manifest")
        private_dir(target.parent)
        staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=target.parent))
        for name, info in manifest["files"].items():
            relative_name(name)
            if name != "storage.sqlite3" and not re.fullmatch(r"assets/[0-9a-f]{2}/[0-9a-f]{64}", name):
                raise StorageError("unexpected backup file")
            content = read_bytes(source / name)
            if sha256(content) != info["sha256"] or len(content) != info["size"]:
                raise StorageError("backup file checksum mismatch")
            atomic_write(staging / name, content, immutable=True)
        with sqlite3.connect(staging / "storage.sqlite3") as conn:
            if conn.execute("PRAGMA user_version").fetchone()[0] != manifest["schema_version"] or conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or conn.execute("PRAGMA foreign_key_check").fetchall():
                raise StorageError("restore database verification failed")
            for path, checksum, size in conn.execute("SELECT path,sha256,size FROM attachments UNION SELECT path,sha256,size FROM publication_assets"):
                if manifest["files"].get(path) != {"sha256": checksum, "size": size}:
                    raise StorageError("backup manifest omits/mismatches referenced asset")
        if target.exists():
            raise StorageError("restore destination appeared during write")
        sync_dir(staging)
        os.rename(staging, target)
        sync_dir(target.parent)
        return cls(target)
