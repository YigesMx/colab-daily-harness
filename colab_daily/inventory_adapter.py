"""Inventory/discovery documents stay temporary; SQLite stores bounded receipts."""
from .config import Config
from .lifecycle import Lifecycle
from .storage import StorageError
from .storage.files import read_json
from .adapters import artifact_receipt, verify_artifacts


def inventory_command(args, execute):
    life = Lifecycle(Config.load())
    with life.use(args.owner, args.cycle_id) as state:
        if life.workspace_path(args.crawl_root) != life.working:
            raise StorageError("inventory must consume the single owned workspace")
        args.crawl_root = life.working
        args.source_receipt = life.workspace_path(args.source_receipt)
        args.output = life.workspace_path(args.output)
        run_id = args.run_id + ":inventory"
        context = {"paths": [p.relative_to(life.working).as_posix() for p in (args.source_receipt, args.output)]}
        life.store.claim_run(state["cycle_id"], run_id, "source-inventory", args.owner, context)
        previous = life.store.run_state(run_id)["stages"].get("inventory")
        if previous and previous["status"] == "complete":
            verify_artifacts(life, previous["payload"])
            return 0
        revision = previous["revision"] if previous else 0
        receipt = read_json(args.source_receipt)
        revision = life.store.checkpoint_stage(run_id, args.owner, "inventory", f"start:{revision}", revision,
                                              "running", {"paths": context["paths"]})
        try:
            code = execute(args)
            if code != 0:
                raise StorageError("inventory command did not complete")
            inventory = read_json(args.output)
            metadata = artifact_receipt(life, [args.source_receipt, args.output],
                                        counts={"sources": len(receipt.get("sources", [])), "records": len(inventory.get("records", []))})
        except Exception:
            life.store.checkpoint_stage(run_id, args.owner, "inventory", f"result:{revision}", revision,
                                        "failed", {"paths": context["paths"], "reason": "inventory validation failed"})
            raise
        life.store.checkpoint_stage(run_id, args.owner, "inventory", f"result:{revision}", revision, "complete", metadata)
        return code
