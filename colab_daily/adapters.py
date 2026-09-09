"""Guard runtime writers; persist only bounded control metadata, never file bytes."""
from datetime import datetime
from pathlib import Path
import os

from .config import Config
from .lifecycle import Lifecycle, aware, tree_receipt, verify_tree
from .storage import StorageError, digest
from .storage.files import atomic_write, canonical, private_dir, read_bytes, read_json, relative_name, sha256


def json_args(args):
    return {key: value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()}


def artifact_receipt(life, paths, identity=None, counts=None):
    paths = [life.workspace_path(path) for path in paths]
    if len(paths) > 64:
        raise StorageError("too many control artifacts")
    return {"identity": identity or {}, "paths": [p.relative_to(life.working).as_posix() for p in paths],
            "checksums": {str(i): sha256(read_bytes(path)) for i, path in enumerate(paths)},
            "counts": counts or {"artifacts": len(paths)}}


def verify_artifacts(life, receipt):
    for index, name in enumerate(receipt["paths"]):
        relative_name(name)
        path = life.workspace_path(life.working / name)
        if not path.is_file() or sha256(read_bytes(path)) != receipt["checksums"][str(index)]:
            raise StorageError("temporary control artifact missing/changed; recover working_tmp or explicitly rerun")


def source_command(args, execute, source):
    relative_name(source)
    if "/" in source:
        raise StorageError("source directory must be a single name")
    life = Lifecycle(Config.load(getattr(args, "project_root", None)))
    with life.use(getattr(args, "owner", None), args.cycle_id) as state:
        frozen = state["input"]
        args.cycle_id = state["cycle_id"]
        if getattr(args, "days", None) is not None:
            raise StorageError("production crawler cannot override the frozen daily window")
        if (args.since is None) != (args.until is None):
            raise StorageError("both explicit crawler window endpoints are required")
        if args.since is not None and (aware(args.since) != aware(frozen["window_since"]) or aware(args.until) != aware(frozen["window_until"])):
            raise StorageError("crawler window differs from frozen cycle")
        args.since, args.until = aware(frozen["window_since"]), aware(frozen["window_until"])
        if args.output_dir is not None and life.workspace_path(args.output_dir) != life.working:
            raise StorageError("crawler output must use the single owned workspace")
        args.output_dir = life.working
        for key in ("sources", "consensus"):
            if getattr(args, key, None) is not None:
                setattr(args, key, life.project_path(getattr(args, key)))
        if getattr(args, "dry_run", False):
            raise StorageError("production adapter does not support nonpersisted dry-run source execution")
        request = json_args(args)
        request.pop("project_root", None)
        inputs = {"consensus": getattr(args, "consensus", None) or life.root / "consensus.md"}
        script = execute.__globals__.get("__file__") if hasattr(execute, "__globals__") else None
        if getattr(args, "sources", None) is not None:
            inputs["sources"] = args.sources
        elif script:
            inputs["sources"] = Path(script).parent.parent / "sources.json"
        request["input_fingerprints"] = {key: sha256(read_bytes(path)) if Path(path).exists() else None for key, path in inputs.items()}
        run_id = state["cycle_id"] + ":source:" + source
        directory = life.workspace_path(life.working / source)
        with life.store.connection() as conn:
            known = conn.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not known and directory.exists():
            raise StorageError("source output exists without an authoritative run claim")
        context = {"identity": {"source": source, "input_sha256": digest(request)}, "paths": [source]}
        life.store.claim_run(state["cycle_id"], run_id, "source:" + source, args.owner, context)
        previous = life.store.run_state(run_id)["stages"].get("crawl")
        if previous and previous["status"] == "complete":
            payload = previous["payload"]
            verify_tree(directory, {"sha256": payload["checksums"]["tree"], **payload["counts"]})
            return payload["identity"]["return_code"]
        revision = previous["revision"] if previous else 0
        paths = [source]
        if directory.exists():
            current = tree_receipt(directory)
            if previous is None:
                raise StorageError("source output exists without a started checkpoint")
            if previous["status"] == "failed" and current["sha256"] != previous["payload"]["checksums"]["tree"]:
                raise StorageError("failed source staging changed outside its recorded attempt")
            # Preserve failed/running material in working_tmp, NOT in SQLite.
            archive = life.workspace_path(life.working / ".source-attempts" / source / str(revision))
            if archive.exists():
                raise StorageError("source retry archive already exists")
            private_dir(archive.parent)
            os.rename(directory, archive)
            paths.append(archive.relative_to(life.working).as_posix())
        revision = life.store.checkpoint_stage(run_id, args.owner, "crawl", f"start:{revision}", revision,
                                              "running", {"identity": {"source": source}, "paths": paths})
        try:
            result = execute(args)
        except Exception:
            receipt = tree_receipt(directory)
            life.store.checkpoint_stage(run_id, args.owner, "crawl", f"result:{revision}", revision, "failed",
                                        {"paths": [source], "checksums": {"tree": receipt.pop("sha256")},
                                         "counts": receipt, "reason": "source command failed"})
            raise
        manifest = directory / "crawl_manifest.json"
        complete = result == 0 and (not manifest.exists() or read_json(manifest).get("status") == "success")
        receipt = tree_receipt(directory)
        life.store.checkpoint_stage(run_id, args.owner, "crawl", f"result:{revision}", revision,
                                    "complete" if complete else "failed",
                                    {"identity": {"return_code": result}, "paths": [source],
                                     "checksums": {"tree": receipt.pop("sha256")}, "counts": receipt})
        return result if complete else (result or 1)


def rating_command(args, execute):
    life = Lifecycle(Config.load())
    run_dir = life.workspace_path(args.run_dir)
    args.run_dir = run_dir
    for key in ("paper_output", "news_output", "policy_output"):
        if getattr(args, key, None) is not None:
            setattr(args, key, life.workspace_path(getattr(args, key)))
    request_hash = digest(json_args(args))
    with life.use(args.owner) as state:
        relative = run_dir.relative_to(life.working).as_posix()
        if not relative.startswith("rating_filter_organize/runs/"):
            raise StorageError("rating run path is outside the managed rating tree")
        run_id = run_dir.name
        life.store.claim_run(state["cycle_id"], run_id, "rating-coordinator", args.owner, {"paths": [relative]})
        stages = life.store.run_state(run_id)["stages"]
        stage = "prepare:" + args.track if args.command == "prepare-track" else "assemble"
        old = stages.get(stage)
        result_path = run_dir / ".runtime" / (stage.replace(":", "-") + ".json")
        if old and old["status"] == "complete":
            if old["payload"]["identity"]["request_sha256"] != request_hash:
                raise StorageError("completed rating command input changed")
            verify_artifacts(life, old["payload"])
            verify_tree(run_dir / "shared", {"sha256": old["payload"]["checksums"]["shared"],
                        "files": old["payload"]["counts"]["shared_files"], "bytes": old["payload"]["counts"]["shared_bytes"]})
            return read_json(result_path)
        routing = read_json(run_dir / "shared/routing.json")
        if routing.get("cycle_id") != state["cycle_id"]:
            raise StorageError("rating routing belongs to another cycle")
        prior_path = run_dir / "shared/prior_cycle_identity.json"
        frozen_prior = life.store.prior_identity(state["cycle_id"], run_id)
        if prior_path.exists():
            prior = read_json(prior_path)
            if prior.get("snapshot") != frozen_prior["snapshot"] or prior.get("cycle_id") != state["cycle_id"] or prior.get("run_id") != run_id:
                raise StorageError("rating prior snapshot differs from SQLite authority")
        else:
            atomic_write(prior_path, (canonical(frozen_prior) + "\n").encode(), immutable=True)
        revision = old["revision"] if old else 0
        identity = {"request_sha256": request_hash}
        revision = life.store.checkpoint_stage(run_id, args.owner, stage, f"start:{stage}:{revision}", revision,
                                              "running", {"identity": identity, "paths": [relative]})
        try:
            result = dict(execute())
            result.pop("created", None)  # attempt-local, not immutable business output
            atomic_write(result_path, (canonical(result) + "\n").encode(), immutable=True)
            paths = [result_path, run_dir / "shared/routing.json", prior_path]
            if args.command == "prepare-track":
                paths.append(run_dir / "tracks" / args.track / "input.json")
            else:
                paths.extend([run_dir / name for name in ("grouped_selection.json", "manifest.json", "partition_manifest.json", "production_partition_report.json")])
                paths.extend(run_dir / "tracks" / track / name for track in ("paper", "news", "policy")
                             for name in ("input.json", "terminal_output.json"))
                paths.extend([life.working / "rating_filter_organize" / name for name in ("current.json", "activation_index.json")])
                for track in ("paper", "news", "policy"):
                    path = run_dir / "tracks" / track / "context.json"
                    paths.append(path)
                    context = read_json(path)
                    identity[track + "_context_id"] = context["context_id"]
            if (run_dir / "partition_manifest.json").exists() and run_dir / "partition_manifest.json" not in paths:
                paths.append(run_dir / "partition_manifest.json")
            receipt = artifact_receipt(life, paths, identity=identity)
            shared = tree_receipt(run_dir / "shared")
            receipt["checksums"]["shared"] = shared["sha256"]
            receipt["counts"].update(shared_files=shared["files"], shared_bytes=shared["bytes"])
        except Exception:
            life.store.checkpoint_stage(run_id, args.owner, stage, f"{stage}:{revision}", revision, "failed",
                                        {"identity": identity, "paths": [relative], "reason": "rating validation failed"})
            raise
        life.store.checkpoint_stage(run_id, args.owner, stage, f"{stage}:{revision}", revision, "complete", receipt)
        return result
