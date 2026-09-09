"""Offline/local CLI. Sensitive payloads are written to private files, not stdout."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

from colab_daily.config import Config
from .files import atomic_write, canonical, read_json, safe_path
from .store import Store, StorageError


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--storage-dir", type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    status = commands.add_parser("status")
    status.add_argument("--cycle")
    status.add_argument("--output", type=Path)
    for name in ("import-legacy", "verify-legacy"):
        command = commands.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
    archive = commands.add_parser("export-legacy")
    archive.add_argument("--output", type=Path, required=True)
    begin = commands.add_parser("begin-cycle")
    begin.add_argument("--cycle", required=True)
    begin.add_argument("--metadata", type=Path, required=True)
    freeze = commands.add_parser("freeze-publication")
    freeze.add_argument("--cycle", required=True)
    freeze.add_argument("--publication", type=Path, required=True)
    freeze.add_argument("--assets", type=Path, required=True, help="JSON mapping logical name to project-relative file")
    freeze.add_argument("--validation", type=Path, required=True)
    export = commands.add_parser("export-publication")
    export.add_argument("--cycle", required=True)
    export.add_argument("--output", type=Path, required=True)
    prior = commands.add_parser("prior-identity")
    prior.add_argument("--cycle", required=True)
    prior.add_argument("--run", required=True)
    prior.add_argument("--previous-cycle")
    prior.add_argument("--output", type=Path, required=True)
    for name in ("start-delivery", "record-delivery"):
        command = commands.add_parser(name)
        command.add_argument("--cycle", required=True)
        command.add_argument("--channel", choices=("deployment", "notification"), required=True)
        command.add_argument("--attempt", required=True)
        command.add_argument("--input", type=Path, required=True)
        if name == "record-delivery":
            command.add_argument("--state", choices=("unknown", "failed", "confirmed"), required=True)
        else:
            command.add_argument("--output", type=Path, required=True, help="read state before any side effect; retries return existing intent")
    claim = commands.add_parser("claim-run")
    claim.add_argument("--cycle", required=True)
    claim.add_argument("--run", required=True)
    claim.add_argument("--kind", required=True)
    claim.add_argument("--owner", required=True)
    claim.add_argument("--context", type=Path, required=True)
    claim.add_argument("--output", type=Path, required=True)
    checkpoint = commands.add_parser("checkpoint-stage")
    checkpoint.add_argument("--run", required=True)
    checkpoint.add_argument("--owner", required=True)
    checkpoint.add_argument("--stage", required=True)
    checkpoint.add_argument("--operation", required=True)
    checkpoint.add_argument("--revision", type=int, required=True)
    checkpoint.add_argument("--status", choices=("running", "failed", "complete"), required=True)
    checkpoint.add_argument("--input", type=Path, required=True)
    run_state = commands.add_parser("run-state")
    run_state.add_argument("--run", required=True)
    run_state.add_argument("--output", type=Path, required=True)
    source_state = commands.add_parser("source-state")
    source_state.add_argument("--source", required=True)
    source_state.add_argument("--output", type=Path, required=True)
    source_update = commands.add_parser("update-source-state")
    source_update.add_argument("--source", required=True)
    source_update.add_argument("--operation", required=True)
    source_update.add_argument("--revision", type=int, required=True)
    source_update.add_argument("--input", type=Path, required=True)
    history = commands.add_parser("delivery-history")
    history.add_argument("--cycle", required=True)
    history.add_argument("--channel", choices=("deployment", "notification"))
    history.add_argument("--output", type=Path, required=True)
    release = commands.add_parser("release")
    release.add_argument("--cycle", required=True)
    backup = commands.add_parser("backup")
    backup.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--input", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = Config.load(args.project_root)

        def path(value):
            value = Path(value)
            return safe_path(value if value.is_absolute() else config.project_root / value)

        def load(value):
            return read_json(path(value))

        def output(value, result):
            atomic_write(path(value), (canonical(result) + "\n").encode())

        if args.command == "restore":
            restored = Store.restore(path(args.input), path(args.output))
            result = restored.status()
        else:
            store = Store(path(args.storage_dir) if args.storage_dir else config.storage_dir)
            cmd = args.command
            result = {"ok": True}
            if cmd == "init":
                result = store.status()
            elif cmd == "status":
                result = store.status(args.cycle)
                if args.output:
                    output(args.output, result)
                    result = {"ok": True}
                elif args.cycle:
                    result = {"phase": result["phase"], "frozen": result["frozen_sha256"] is not None}
            elif cmd == "import-legacy":
                result = store.import_legacy(path(args.input))
            elif cmd == "verify-legacy":
                result = store.verify_legacy(path(args.input))
            elif cmd == "export-legacy":
                store.export_legacy(path(args.output))
            elif cmd == "begin-cycle":
                store.begin_cycle(args.cycle, load(args.metadata))
            elif cmd == "freeze-publication":
                assets = {name: path(value) for name, value in load(args.assets).items()}
                store.freeze_publication(args.cycle, load(args.publication), assets, load(args.validation))
            elif cmd == "export-publication":
                store.export_publication(args.cycle, path(args.output))
            elif cmd == "prior-identity":
                output(args.output, store.prior_identity(args.cycle, args.run, args.previous_cycle))
            elif cmd == "start-delivery":
                output(args.output, store.start_delivery(args.cycle, args.channel, args.attempt, load(args.input)))
            elif cmd == "record-delivery":
                store.record_delivery(args.cycle, args.channel, args.attempt, args.state, load(args.input))
            elif cmd == "claim-run":
                output(args.output, store.claim_run(args.cycle, args.run, args.kind, args.owner, load(args.context)))
            elif cmd == "checkpoint-stage":
                result = {"revision": store.checkpoint_stage(args.run, args.owner, args.stage, args.operation,
                                                             args.revision, args.status, load(args.input))}
            elif cmd == "run-state":
                output(args.output, store.run_state(args.run))
            elif cmd == "source-state":
                output(args.output, store.source_state(args.source))
            elif cmd == "update-source-state":
                update = load(args.input)
                result = {"revision": store.update_source_state(args.source, args.operation, args.revision,
                                                                update["cursor"], update["events"])}
            elif cmd == "delivery-history":
                output(args.output, store.delivery_history(args.cycle, args.channel))
            elif cmd == "release":
                result = {"phase": store.release(args.cycle)}
            elif cmd == "backup":
                result = store.backup(path(args.output))
        print(json.dumps(result, sort_keys=True))
        return 0
    except StorageError as exc:
        # StorageError messages contain only generic schema/state diagnostics.
        print(f"storage: {exc}", file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        # Never echo arbitrary JSON, private paths, IDs, or SQLite data in errors.
        print("storage: invalid input or local I/O/database failure; inspect private source files and permissions", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
