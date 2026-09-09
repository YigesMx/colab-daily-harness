"""SQLite-authoritative daily lifecycle and the single project working_tmp workspace."""
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
from zoneinfo import ZoneInfo

from .config import Config
from .storage import Store, StorageError, digest
from .storage.metadata import control_metadata
from .storage.files import atomic_write, canonical, private_dir, read_bytes, read_json, relative_name, safe_path, sha256

SHANGHAI = ZoneInfo("Asia/Shanghai")


def aware(value):
    value = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("an offset-aware timestamp is required")
    return value


def daily_window(display_date):
    if not isinstance(display_date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", display_date):
        raise StorageError("display date must be YYYY-MM-DD")
    day = date.fromisoformat(display_date)
    until = datetime.combine(day, time(), SHANGHAI)
    return {"display_date": display_date, "timezone": "Asia/Shanghai", "selection_limit": 20,
            "window_since": (until - timedelta(days=1)).isoformat(), "window_until": until.isoformat()}


def tree_receipt(directory):
    """Bounded checksum/count metadata. Intermediate bytes stay on disk only."""
    import hashlib
    directory = safe_path(directory)
    checksum = hashlib.sha256()
    count = total = 0
    if directory.exists():
        for path in sorted(directory.rglob("*")):
            safe_path(path)
            if path.is_file() and not path.name.endswith(".lock"):
                file_hash = hashlib.sha256()
                with path.open("rb") as stream:
                    while chunk := stream.read(65536):
                        file_hash.update(chunk)
                        total += len(chunk)
                name = path.relative_to(directory).as_posix().encode()
                checksum.update(str(len(name)).encode() + b":" + name + file_hash.digest())
                count += 1
    return {"sha256": checksum.hexdigest(), "files": count, "bytes": total}


def verify_tree(directory, receipt):
    if not Path(directory).is_dir() or tree_receipt(directory) != receipt:
        raise StorageError("temporary artifacts missing or changed; no database content mirror exists")


class Lifecycle:
    def __init__(self, config=None):
        self.config = config or Config.load()
        self.root = safe_path(self.config.project_root)
        self.working = safe_path(self.config.working_dir)
        if self.working != self.root / "working_tmp":
            raise StorageError("production workspace must be project-root working_tmp")
        storage = safe_path(self.config.storage_dir)
        site = safe_path(self.config.site_dir)
        for path in (storage, safe_path(self.config.backup_dir), site):
            if path == self.working or self.working in path.parents or path in self.working.parents:
                raise StorageError("durable state/site and temporary workspace must not overlap")
        self.store = Store(storage)
        self.lock_path = private_dir(self.root / "state") / "workspace.lock"

    @contextmanager
    def lock(self):
        # This stable file is coordination only. All owner/context facts are in DB.
        safe_path(self.lock_path)
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    @staticmethod
    def _view(cycle, claim):
        return {"cycle_id": cycle["cycle_id"], "publish_id": cycle["id"], "phase": cycle["phase"],
                "owner": claim["owner"], "workspace_state": claim["state"],
                "input": json.loads(cycle["begin_json"]), "context": json.loads(claim["context_json"])}

    def begin(self, trigger, owner, display_date=None, cycle_id=None, since=None, until=None, context=None, now=None):
        if trigger not in ("scheduled", "manual", "retry") or not isinstance(owner, str) or not owner.strip():
            raise StorageError("valid trigger and nonempty owner required")
        if context is not None:
            control_metadata(context)
        if (since is None) != (until is None):
            raise StorageError("both explicit window endpoints are required")
        with self.lock(), self.store.connection(write=True) as conn:
            active = conn.execute("SELECT * FROM workspace_claims WHERE state!='closed'").fetchone()
            if trigger == "retry":
                if cycle_id is None and active is not None and active["owner"] == owner:
                    cycle_id = conn.execute("SELECT cycle_id FROM publish WHERE id=?", (active["publish_id"],)).fetchone()[0]
                if cycle_id is None:
                    raise StorageError("retry requires a cycle or its unique existing owner")
                cycle = self.store._cycle(conn, cycle_id)
                frozen = json.loads(cycle["begin_json"])
                if display_date is not None and display_date != frozen.get("display_date"):
                    raise StorageError("retry display date changed")
                display_date = frozen.get("display_date")
                observed = None
            else:
                observed = aware(now or datetime.now(timezone.utc)) if trigger == "scheduled" else None
                if trigger == "scheduled":
                    actual_day = observed.astimezone(SHANGHAI).date().isoformat()
                    if display_date is not None and display_date != actual_day:
                        raise StorageError("scheduled date differs from observed Shanghai day")
                    display_date = actual_day
                elif display_date is None:
                    raise StorageError("manual start requires an explicit display date")
            expected = daily_window(display_date)
            expected_id = "daily-" + display_date
            if cycle_id is not None and cycle_id != expected_id:
                raise StorageError("cycle identity differs from frozen daily date")
            cycle_id = expected_id
            if since is not None and (aware(since) != aware(expected["window_since"]) or aware(until) != aware(expected["window_until"])):
                raise StorageError("explicit window must equal the previous complete Shanghai day")
            if active is not None:
                bound = conn.execute("SELECT cycle_id FROM publish WHERE id=?", (active["publish_id"],)).fetchone()[0]
                if active["owner"] != owner or bound != cycle_id or active["path"] != str(self.working):
                    raise StorageError("single workspace is owned by another cycle/context; no time-based takeover")
            cycle = conn.execute("SELECT * FROM publish WHERE cycle_id=?", (cycle_id,)).fetchone()
            if cycle is not None:
                claim = conn.execute("SELECT * FROM workspace_claims WHERE publish_id=?", (cycle["id"],)).fetchone()
                saved = json.loads(cycle["begin_json"])
                if cycle["legacy"] or claim is None:
                    raise StorageError("existing cycle has no matching lifecycle ownership")
                if any(saved.get(k) != v for k, v in expected.items()) or claim["owner"] != owner or claim["path"] != str(self.working):
                    raise StorageError("existing lifecycle input/owner changed")
                if context is not None and claim["context_json"] != canonical(context):
                    raise StorageError("immutable lifecycle context changed")
            else:
                if trigger == "retry":
                    raise StorageError("retry cannot create a cycle")
                if conn.execute("SELECT 1 FROM workspace_claims WHERE owner=?", (owner,)).fetchone():
                    raise StorageError("owner tokens cannot be reused across cycles")
                safe_path(self.working)
                if self.working.exists() and any(self.working.iterdir()):
                    raise StorageError("unowned workspace is nonempty; preserve and investigate")
                frozen = {**expected, "trigger": trigger, "observed_at": observed.isoformat() if observed else None}
                stamp = observed.isoformat() if observed is not None else datetime.now(timezone.utc).isoformat()
                conn.execute("INSERT INTO publish(cycle_id,phase,begin_json,created,updated) VALUES (?,'PHASE_release',?,?,?)", (cycle_id, canonical(frozen), stamp, stamp))
                cycle = self.store._cycle(conn, cycle_id)
                conn.execute("INSERT INTO workspace_claims VALUES (?,?,?,?, 'active',?,?)", (cycle["id"], owner, str(self.working), canonical(context or {}), stamp, stamp))
                claim = conn.execute("SELECT * FROM workspace_claims WHERE publish_id=?", (cycle["id"],)).fetchone()
            result = self._view(cycle, claim)
            if result["workspace_state"] == "active":
                private_dir(self.working)
        return result

    def require(self, owner, cycle_id=None, allow_released=False):
        with self.store.connection() as conn:
            claim = conn.execute("SELECT * FROM workspace_claims WHERE state!='closed'").fetchone()
            if claim is None or claim["owner"] != owner or claim["path"] != str(self.working):
                raise StorageError("workspace owner mismatch or no active claim")
            cycle = conn.execute("SELECT * FROM publish WHERE id=?", (claim["publish_id"],)).fetchone()
            if cycle_id is not None and cycle["cycle_id"] != cycle_id:
                raise StorageError("workspace cycle mismatch")
            if not allow_released and (claim["state"] != "active" or cycle["phase"] != "PHASE_release"):
                raise StorageError("workspace is not active for production work")
            safe_path(self.working)
            return self._view(cycle, claim)

    @contextmanager
    def use(self, owner, cycle_id=None):
        with self.lock():
            state = self.require(owner, cycle_id)
            private_dir(self.working)
            yield state

    def project_path(self, value):
        value = Path(value)
        return safe_path(value if value.is_absolute() else self.root / value)

    def workspace_path(self, value):
        path = self.project_path(value)
        if path != self.working and self.working not in path.parents:
            raise StorageError("runtime output must be within the owned working_tmp")
        return path

    def cleanup(self, owner, cycle_id):
        """Only formal Released data permits removal; cleanup intent survives crash."""
        with self.lock():
            if not cycle_id:
                raise StorageError("cleanup requires explicit cycle identity")
            with self.store.connection() as conn:
                old = conn.execute("SELECT w.* FROM workspace_claims w JOIN publish p ON p.id=w.publish_id WHERE p.cycle_id=?", (cycle_id,)).fetchone()
                if old and old["state"] == "closed" and old["owner"] == owner and old["path"] == str(self.working):
                    return {"cleanup": "complete"}
            state = self.require(owner, cycle_id, allow_released=True)
            if state["phase"] != "Released":
                raise StorageError("cleanup requires formal Released publication")
            self.store.export_publication(cycle_id)
            if self.working.exists():
                for path in self.working.rglob("*"):
                    safe_path(path)
            with self.store.connection(write=True) as conn:
                conn.execute("UPDATE workspace_claims SET state='cleaning',updated=? WHERE publish_id=?", (datetime.now(timezone.utc).isoformat(), state["publish_id"]))
            if self.working.exists():
                shutil.rmtree(self.working)
            with self.store.connection(write=True) as conn:
                conn.execute("UPDATE workspace_claims SET state='closed',updated=? WHERE publish_id=?", (datetime.now(timezone.utc).isoformat(), state["publish_id"]))
        return {"cleanup": "complete"}

    def prior(self, owner, cycle_id, run_id, output=None):
        with self.use(owner, cycle_id):
            result = self.store.prior_identity(cycle_id, run_id)
            if output:
                # Regenerate only the base snapshot; semantic decisions belong to
                # their own persisted run checkpoint and must not be overwritten.
                path = self.workspace_path(output)
                atomic_write(path, (canonical(result) + "\n").encode(), immutable=True)
            return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    sub = parser.add_subparsers(dest="command", required=True)
    begin = sub.add_parser("begin")
    begin.add_argument("--trigger", choices=("scheduled", "manual", "retry"), required=True)
    begin.add_argument("--display-date")
    begin.add_argument("--since")
    begin.add_argument("--until")
    begin.add_argument("--context", type=Path)
    for name in ("status", "prior", "claim-run", "checkpoint", "run-state", "cleanup"):
        sub.add_parser(name)
    for command in sub.choices.values():
        command.add_argument("--owner", required=True)
        command.add_argument("--cycle")
        command.add_argument("--output", type=Path)
    for name in ("prior", "claim-run", "checkpoint", "run-state"):
        sub.choices[name].add_argument("--run", required=True)
    sub.choices["claim-run"].add_argument("--kind", required=True)
    sub.choices["claim-run"].add_argument("--run-owner", help="isolated child context owner; workspace owner still authorizes the claim")
    sub.choices["claim-run"].add_argument("--context", type=Path, required=True)
    check = sub.choices["checkpoint"]
    check.add_argument("--stage", required=True)
    check.add_argument("--operation", required=True)
    check.add_argument("--revision", type=int, required=True)
    check.add_argument("--status", choices=("running", "failed", "complete"), required=True)
    check.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        life = Lifecycle(Config.load(args.project_root))
        load = lambda p: read_json(life.project_path(p))
        if args.command == "begin":
            result = life.begin(args.trigger, args.owner, args.display_date, args.cycle, args.since, args.until,
                                load(args.context) if args.context else None)
        elif args.command == "cleanup":
            result = life.cleanup(args.owner, args.cycle)
        elif args.command == "prior":
            result = life.prior(args.owner, args.cycle, args.run, args.output)
        else:
            with life.use(args.owner, args.cycle) as state:
                if args.command == "status":
                    result = state
                elif args.command == "claim-run":
                    context = load(args.context)
                    run_owner = args.run_owner or args.owner
                    if args.kind.startswith("refine:") and context.get("context_id") != run_owner:
                        raise StorageError("refine child owner must equal the isolated context_id")
                    result = life.store.claim_run(state["cycle_id"], args.run, args.kind, run_owner, context)
                else:
                    run = life.store.run_state(args.run)
                    if run["publish_id"] != state["publish_id"] or run["owner"] != args.owner:
                        raise StorageError("run does not belong to this workspace owner/cycle")
                    if args.command == "run-state":
                        result = run
                    else:
                        result = {"revision": life.store.checkpoint_stage(args.run, args.owner, args.stage, args.operation,
                                                                         args.revision, args.status, load(args.input))}
        if args.output and args.command not in ("prior", "cleanup"):
            path = life.workspace_path(args.output)
            with life.use(args.owner, result.get("cycle_id", args.cycle)):
                atomic_write(path, (canonical(result) + "\n").encode())
        print(json.dumps({"ok": True}))
        return 0
    except (ValueError, OSError, KeyError, TypeError):
        print("lifecycle: input/ownership/state check failed; inspect private inputs", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
