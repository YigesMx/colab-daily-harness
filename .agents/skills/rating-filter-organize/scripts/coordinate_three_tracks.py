#!/usr/bin/env python3
"""Coordinate three fully isolated Paper, News, and Policy production ratings."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit


SCRIPTS_DIR = Path(__file__).resolve().parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from validate_category_quota import VALID_CATEGORY_SET  # noqa: E402
from validate_grouped_candidates import validate_grouped_candidates  # noqa: E402


sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from colab_daily.adapters import rating_command
from colab_daily.config import Config

PROJECT_ROOT = Config.load().project_root
RATING_ROOT = PROJECT_ROOT / "working_tmp" / "rating_filter_organize"
RATING_SKILL_PATH = Path(__file__).resolve().parents[1] / "SKILL.md"
TRACK_CATEGORIES = {
    "paper": frozenset({"Paper"}),
    "news": frozenset({"News"}),
    "policy": frozenset({"Policy"}),
}
TRACK_CONSENSUS_PATHS = {
    "paper": PROJECT_ROOT / "consensus.md",
    "news": PROJECT_ROOT / "news_consensus.md",
    "policy": PROJECT_ROOT / "policy_consensus.md",
}
CONSENSUS_PATH = TRACK_CONSENSUS_PATHS["paper"]
NEWS_CONSENSUS_PATH = TRACK_CONSENSUS_PATHS["news"]
POLICY_CONSENSUS_PATH = TRACK_CONSENSUS_PATHS["policy"]
TRACK_PROMPT_PATHS = {
    "paper": PROJECT_ROOT / ".opencode" / "agents" / "paper-track.md",
    "news": PROJECT_ROOT / ".opencode" / "agents" / "news-track.md",
    "policy": PROJECT_ROOT / ".opencode" / "agents" / "policy-track.md",
}
TRACK_CAPACITY_FIELDS = {
    "paper": "paper_capacity",
    "news": "news_capacity",
    "policy": "policy_capacity",
}
TRACK_TARGETS = {
    "paper": 10,
    "news": 5,
    "policy": 3,
}
TRACK_RESERVE_CAPACITIES = {
    "paper": 10,
    "news": 10,
    "policy": 3,
}
SELECTION_LIMIT = 20
# Shared evidence may be materialized under this run's shared/ directory or may
# reference the source records of the single owned per-cycle workspace tree that
# this run reconciled against; both are immutable for the lifetime of the run.
WORKSPACE_ROOT = (PROJECT_ROOT / "working_tmp").resolve()
TRACK_CONTRACT_FILENAMES = {
    "consensus": "consensus.md",
    "rating_skill": "rating_skill.md",
    "agent_prompt": "agent_prompt.md",
}
TRACK_INPUT_FIELDS = (
    "candidate_id",
    "title",
    "category",
    "canonical_record_path",
    "source_record_paths",
    "source_urls",
    "canonical_url",
    "normalized_arxiv_id",
    "source_identities",
)
FORBIDDEN_SHARED_QUALITY_FIELDS = frozenset(
    {
        "admission",
        "decision",
        "group_rank",
        "group_score",
        "qualified",
        "rank",
        "rough_score",
        "score",
        "scores",
        "selected",
        "total_score",
    }
)
SCORE_SCALES = {
    "paper": "paper-v2",
    "news": "news-v3",
    "policy": "policy-v3",
}
CONFIDENCE_VALUES = frozenset({"high", "medium", "low"})
PRIOR_DECISIONS = frozenset(
    {
        "confirmed_same",
        "distinct_sequel",
        "independent_announcement",
        "ambiguous_retained",
        "no_match",
    }
)
COMPLETED_PRIOR_PHASES = frozenset({"Migrated", "Released"})
FORBIDDEN_PRIOR_FIELDS = frozenset(
    {"Score", "Summary", "Selected", "Category", "PublishContent", "RecordContent"}
)
SCORE_COMPONENTS = {
    "paper": {
        "relevance": 30,
        "novelty": 20,
        "technical_credibility": 20,
        "impact": 15,
        "team_signal": 10,
        "timeliness": 5,
    },
    "news": {
        "materiality": 30,
        "evidence_strength": 25,
        "research_deployment_impact": 20,
        "novelty_increment": 15,
        "timeliness": 10,
    },
    "policy": {
        "policy_materiality": 30,
        "legal_authority": 25,
        "scope_clarity": 20,
        "implementation_path": 15,
        "timeliness": 10,
    },
}

TRACK_OUTPUT_FIELDS = frozenset(
    {
        "schema_version",
        "quota_contract",
        "production_output",
        "isolated_context",
        "old_run_judgments_read",
        "cross_track_reads",
        "canonicalization_performed",
        "classification_performed",
        "run_id",
        "run_identity",
        "track",
        "context_id",
        "output_identity",
        "production_input_path",
        "consensus_path",
        "actual_paths_read",
        "external_urls_read",
        "ordered_candidate_ids",
        "decisions",
        "under_target_reason",
    }
)
DECISION_FIELDS = frozenset(
    {
        "candidate_id",
        "category",
        "admission_passed",
        "qualified",
        "group_score",
        "score_components",
        "confidence",
        "decision_reasons",
        "evidence_paths",
        "comparison_reasons",
        "cutoff_reason",
    }
)


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _valid_source_urls(value: object) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if not isinstance(item, str) or item != item.strip():
            return False
        parsed = urlsplit(item)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return False
    return True


def _immutable_write_json(path: Path, payload: object) -> bool:
    """Create JSON once, reuse an exact value, and reject every mutation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _canonical_json(_load_json(path)) != _canonical_json(payload):
            raise ValueError(f"immutable JSON differs from existing file: {path}")
        return False

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, path)
            return True
        except FileExistsError:
            if _canonical_json(_load_json(path)) != _canonical_json(payload):
                raise ValueError(f"immutable JSON differs from existing file: {path}")
            return False
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _immutable_write_bytes(path: Path, payload: bytes) -> bool:
    """Create bytes once, reuse an exact byte sequence, and reject drift."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise ValueError(f"immutable bytes differ from existing file: {path}")
        return False

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary_name, path)
            return True
        except FileExistsError:
            if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
                raise ValueError(f"immutable bytes differ from existing file: {path}")
            return False
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _require_exact_json(path: Path, expected: object, stage: str) -> None:
    if not path.is_file():
        raise ValueError(f"{stage} is required before continuing: {path}")
    if _canonical_json(_load_json(path)) != _canonical_json(expected):
        raise ValueError(f"immutable {stage} differs from the frozen shared input")


def _project_path(value: str | Path) -> Path:
    path = Path(value)
    resolved = (PROJECT_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    if resolved != PROJECT_ROOT and PROJECT_ROOT not in resolved.parents:
        raise ValueError(f"path escapes the project directory: {value}")
    return resolved


def _managed_run_dir(run_dir: Path) -> Path:
    run_dir = _project_path(run_dir)
    if run_dir.parent != (RATING_ROOT / "runs").resolve():
        raise ValueError(
            "run directory must be under a project-local "
            "working_tmp/rating_filter_organize/runs directory"
        )
    return run_dir


def _atomic_replace_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


@contextmanager
def _activation_lock():
    RATING_ROOT.mkdir(parents=True, exist_ok=True)
    lock_path = RATING_ROOT / ".activation.lock"
    if lock_path.is_symlink():
        raise ValueError("rating activation lock must not be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _candidate_id(item: Mapping[str, object], location: str) -> str:
    value = item.get("candidate_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location} has no non-empty candidate_id")
    return value


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _output_path(run_dir: Path, track: str) -> Path:
    return run_dir / "tracks" / track / "output.json"


def _input_path(run_dir: Path, track: str) -> Path:
    return run_dir / "tracks" / track / "input.json"


def _terminal_path(run_dir: Path, track: str) -> Path:
    return run_dir / "tracks" / track / "terminal_output.json"


def _track_contract_paths(run_dir: Path, track: str) -> dict[str, Path]:
    contracts_dir = run_dir / "tracks" / track / "contracts"
    if contracts_dir.resolve() != contracts_dir:
        raise ValueError(f"{track} contract directory must not use symbolic links")
    return {
        name: contracts_dir / filename
        for name, filename in TRACK_CONTRACT_FILENAMES.items()
    }


def _track_contract_identities(track: str) -> dict[str, str]:
    return {
        "consensus": f"{SCORE_SCALES[track]}:consensus",
        "rating_skill": "rating-filter-organize:skill",
        "agent_prompt": f"{track}:agent_prompt",
    }


def _track_contract_sources(track: str) -> dict[str, Path]:
    return {
        "consensus": TRACK_CONSENSUS_PATHS[track],
        "rating_skill": RATING_SKILL_PATH,
        "agent_prompt": TRACK_PROMPT_PATHS[track],
    }


def _track_contract_declarations(
    run_dir: Path,
    track: str,
) -> dict[str, dict[str, object]]:
    snapshot_paths = _track_contract_paths(run_dir, track)
    identities = _track_contract_identities(track)
    metadata: dict[str, dict[str, object]] = {}
    for name, source in _track_contract_sources(track).items():
        resolved_source = _project_path(source)
        if not resolved_source.is_file():
            raise ValueError(f"{track} {name} contract source is not a file")
        metadata[name] = {
            "contract_identity": identities[name],
            "path": snapshot_paths[name].resolve().as_posix(),
            "size_bytes": resolved_source.stat().st_size,
        }
    return metadata


def _freeze_track_contracts(run_dir: Path, track: str) -> dict[str, dict[str, object]]:
    """Freeze the exact contract bytes used by one isolated track."""
    metadata = _track_contract_declarations(run_dir, track)
    input_already_frozen = _input_path(run_dir, track).is_file()
    for name, source in _track_contract_sources(track).items():
        payload = _project_path(source).read_bytes()
        if len(payload) != metadata[name]["size_bytes"]:
            raise ValueError(f"{track} {name} contract changed while being frozen")
        snapshot_path = Path(metadata[name]["path"])
        if input_already_frozen and not snapshot_path.is_file():
            raise ValueError(
                f"immutable {track} {name} contract snapshot is missing; "
                "a frozen input cannot recreate it"
            )
        _immutable_write_bytes(snapshot_path, payload)
    return metadata


def _freeze_all_track_contracts(run_dir: Path) -> None:
    """Capture both track contracts before either production input exists."""
    run_already_frozen = (run_dir / "partition_manifest.json").is_file() or any(
        _input_path(run_dir, track).is_file() for track in TRACK_CATEGORIES
    )
    source_bytes: dict[Path, bytes] = {}
    frozen_bytes: dict[tuple[str, str], bytes] = {}
    for track in TRACK_CATEGORIES:
        for name, source in _track_contract_sources(track).items():
            resolved = _project_path(source)
            source_bytes.setdefault(resolved, resolved.read_bytes())
            frozen_bytes[(track, name)] = source_bytes[resolved]
    for track in TRACK_CATEGORIES:
        paths = _track_contract_paths(run_dir, track)
        identities = _track_contract_identities(track)
        for name in TRACK_CONTRACT_FILENAMES:
            payload = frozen_bytes[(track, name)]
            if run_already_frozen and not paths[name].is_file():
                raise ValueError(
                    f"immutable {track} {name} contract snapshot is missing; "
                    "a frozen input cannot recreate it"
                )
            _immutable_write_bytes(paths[name], payload)
            declaration = _track_contract_declarations(run_dir, track)[name]
            if declaration != {
                "contract_identity": identities[name],
                "path": paths[name].resolve().as_posix(),
                "size_bytes": len(payload),
            }:
                raise ValueError(f"{track} {name} contract changed during run freeze")
    for source, payload in source_bytes.items():
        if source.read_bytes() != payload:
            raise ValueError(f"contract changed during run freeze: {source}")


def _resolve_supplied_output(
    run_dir: Path,
    track: str,
    supplied: Path | None,
) -> Path:
    expected = _output_path(run_dir, track).resolve()
    resolved = expected if supplied is None else _project_path(supplied)
    if resolved != expected:
        raise ValueError(f"{track} output must use its declared production output path")
    if not resolved.is_file():
        raise ValueError(f"{track} terminal output is required: {resolved}")
    return resolved


def _recover_rating_activation() -> bool:
    """Complete a validated interrupted activation journal under the global lock."""
    publishing_path = RATING_ROOT / "publishing.json"
    if not publishing_path.exists():
        return False
    with _activation_lock():
        if not publishing_path.exists():
            return False
        marker = _load_json(publishing_path)
        if not isinstance(marker, dict) or set(marker) != {
            "schema_version",
            "publication_kind",
            "status",
            "cycle_id",
            "rating_run_id",
            "manifest_path",
            "grouped_selection_path",
        }:
            raise ValueError("rating publishing journal has an invalid schema")
        if (
            marker.get("schema_version") != 3
            or marker.get("publication_kind") != "internal_rating_snapshot"
            or marker.get("status") != "activating"
            or not isinstance(marker.get("cycle_id"), str)
            or not isinstance(marker.get("rating_run_id"), str)
        ):
            raise ValueError("rating publishing journal has invalid identity values")
        run_dir = _managed_run_dir(RATING_ROOT / "runs" / marker["rating_run_id"])
        manifest_path = _project_path(marker["manifest_path"])
        artifact_path = _project_path(marker["grouped_selection_path"])
        if (
            manifest_path != run_dir / "manifest.json"
            or artifact_path != run_dir / "grouped_selection.json"
            or not manifest_path.is_file()
            or not artifact_path.is_file()
        ):
            raise ValueError("rating publishing journal paths are not canonical")
        manifest = _load_json(manifest_path)
        artifact = _load_json(artifact_path)
        validation = validate_grouped_candidates(artifact)
        if (
            not isinstance(manifest, dict)
            or manifest.get("status") != "complete"
            or manifest.get("publication_kind") != "internal_rating_snapshot"
            or manifest.get("run_id") != marker["rating_run_id"]
            or manifest.get("cycle_id") != marker["cycle_id"]
            or manifest.get("grouped_selection_path")
            != marker["grouped_selection_path"]
            or manifest.get("selected_count") != validation["candidate_count"]
            or not validation["valid"]
        ):
            raise ValueError("rating publishing journal targets are not complete and valid")
        current = {
            "schema_version": 3,
            "publication_kind": "internal_rating_snapshot",
            "status": "complete",
            "cycle_id": marker["cycle_id"],
            "rating_run_id": marker["rating_run_id"],
            "run_id": marker["rating_run_id"],
            "run_path": _relative_project_path(run_dir),
            "manifest_path": marker["manifest_path"],
            "grouped_selection_path": marker["grouped_selection_path"],
            "selection_limit": manifest["selection_limit"],
            "selected_count": manifest["selected_count"],
            "activated_after_validation": True,
        }
        index_path = RATING_ROOT / "activation_index.json"
        index = _load_json(index_path) if index_path.exists() else {}
        if not isinstance(index, dict):
            raise ValueError("rating activation index must be an object")
        active_cycles = index.get("active_cycles", {})
        if not isinstance(active_cycles, dict):
            raise ValueError("rating activation index active_cycles must be an object")
        active_cycles = dict(active_cycles)
        active_cycles[marker["cycle_id"]] = {
            "rating_run_id": marker["rating_run_id"],
            "manifest_path": marker["manifest_path"],
            "grouped_selection_path": marker["grouped_selection_path"],
        }
        _atomic_replace_json(
            index_path,
            {
                "schema_version": 3,
                "publication_kind": "internal_rating_snapshot",
                "active_cycles": active_cycles,
            },
        )
        _atomic_replace_json(RATING_ROOT / "current.json", current)
        publishing_path.unlink()
        return True


def _shared_payload(run_dir: Path, filename: str) -> dict[str, object]:
    path = run_dir / "shared" / filename
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"shared/{filename} is required before production partitioning")
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"shared/{filename} must contain an object")
    return payload


def _validate_prior_snapshot(prior: dict[str, object]) -> dict[str, object]:
    snapshot = prior.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("prior_cycle_identity.json must contain a snapshot object")
    forbidden = FORBIDDEN_PRIOR_FIELDS.intersection(snapshot)
    if forbidden:
        raise ValueError(f"prior snapshot contains forbidden judgment fields: {sorted(forbidden)}")
    first_cycle = snapshot.get("first_cycle")
    previous_publish = snapshot.get("previous_publish")
    records = snapshot.get("records")
    before = snapshot.get("before")
    after = snapshot.get("after")
    if not isinstance(first_cycle, bool) or not isinstance(records, list):
        raise ValueError("prior snapshot must declare first_cycle and records")
    if len(records) > 20 and (not isinstance(previous_publish, dict) or previous_publish.get("phase") != "Migrated"):
        raise ValueError("non-migrated prior snapshot machine record count exceeds 20")
    if first_cycle:
        if previous_publish is not None or before is not None or after is not None or records:
            raise ValueError("first-cycle prior snapshot must be explicitly empty")
        return {"first_cycle": True, "prior_record_count": 0}
    if not isinstance(previous_publish, dict):
        raise ValueError("prior snapshot must declare the previous Publish")
    required_publish = {"row_id", "cycle_id", "phase", "updated"}
    if set(previous_publish) != required_publish:
        raise ValueError("previous Publish snapshot must use the exact identity schema")
    if (
        isinstance(previous_publish["row_id"], bool)
        or not isinstance(previous_publish["row_id"], int)
        or previous_publish["row_id"] <= 0
        or not isinstance(previous_publish["cycle_id"], str)
        or not previous_publish["cycle_id"]
        or previous_publish["phase"] not in COMPLETED_PRIOR_PHASES
        or not isinstance(previous_publish["updated"], str)
        or not previous_publish["updated"]
    ):
        raise ValueError("previous Publish snapshot has invalid identity values")
    if not isinstance(before, dict) or not isinstance(after, dict) or before != after:
        raise ValueError("prior snapshot before/after readback is not stable")
    if set(before) != {"publish", "records"} or before["publish"] != previous_publish:
        raise ValueError("prior snapshot readback does not match the previous Publish")
    stable_records = before["records"]
    if not isinstance(stable_records, list):
        raise ValueError("prior snapshot readback records must be a list")

    record_ids = set()
    candidate_ids = set()
    stable_identity_rows = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(f"prior snapshot record {index} must be an object")
        if FORBIDDEN_PRIOR_FIELDS.intersection(record):
            raise ValueError("prior snapshot record contains forbidden judgment fields")
        allowed = {
            "row_id",
            "candidate_id",
            "title",
            "publish_row_id",
            "updated",
            "normalized_arxiv_id",
            "canonical_url",
            "source_identities",
        }
        if set(record) != allowed:
            raise ValueError("prior snapshot record must use the exact identity whitelist")
        row_id = record["row_id"]
        candidate_id = record["candidate_id"]
        if (
            isinstance(row_id, bool)
            or not isinstance(row_id, int)
            or row_id <= 0
            or row_id in record_ids
            or not isinstance(candidate_id, str)
            or not candidate_id
            or candidate_id in candidate_ids
            or record["publish_row_id"] != previous_publish["row_id"]
            or not isinstance(record["title"], str)
            or not isinstance(record["updated"], str)
            or not isinstance(record["source_identities"], list)
        ):
            raise ValueError("prior snapshot records are invalid or non-unique")
        for nullable in ("normalized_arxiv_id", "canonical_url"):
            if record[nullable] is not None and not isinstance(record[nullable], str):
                raise ValueError(f"prior snapshot {nullable} must be text or null")
        if not all(isinstance(value, str) and value for value in record["source_identities"]):
            raise ValueError("prior snapshot source identities must be non-empty strings")
        record_ids.add(row_id)
        candidate_ids.add(candidate_id)
        stable_identity_rows.append(
            {"row_id": row_id, "candidate_id": candidate_id, "updated": record["updated"]}
        )
    if stable_records != sorted(stable_identity_rows, key=lambda item: item["row_id"]):
        raise ValueError("prior snapshot record readback does not match identity rows")
    return {"first_cycle": False, "prior_record_count": len(records)}


def _record_path(value: object, location: str, shared_dir: Path) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a non-empty path")
    resolved = _project_path(value)
    if (
        shared_dir not in resolved.parents
        and WORKSPACE_ROOT not in resolved.parents
    ) or not resolved.is_file():
        raise ValueError(
            f"{location} must be a file under this run's shared directory "
            "or the owned workspace"
        )
    return resolved.as_posix()


def reconcile_shared_manifests(
    run_dir: Path,
    cycle_id: str,
    routing_candidates: list[dict[str, object]],
) -> dict[str, object]:
    """Prove source records, canonical objects, prior decisions, and routing agree."""
    inventory = _shared_payload(run_dir, "source_inventory.json")
    canonical = _shared_payload(run_dir, "canonical_objects.json")
    prior = _shared_payload(run_dir, "prior_cycle_identity.json")
    shared_dir = (run_dir / "shared").resolve()
    for name, payload in (
        ("source_inventory.json", inventory),
        ("canonical_objects.json", canonical),
        ("prior_cycle_identity.json", prior),
    ):
        if payload.get("run_id") != run_dir.name or payload.get("cycle_id") != cycle_id:
            raise ValueError(f"shared/{name} identity does not match routing")

    discovered = inventory.get("discovered_sources")
    sources = inventory.get("sources")
    records = inventory.get("records")
    if not isinstance(discovered, list) or not all(
        isinstance(value, str) and value for value in discovered
    ) or len(discovered) != len(set(discovered)):
        raise ValueError("source inventory must list unique discovered_sources")
    if not isinstance(sources, list) or not isinstance(records, list):
        raise ValueError("source inventory must contain sources and records lists")
    source_names: set[str] = set()
    declared_record_paths: set[str] = set()
    source_statuses: dict[str, str] = {}
    valid_statuses = {"success", "success_empty", "success_stale", "partial", "skipped", "failed", "blocked", "not_yet_published"}
    for index, raw in enumerate(sources):
        if not isinstance(raw, dict):
            raise ValueError(f"source inventory source {index} must be an object")
        name = raw.get("source_name")
        status = raw.get("status")
        error = raw.get("error")
        paths = raw.get("record_paths")
        if not isinstance(name, str) or not name or name in source_names:
            raise ValueError("source inventory source names must be unique and non-empty")
        if status not in valid_statuses:
            raise ValueError(f"source inventory has invalid status for {name}")
        if error is not None and not isinstance(error, str):
            raise ValueError(f"source inventory error for {name} must be text or null")
        if not isinstance(paths, list):
            raise ValueError(f"source inventory record_paths for {name} must be a list")
        source_names.add(name)
        source_statuses[name] = status
        for path_index, value in enumerate(paths):
            path = _record_path(value, f"source {name} record {path_index}", shared_dir)
            if path in declared_record_paths:
                raise ValueError(f"source record is declared more than once: {path}")
            declared_record_paths.add(path)
    if source_names != set(discovered):
        raise ValueError("source inventory must account for every dynamically discovered source")

    inventory_record_paths: set[str] = set()
    inventory_records_by_path: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(records):
        if not isinstance(raw, dict):
            raise ValueError(f"source inventory record {index} must be an object")
        if raw.get("inventory_source_name") not in source_names:
            raise ValueError("source inventory record refers to an unknown source")
        path = _record_path(raw.get("path"), f"source inventory record {index}", shared_dir)
        if path in inventory_record_paths:
            raise ValueError(f"duplicate source inventory record path: {path}")
        inventory_record_paths.add(path)
        inventory_records_by_path[path] = raw
    if inventory_record_paths != declared_record_paths:
        raise ValueError("source inventory source declarations and records do not reconcile")

    objects = canonical.get("objects")
    if not isinstance(objects, list):
        raise ValueError("canonical_objects.json must contain an objects list")
    canonical_by_id: dict[str, dict[str, object]] = {}
    canonical_membership: set[str] = set()
    for index, raw in enumerate(objects):
        if not isinstance(raw, dict):
            raise ValueError(f"canonical object {index} must be an object")
        candidate_id = _candidate_id(raw, f"canonical object {index}")
        if candidate_id in canonical_by_id:
            raise ValueError(f"duplicate canonical candidate_id: {candidate_id}")
        category = raw.get("category")
        if category not in VALID_CATEGORY_SET:
            raise ValueError(f"canonical object {candidate_id} has invalid category")
        canonical_path = _record_path(
            raw.get("canonical_record_path"),
            f"canonical object {candidate_id}",
            shared_dir,
        )
        member_paths = raw.get("source_record_paths")
        if not isinstance(member_paths, list) or not member_paths:
            raise ValueError(f"canonical object {candidate_id} must have source records")
        normalized_members = {
            _record_path(value, f"canonical object {candidate_id} member", shared_dir)
            for value in member_paths
        }
        if len(normalized_members) != len(member_paths):
            raise ValueError(f"canonical object {candidate_id} repeats a source record")
        overlap = canonical_membership & normalized_members
        if overlap:
            raise ValueError(f"source records belong to multiple canonical objects: {sorted(overlap)}")
        canonical_membership.update(normalized_members)
        source_urls = raw.get("source_urls")
        if not _valid_source_urls(source_urls):
            raise ValueError(f"canonical object {candidate_id} source_urls must be HTTP(S) strings")
        canonical_url = raw.get("canonical_url")
        normalized_arxiv_id = raw.get("normalized_arxiv_id")
        source_identities = raw.get("source_identities")
        member_records = [inventory_records_by_path[path] for path in normalized_members]
        inventory_urls = [record.get("url") for record in member_records]
        if not _valid_source_urls(inventory_urls) or set(source_urls) != set(inventory_urls):
            raise ValueError(f"canonical object {candidate_id} source URLs differ from inventory identities")
        inventory_arxiv_ids = {record.get("normalized_arxiv_id") for record in member_records if record.get("normalized_arxiv_id") is not None}
        expected_arxiv_id = next(iter(inventory_arxiv_ids)) if len(inventory_arxiv_ids) == 1 else None
        if len(inventory_arxiv_ids) > 1 or canonical_url not in source_urls or normalized_arxiv_id != expected_arxiv_id:
            raise ValueError(f"canonical object {candidate_id} has an invalid immutable identity projection")
        if source_identities != source_urls or len(source_identities) != len(set(source_identities)):
            raise ValueError(f"canonical object {candidate_id} source_identities must exactly preserve complete inventory URLs")
        canonical_by_id[candidate_id] = {
            "category": category,
            "canonical_record_path": canonical_path,
            "source_record_paths": normalized_members,
            "source_urls": source_urls,
            "canonical_url": canonical_url,
            "normalized_arxiv_id": normalized_arxiv_id,
            "source_identities": source_identities,
        }
    if canonical_membership != inventory_record_paths:
        raise ValueError("canonical membership does not account for every source record exactly once")

    decisions = prior.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("prior_cycle_identity.json must contain a decisions list")
    prior_by_id: dict[str, str] = {}
    prior_snapshot_report = _validate_prior_snapshot(prior)
    for index, raw in enumerate(decisions):
        if not isinstance(raw, dict):
            raise ValueError(f"prior identity decision {index} must be an object")
        candidate_id = _candidate_id(raw, f"prior identity decision {index}")
        decision = raw.get("decision")
        if candidate_id in prior_by_id or decision not in PRIOR_DECISIONS:
            raise ValueError(f"invalid or duplicate prior identity decision for {candidate_id}")
        prior_by_id[candidate_id] = decision
    if set(prior_by_id) != set(canonical_by_id):
        raise ValueError("prior identity decisions must cover every canonical object exactly once")

    eligible_ids = {
        candidate_id
        for candidate_id, decision in prior_by_id.items()
        if decision != "confirmed_same"
    }
    routing_by_id = {
        _candidate_id(item, "routing candidate"): item for item in routing_candidates
    }
    if len(routing_by_id) != len(routing_candidates) or set(routing_by_id) != eligible_ids:
        raise ValueError("routing candidates must exactly equal non-excluded canonical objects")
    for candidate_id, item in routing_by_id.items():
        expected = canonical_by_id[candidate_id]
        canonical_path = _project_path(item.get("canonical_record_path", "")).as_posix()
        source_paths = {
            _project_path(value).as_posix() for value in item.get("source_record_paths", [])
        }
        source_urls = item.get("source_urls")
        if not _valid_source_urls(source_urls):
            raise ValueError(f"routing candidate {candidate_id} source_urls must be HTTP(S) strings")
        if (
            item.get("category") != expected["category"]
            or canonical_path != expected["canonical_record_path"]
            or source_paths != expected["source_record_paths"]
            or source_urls != expected["source_urls"]
            or item.get("canonical_url") != expected["canonical_url"]
            or item.get("normalized_arxiv_id") != expected["normalized_arxiv_id"]
            or item.get("source_identities") != expected["source_identities"]
        ):
            raise ValueError(f"routing candidate {candidate_id} differs from canonical freeze")
    return {
        "source_count": len(source_names),
        "source_statuses": source_statuses,
        "source_record_count": len(inventory_record_paths),
        "canonical_object_count": len(canonical_by_id),
        "prior_exclusion_count": sum(
            decision == "confirmed_same" for decision in prior_by_id.values()
        ),
        **prior_snapshot_report,
        "eligible_count": len(eligible_ids),
    }


def load_frozen_production_input(run_dir: Path) -> dict[str, object]:
    """Load the quality-neutral shared routing freeze for a production run."""
    run_dir = _managed_run_dir(run_dir)
    routing_path = run_dir / "shared" / "routing.json"
    if not routing_path.is_file():
        raise ValueError("shared/routing.json is required before production partitioning")
    if (RATING_ROOT / "publishing.json").exists():
        _recover_rating_activation()

    payload = _load_json(routing_path)
    if not isinstance(payload, dict):
        raise ValueError("shared/routing.json must contain an object")
    if payload.get("production_input") is not True:
        raise ValueError("shared routing must declare production_input=true")
    if payload.get("quality_neutral") is not True:
        raise ValueError("shared routing must declare quality_neutral=true")
    run_id = payload.get("run_id")
    if run_id != run_dir.name:
        raise ValueError("shared routing run_id does not match its immutable run directory")
    cycle_id = payload.get("cycle_id")
    if not isinstance(cycle_id, str) or not cycle_id:
        raise ValueError("shared routing must declare a non-empty cycle_id")
    selection_limit = payload.get("selection_limit")
    if selection_limit != SELECTION_LIMIT:
        raise ValueError(f"three-track-v3 selection_limit must be exactly {SELECTION_LIMIT}")
    candidates = payload.get("eligible_candidates")
    if not isinstance(candidates, list):
        raise ValueError("shared routing must contain an eligible_candidates list")

    reconciliation = reconcile_shared_manifests(run_dir, cycle_id, candidates)
    normalized: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    seen_evidence_paths: dict[str, str] = {}
    shared_dir = (run_dir / "shared").resolve()
    for index, raw in enumerate(candidates):
        if not isinstance(raw, dict):
            raise ValueError(f"eligible candidate {index} must be an object")
        candidate_id = _candidate_id(raw, f"eligible candidate {index}")
        if candidate_id in seen_ids:
            raise ValueError(f"duplicate frozen candidate_id: {candidate_id}")
        seen_ids.add(candidate_id)
        category = raw.get("category")
        if category not in VALID_CATEGORY_SET:
            raise ValueError(
                f"frozen candidate {candidate_id} has invalid final category: {category!r}"
            )
        forbidden = sorted(FORBIDDEN_SHARED_QUALITY_FIELDS.intersection(raw))
        if forbidden:
            raise ValueError(
                f"shared routing candidate {candidate_id} contains quality fields: {forbidden}"
            )
        canonical = raw.get("canonical_record_path")
        if not isinstance(canonical, str) or not canonical:
            raise ValueError(f"{candidate_id} must declare canonical_record_path")
        sources = raw.get("source_record_paths")
        if not isinstance(sources, list) or not all(
            isinstance(value, str) and value for value in sources
        ):
            raise ValueError(f"{candidate_id} source_record_paths must be a list of strings")
        evidence_paths = [canonical, *sources]
        if len(evidence_paths) != len(set(evidence_paths)):
            raise ValueError(f"{candidate_id} contains duplicate assigned evidence paths")
        resolved_evidence: list[Path] = []
        evidence_files: list[dict[str, object]] = []
        for evidence_index, value in enumerate(evidence_paths):
            resolved = _project_path(value)
            if (
                shared_dir not in resolved.parents
                and WORKSPACE_ROOT not in resolved.parents
            ):
                raise ValueError(
                    "assigned evidence path must resolve under this exact run's "
                    f"shared directory or the owned workspace: {value}"
                )
            if not resolved.is_file():
                raise ValueError(f"assigned evidence path is not a file: {value}")
            key = resolved.as_posix()
            if key in seen_evidence_paths:
                raise ValueError(
                    "evidence path is assigned to multiple candidates: "
                    f"{seen_evidence_paths[key]} and {candidate_id}"
                )
            seen_evidence_paths[key] = candidate_id
            resolved_evidence.append(resolved)
            evidence_role = (
                "canonical_record" if evidence_index == 0 else "source_record"
            )
            role_index = 0 if evidence_index == 0 else evidence_index
            evidence_files.append(
                {
                    "candidate_id": candidate_id,
                    "evidence_identity": f"{candidate_id}:{evidence_role}:{role_index}",
                    "evidence_role": evidence_role,
                    "path": key,
                    "size_bytes": resolved.stat().st_size,
                }
            )
        item = {field: raw[field] for field in TRACK_INPUT_FIELDS if field in raw}
        item["canonical_record_path"] = resolved_evidence[0].as_posix()
        item["source_record_paths"] = [
            path.as_posix() for path in resolved_evidence[1:]
        ]
        item["source_urls"] = list(raw["source_urls"])
        item["evidence_files"] = evidence_files
        normalized.append(item)

    return {
        "run_id": run_id,
        "cycle_id": cycle_id,
        "selection_limit": selection_limit,
        "candidates": normalized,
        "routing_path": routing_path.as_posix(),
        "run_dir": run_dir,
        "shared_reconciliation": reconciliation,
    }


def partition_track_inputs(
    candidates: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    partitions = {track: [] for track in TRACK_CATEGORIES}
    for item in candidates:
        track = str(item["category"]).lower()
        partitions[track].append(dict(item))
    return partitions


def _freeze_track_evidence(
    run_dir: Path,
    track: str,
    candidates: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Copy assigned evidence into a track-private immutable read tree."""
    frozen_candidates: list[dict[str, object]] = []
    for candidate_index, candidate in enumerate(candidates, start=1):
        candidate_dir = run_dir / "tracks" / track / "evidence" / f"{candidate_index:04d}"
        source_paths = [Path(value) for value in candidate["source_record_paths"]]
        copies = [
            (Path(candidate["canonical_record_path"]), candidate_dir / "canonical_record.md"),
            *(
                (source, candidate_dir / f"source_record_{index:04d}.md")
                for index, source in enumerate(source_paths, start=1)
            ),
        ]
        evidence_files = []
        for evidence_index, (source, target) in enumerate(copies):
            payload = source.read_bytes()
            if not payload:
                raise ValueError(f"assigned evidence is empty: {source}")
            _immutable_write_bytes(target, payload)
            if source.read_bytes() != payload:
                raise ValueError(f"assigned evidence changed while being frozen: {source}")
            evidence_role = "canonical_record" if evidence_index == 0 else "source_record"
            evidence_files.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "evidence_identity": (
                        f"{candidate['candidate_id']}:{evidence_role}:{evidence_index}"
                    ),
                    "evidence_role": evidence_role,
                    "path": target.resolve().as_posix(),
                    "size_bytes": len(payload),
                }
            )
        frozen = {
            field: candidate[field]
            for field in ("candidate_id", "title", "category", "canonical_url", "normalized_arxiv_id", "source_identities")
            if field in candidate
        }
        frozen["canonical_record_path"] = evidence_files[0]["path"]
        frozen["source_record_paths"] = [
            evidence["path"] for evidence in evidence_files[1:]
        ]
        frozen["source_urls"] = list(candidate["source_urls"])
        frozen["evidence_files"] = evidence_files
        frozen_candidates.append(frozen)
    return frozen_candidates


def prepare_shared_partitions(run_dir: Path) -> dict[str, object]:
    """Freeze exhaustive shared partitions without materializing any child input."""
    frozen = load_frozen_production_input(run_dir)
    _freeze_all_track_contracts(frozen["run_dir"])
    shared_partitions = partition_track_inputs(frozen["candidates"])
    partitions = {
        track: _freeze_track_evidence(frozen["run_dir"], track, candidates)
        for track, candidates in shared_partitions.items()
    }
    track_ids = {
        track: {item["candidate_id"] for item in candidates}
        for track, candidates in partitions.items()
    }
    frozen_ids = {item["candidate_id"] for item in frozen["candidates"]}
    all_ids: set[str] = set()
    for track in TRACK_CATEGORIES:
        if all_ids & track_ids[track]:
            raise ValueError("production partitions are not disjoint")
        all_ids.update(track_ids[track])
    if all_ids != frozen_ids:
        raise ValueError("production partitions are not exhaustive and disjoint")

    manifest: dict[str, object] = {
        "schema_version": 3,
        "quota_contract": "three-track-v3",
        "production_input": True,
        "quality_neutral_shared_input": True,
        "run_id": frozen["run_id"],
        "cycle_id": frozen["cycle_id"],
        "selection_limit": frozen["selection_limit"],
        "routing_path": frozen["routing_path"],
        "shared_reconciliation": frozen["shared_reconciliation"],
        "materialization_order": [
            *(f"{track}_input" for track in TRACK_CATEGORIES),
            *(f"{track}_terminal_output" for track in TRACK_CATEGORIES),
            "assembly",
        ],
        "partitions": {
            track: {
                "candidate_ids": [
                    item["candidate_id"] for item in partitions[track]
                ],
                "candidates": partitions[track],
                "evidence_files": [
                    evidence
                    for item in partitions[track]
                    for evidence in item["evidence_files"]
                ],
                "contract_files": _track_contract_declarations(
                    frozen["run_dir"], track
                ),
                "capacity": TRACK_RESERVE_CAPACITIES[track],
                "target": TRACK_TARGETS[track],
            }
            for track in TRACK_CATEGORIES
        },
        "exhaustive": True,
        "disjoint": True,
    }
    manifest_path = frozen["run_dir"] / "partition_manifest.json"
    _immutable_write_json(manifest_path, manifest)
    return {
        "frozen": frozen,
        "partitions": partitions,
        "partition_manifest": manifest,
        "partition_manifest_path": manifest_path.as_posix(),
    }

def _allowed_track_paths(
    input_path: Path,
    assigned: list[dict[str, object]],
    contracts: dict[str, dict[str, object]],
) -> list[str]:
    paths = {
        input_path.resolve(),
        *(_project_path(contract["path"]) for contract in contracts.values()),
    }
    for item in assigned:
        paths.add(_project_path(item["canonical_record_path"]))
        paths.update(_project_path(value) for value in item["source_record_paths"])
    return sorted(path.as_posix() for path in paths)


def _track_input(prepared: dict[str, object], track: str) -> dict[str, object]:
    frozen = prepared["frozen"]
    assigned = prepared["partitions"][track]
    input_path = _input_path(frozen["run_dir"], track)
    contracts = _freeze_track_contracts(frozen["run_dir"], track)
    capacity_field = TRACK_CAPACITY_FIELDS[track]
    capacity = TRACK_RESERVE_CAPACITIES[track]
    target = TRACK_TARGETS[track]
    return {
        "schema_version": 3,
        "quota_contract": "three-track-v3",
        "production_input": True,
        "isolated_partition": True,
        "quality_neutral_shared_input": True,
        "run_id": frozen["run_id"],
        "cycle_id": frozen["cycle_id"],
        "track": track,
        "selection_limit": frozen["selection_limit"],
        capacity_field: capacity,
        "track_target": target,
        "track_reserve_capacity": capacity,
        "run_identity": {
            "rating_run_id": frozen["run_id"],
            "track": track,
        },
        "consensus_path": contracts["consensus"]["path"],
        "rating_skill_path": contracts["rating_skill"]["path"],
        "agent_prompt_path": contracts["agent_prompt"]["path"],
        "contract_files": contracts,
        "output_path": _output_path(frozen["run_dir"], track).as_posix(),
        "allowed_paths": _allowed_track_paths(input_path, assigned, contracts),
        "candidates": assigned,
    }


def prepare_track(run_dir: Path, track: str) -> dict[str, object]:
    """Materialize one isolated production input without reading sibling outputs."""
    if track not in TRACK_CATEGORIES:
        raise ValueError(f"unknown production track: {track}")
    prepared = prepare_shared_partitions(run_dir)
    track_input = _track_input(prepared, track)
    input_path = _input_path(prepared["frozen"]["run_dir"], track)
    created = _immutable_write_json(input_path, track_input)
    capacity_field = TRACK_CAPACITY_FIELDS[track]
    return {
        "stage": f"{track}_input",
        "run_id": prepared["frozen"]["run_id"],
        "track": track,
        "input_path": input_path.as_posix(),
        "output_path": track_input["output_path"],
        capacity_field: track_input[capacity_field],
        "created": created,
    }

def _load_track_output(path: Path, expected_track: str) -> dict[str, object]:
    payload = _load_json(path)
    if not isinstance(payload, dict):
        raise ValueError(f"{expected_track} output must be an object")
    if set(payload) != TRACK_OUTPUT_FIELDS:
        raise ValueError(
            f"{expected_track} output must contain the exact bounded output schema"
        )
    if payload.get("schema_version") != 3:
        raise ValueError(f"{expected_track} output schema_version must be 3")
    if payload.get("quota_contract") != "three-track-v3":
        raise ValueError(f"{expected_track} output quota_contract must be three-track-v3")
    required_values = {
        "production_output": True,
        "isolated_context": True,
        "old_run_judgments_read": False,
        "cross_track_reads": False,
        "canonicalization_performed": False,
        "classification_performed": False,
    }
    for field, expected in required_values.items():
        if payload.get(field) is not expected:
            raise ValueError(f"{expected_track} output must declare {field}={expected!r}")
    if payload.get("track") != expected_track:
        raise ValueError(f"{expected_track} output declares the wrong track")
    for field in ("context_id", "output_identity"):
        if not isinstance(payload.get(field), str) or not payload[field]:
            raise ValueError(f"{expected_track} output must declare a non-empty {field}")
    if not isinstance(payload.get("actual_paths_read"), list) or not all(
        isinstance(value, str) and value for value in payload["actual_paths_read"]
    ):
        raise ValueError(f"{expected_track} output must list actual_paths_read")
    if not isinstance(payload.get("external_urls_read"), list) or not all(
        isinstance(value, str) and value.startswith(("https://", "http://"))
        for value in payload["external_urls_read"]
    ):
        raise ValueError(f"{expected_track} output must list external_urls_read")
    if not isinstance(payload.get("decisions"), list):
        raise ValueError(f"{expected_track} output must contain decisions")
    if not isinstance(payload.get("ordered_candidate_ids"), list) or not all(
        isinstance(value, str) and value for value in payload["ordered_candidate_ids"]
    ):
        raise ValueError(f"{expected_track} output must contain ordered_candidate_ids")
    return payload


def _track_capacity(track: str, track_input: dict[str, object]) -> int:
    field = TRACK_CAPACITY_FIELDS[track]
    value = track_input.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{track} input must declare a numeric {field}")
    return value


def validate_track_output(
    payload: dict[str, object],
    expected_track: str,
    assigned: list[dict[str, object]],
    track_input: dict[str, object],
) -> dict[str, dict[str, object]]:
    run_dir = _managed_run_dir(Path(track_input["output_path"]).parents[2])
    expected_input = _input_path(run_dir, expected_track)
    expected_contracts = {
        name: path.resolve().as_posix()
        for name, path in _track_contract_paths(run_dir, expected_track).items()
    }
    expected_consensus = expected_contracts["consensus"]
    if payload.get("run_id") != track_input["run_id"]:
        raise ValueError(f"{expected_track} output run_id does not match its input")
    if payload.get("run_identity") != track_input["run_identity"]:
        raise ValueError(f"{expected_track} output run_identity does not match its input")
    if _project_path(payload.get("consensus_path", "")).as_posix() != expected_consensus:
        raise ValueError(f"{expected_track} output declares the wrong consensus path")
    if _project_path(payload.get("production_input_path", "")) != expected_input.resolve():
        raise ValueError(f"{expected_track} output declares the wrong production input path")
    for field, contract_name in (
        ("consensus_path", "consensus"),
        ("rating_skill_path", "rating_skill"),
        ("agent_prompt_path", "agent_prompt"),
    ):
        if _project_path(track_input.get(field, "")).as_posix() != expected_contracts[
            contract_name
        ]:
            raise ValueError(f"{expected_track} input declares the wrong {field}")

    allowed_paths = set(track_input["allowed_paths"])
    actual_paths = {_project_path(value).as_posix() for value in payload["actual_paths_read"]}
    unexpected_paths = sorted(actual_paths - allowed_paths)
    if unexpected_paths:
        raise ValueError(
            f"{expected_track} output read paths outside its isolated partition: "
            f"{unexpected_paths}"
        )
    required_paths = {
        expected_input.resolve().as_posix(),
        *expected_contracts.values(),
    }
    missing_required = sorted(required_paths - actual_paths)
    if missing_required:
        raise ValueError(
            f"{expected_track} output did not read required production paths: "
            f"{missing_required}"
        )

    assigned_by_id = {item["candidate_id"]: item for item in assigned}
    decisions: dict[str, dict[str, object]] = {}
    qualified_ids: set[str] = set()
    for index, raw in enumerate(payload["decisions"]):
        if not isinstance(raw, dict):
            raise ValueError(f"{expected_track} decision {index} must be an object")
        if set(raw) != DECISION_FIELDS:
            raise ValueError(
                f"{expected_track} decision {index} must use the exact bounded schema"
            )
        candidate_id = _candidate_id(raw, f"{expected_track} decision {index}")
        if candidate_id in decisions:
            raise ValueError(f"duplicate {expected_track} candidate_id: {candidate_id}")
        if candidate_id not in assigned_by_id:
            raise ValueError(f"{candidate_id} was not assigned to {expected_track}")
        category = raw.get("category")
        if category not in TRACK_CATEGORIES[expected_track]:
            raise ValueError(f"{candidate_id} leaks category {category!r} into {expected_track}")
        if category != assigned_by_id[candidate_id]["category"]:
            raise ValueError(f"{candidate_id} changed its shared frozen category")
        if not isinstance(raw.get("admission_passed"), bool) or not isinstance(
            raw.get("qualified"), bool
        ):
            raise ValueError(
                f"{candidate_id} admission_passed and qualified must be boolean"
            )
        if raw["qualified"] and not raw["admission_passed"]:
            raise ValueError(f"{candidate_id} cannot qualify after admission failure")
        if raw["qualified"]:
            qualified_ids.add(candidate_id)
        group_score = raw.get("group_score")
        if not _is_finite_number(group_score):
            raise ValueError(f"{candidate_id} group_score must be finite")
        if not 0 <= group_score <= 100:
            raise ValueError(f"{candidate_id} group_score must be within 0..100")
        components = raw.get("score_components")
        expected_components = SCORE_COMPONENTS[expected_track]
        if not isinstance(components, dict) or set(components) != set(expected_components):
            raise ValueError(f"{candidate_id} score_components use the wrong scale")
        for component, maximum in expected_components.items():
            value = components[component]
            if not _is_finite_number(value) or not 0 <= value <= maximum:
                raise ValueError(
                    f"{candidate_id} score component {component} must be within 0..{maximum}"
                )
        if round(float(group_score), 6) != round(
            sum(float(value) for value in components.values()), 6
        ):
            raise ValueError(f"{candidate_id} group_score must equal its score components")
        if raw.get("confidence") not in CONFIDENCE_VALUES:
            raise ValueError(f"{candidate_id} confidence must be high, medium, or low")
        if not isinstance(raw.get("decision_reasons"), list) or not raw[
            "decision_reasons"
        ] or not all(
            isinstance(value, str) and value for value in raw["decision_reasons"]
        ):
            raise ValueError(f"{candidate_id} decision_reasons must be non-empty strings")
        for field in ("comparison_reasons",):
            if not isinstance(raw.get(field), list) or not raw[field] or not all(
                isinstance(value, str) and value for value in raw[field]
            ):
                raise ValueError(f"{candidate_id} {field} must be non-empty strings")
        if not isinstance(raw.get("cutoff_reason"), str) or not raw["cutoff_reason"]:
            raise ValueError(f"{candidate_id} cutoff_reason must be non-empty")
        evidence_paths = raw.get("evidence_paths")
        if not isinstance(evidence_paths, list) or not evidence_paths or not all(
            isinstance(value, str) and value for value in evidence_paths
        ):
            raise ValueError(f"{candidate_id} evidence_paths must be non-empty strings")
        resolved_evidence = {_project_path(value).as_posix() for value in evidence_paths}
        candidate_evidence = {
            _project_path(assigned_by_id[candidate_id]["canonical_record_path"]).as_posix(),
            *(
                _project_path(value).as_posix()
                for value in assigned_by_id[candidate_id]["source_record_paths"]
            ),
        }
        if not resolved_evidence <= candidate_evidence:
            raise ValueError(
                f"{candidate_id} cites evidence outside that candidate's assigned evidence"
            )
        if not resolved_evidence <= actual_paths:
            raise ValueError(f"{candidate_id} cites evidence not present in actual_paths_read")
        decisions[candidate_id] = dict(raw)

    missing = sorted(set(assigned_by_id) - set(decisions))
    if missing:
        raise ValueError(f"{expected_track} output is missing assigned candidates: {missing}")

    ordered_ids = payload["ordered_candidate_ids"]
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ValueError(f"{expected_track} ordered_candidate_ids contains duplicates")
    if set(ordered_ids) != qualified_ids:
        raise ValueError(
            f"{expected_track} ordered_candidate_ids must exactly equal qualified decisions"
        )
    capacity = _track_capacity(expected_track, track_input)
    if len(ordered_ids) > capacity:
        raise ValueError(
            f"{expected_track} ordered output exceeds its frozen production capacity: "
            f"{len(ordered_ids)}>{capacity}"
        )
    target = track_input.get("track_target")
    if not isinstance(target, int) or isinstance(target, bool) or target < 0:
        raise ValueError(f"{expected_track} input must declare a numeric track_target")
    under_target_reason = payload.get("under_target_reason")
    if len(ordered_ids) < target:
        if not isinstance(under_target_reason, str) or not under_target_reason.strip():
            raise ValueError(
                f"{expected_track} output below its target needs under_target_reason"
            )
    elif under_target_reason is not None:
        raise ValueError(
            f"{expected_track} output reached its target and under_target_reason must be null"
        )
    return decisions


def _freeze_track_stage_artifacts(
    run_dir: Path,
    track: str,
    payload: dict[str, object],
) -> None:
    track_dir = run_dir / "tracks" / track
    decisions = payload["decisions"]
    common = {
        "schema_version": 3,
        "run_id": payload["run_id"],
        "track": track,
        "context_id": payload["context_id"],
        "output_identity": payload["output_identity"],
    }
    _immutable_write_json(
        track_dir / "context.json",
        {
            **common,
            "isolated_context": True,
            "actual_paths_read": payload["actual_paths_read"],
            "external_urls_read": payload["external_urls_read"],
        },
    )
    _immutable_write_json(
        track_dir / "triage.json",
        {
            **common,
            "decisions": [
                {
                    "candidate_id": item["candidate_id"],
                    "category": item["category"],
                    "admission_passed": item["admission_passed"],
                    "decision_reasons": item["decision_reasons"],
                    "evidence_paths": item["evidence_paths"],
                }
                for item in decisions
            ],
        },
    )
    _immutable_write_json(
        track_dir / "scoring.json",
        {
            **common,
            "score_scale": SCORE_SCALES[track],
            "decisions": [
                {
                    "candidate_id": item["candidate_id"],
                    "group_score": item["group_score"],
                    "score_components": item["score_components"],
                    "confidence": item["confidence"],
                }
                for item in decisions
            ],
        },
    )
    _immutable_write_json(
        track_dir / "comparison.json",
        {
            **common,
            "ordered_candidate_ids": payload["ordered_candidate_ids"],
            "under_target_reason": payload["under_target_reason"],
            "decisions": [
                {
                    "candidate_id": item["candidate_id"],
                    "comparison_reasons": item["comparison_reasons"],
                }
                for item in decisions
            ],
        },
    )
    _immutable_write_json(
        track_dir / "cutoff.json",
        {
            **common,
            "ordered_candidate_ids": payload["ordered_candidate_ids"],
            "decisions": [
                {
                    "candidate_id": item["candidate_id"],
                    "qualified": item["qualified"],
                    "cutoff_reason": item["cutoff_reason"],
                }
                for item in decisions
            ],
        },
    )


def _freeze_track_output(
    prepared: dict[str, object],
    track: str,
    track_input: dict[str, object],
    supplied_output: Path | None,
    *,
    freeze: bool = True,
) -> dict[str, object]:
    run_dir = prepared["frozen"]["run_dir"]
    output_path = _resolve_supplied_output(run_dir, track, supplied_output)
    payload = _load_track_output(output_path, track)
    decisions = validate_track_output(
        payload,
        track,
        prepared["partitions"][track],
        track_input,
    )
    terminal = {
        "schema_version": 1,
        "terminal_validated": True,
        "run_id": prepared["frozen"]["run_id"],
        "track": track,
        "output_path": output_path.as_posix(),
        "output_identity": payload["output_identity"],
        "selected_count": len(payload["ordered_candidate_ids"]),
        "validated_output": payload,
    }
    terminal_path = _terminal_path(run_dir, track)
    if freeze:
        _freeze_track_stage_artifacts(run_dir, track, payload)
        _immutable_write_json(terminal_path, terminal)
    return {
        "payload": payload,
        "decisions": decisions,
        "terminal": terminal,
        "terminal_path": terminal_path.as_posix(),
    }



def _group_candidate(
    assigned: dict[str, object],
    decision: dict[str, object],
    track: str,
    group_rank: int,
) -> dict[str, object]:
    return {
        "candidate_id": assigned["candidate_id"],
        "title": assigned.get("title", ""),
        "category": assigned["category"],
        "group_rank": group_rank,
        "group_score": decision["group_score"],
        "score_scale": SCORE_SCALES[track],
        "rating_track": track,
        "confidence": decision["confidence"],
        "decision_reasons": decision["decision_reasons"],
        "evidence_paths": decision["evidence_paths"],
        "canonical_record_path": assigned["canonical_record_path"],
        "source_record_paths": assigned["source_record_paths"],
        "source_urls": assigned["source_urls"],
        "canonical_url": assigned["canonical_url"],
        "normalized_arxiv_id": assigned["normalized_arxiv_id"],
        "source_identities": assigned["source_identities"],
    }


def _relative_project_path(path: Path) -> str:
    return path.resolve().relative_to(PROJECT_ROOT).as_posix()


def _activate_rating_run(
    frozen: dict[str, object],
    artifact_path: Path,
    manifest_path: Path,
    manifest: dict[str, object],
) -> None:
    current_path = RATING_ROOT / "current.json"
    index_path = RATING_ROOT / "activation_index.json"
    publishing_path = RATING_ROOT / "publishing.json"
    current = {
        "schema_version": 3,
        "publication_kind": "internal_rating_snapshot",
        "status": "complete",
        "cycle_id": frozen["cycle_id"],
        "rating_run_id": frozen["run_id"],
        "run_id": frozen["run_id"],
        "run_path": _relative_project_path(frozen["run_dir"]),
        "manifest_path": _relative_project_path(manifest_path),
        "grouped_selection_path": _relative_project_path(artifact_path),
        "selection_limit": frozen["selection_limit"],
        "selected_count": manifest["selected_count"],
        "activated_after_validation": True,
    }
    marker = {
        "schema_version": 3,
        "publication_kind": "internal_rating_snapshot",
        "status": "activating",
        "cycle_id": frozen["cycle_id"],
        "rating_run_id": frozen["run_id"],
        "manifest_path": current["manifest_path"],
        "grouped_selection_path": current["grouped_selection_path"],
    }
    with _activation_lock():
        if publishing_path.exists():
            existing = _load_json(publishing_path)
            if existing != marker:
                raise ValueError("another rating activation is already in progress")
        _atomic_replace_json(publishing_path, marker)
        activated = False
        try:
            previous_index: dict[str, object] = {}
            if index_path.exists():
                loaded = _load_json(index_path)
                if not isinstance(loaded, dict):
                    raise ValueError("rating activation index must be an object")
                previous_index = loaded
            active_cycles = previous_index.get("active_cycles", {})
            if not isinstance(active_cycles, dict):
                raise ValueError("rating activation index active_cycles must be an object")
            active_cycles = dict(active_cycles)
            active_cycles[frozen["cycle_id"]] = {
                "rating_run_id": frozen["run_id"],
                "manifest_path": current["manifest_path"],
                "grouped_selection_path": current["grouped_selection_path"],
            }
            index = {
                "schema_version": 3,
                "publication_kind": "internal_rating_snapshot",
                "active_cycles": active_cycles,
            }
            _atomic_replace_json(index_path, index)
            _atomic_replace_json(current_path, current)
            activated = True
        finally:
            if activated:
                publishing_path.unlink(missing_ok=True)


def _rating_manifest(prepared, artifact_path, validation, frozen_tracks, final_track_ids):
    frozen = prepared["frozen"]
    group_counts = {category: validation["groups"][category]["count"] for category in ("Paper", "News", "Policy")}
    tracks = {}
    for track in TRACK_CATEGORIES:
        result = frozen_tracks[track]
        payload = result["payload"]
        track_input = _track_input(prepared, track)
        tracks[track] = {
            "input_path": _input_path(frozen["run_dir"], track).as_posix(),
            "output_path": result["terminal"]["output_path"],
            "terminal_path": result["terminal_path"],
            "context_id": payload["context_id"],
            "output_identity": payload["output_identity"],
            "capacity": track_input[TRACK_CAPACITY_FIELDS[track]],
            "assessed_count": len(payload["decisions"]),
            "qualified_count": sum(item["qualified"] for item in payload["decisions"]),
            "selected_count": len(payload["ordered_candidate_ids"]),
            "final_selected_count": len(final_track_ids[track]),
            "under_target_reason": payload["under_target_reason"],
            "contract_files": track_input["contract_files"],
            "actual_paths_read": payload["actual_paths_read"],
            "external_urls_read": payload["external_urls_read"],
        }
    return {
        "schema_version": 3,
        "quota_contract": "three-track-v3",
        "publication_kind": "internal_rating_snapshot",
        "status": "complete",
        "run_id": frozen["run_id"],
        "rating_run_id": frozen["run_id"],
        "cycle_id": frozen["cycle_id"],
        "selection_limit": frozen["selection_limit"],
        **frozen["shared_reconciliation"],
        "partition_manifest_path": prepared["partition_manifest_path"],
        "grouped_selection_path": _relative_project_path(artifact_path),
        "selected_count": validation["candidate_count"],
        "group_counts": group_counts,
        "score_domains_comparable": False,
        "tracks": tracks,
        "coordinator": {
            "policy_selected_count": len(final_track_ids["policy"]),
            "news_final_capacity": 10 - len(final_track_ids["policy"]),
            "news_selected_count": len(final_track_ids["news"]),
            "paper_selected_count": len(final_track_ids["paper"]),
            "cross_track_score_comparison": False,
            "under_target_reasons": {
                track: frozen_tracks[track]["payload"]["under_target_reason"]
                for track in TRACK_CATEGORIES
            },
        },
        "validations": {
            "shared_reconciliation": True,
            "partition_exhaustive": True,
            "partition_disjoint": True,
            "grouped_candidates": validation,
        },
    }


def assemble_production(run_dir, output_paths=None):
    prepared = prepare_shared_partitions(run_dir)
    frozen = prepared["frozen"]
    output_paths = output_paths or {}
    frozen_tracks = {}
    track_inputs = {}
    context_ids = set()
    output_identities = set()
    for track in TRACK_CATEGORIES:
        track_input = _track_input(prepared, track)
        _require_exact_json(_input_path(frozen["run_dir"], track), track_input, f"{track} input; run prepare-track --track {track} first")
        result = _freeze_track_output(prepared, track, track_input, output_paths.get(track), freeze=True)
        context_id = result["payload"]["context_id"]
        output_identity = result["payload"]["output_identity"]
        if context_id in context_ids or output_identity in output_identities:
            raise ValueError("production track runs need distinct context/output identities")
        context_ids.add(context_id)
        output_identities.add(output_identity)
        track_inputs[track] = track_input
        frozen_tracks[track] = result
        if len(result["payload"]["ordered_candidate_ids"]) > track_input[TRACK_CAPACITY_FIELDS[track]]:
            raise ValueError(f"{track} output exceeds its isolated frozen capacity")

    news_policy_final_capacity = 10
    policy_ids = list(
        frozen_tracks["policy"]["payload"]["ordered_candidate_ids"][
            :min(3, news_policy_final_capacity)
        ]
    )
    news_final_capacity = news_policy_final_capacity - len(policy_ids)
    news_ids = list(
        frozen_tracks["news"]["payload"]["ordered_candidate_ids"][:news_final_capacity]
    )
    paper_final_capacity = 10
    paper_ids = list(
        frozen_tracks["paper"]["payload"]["ordered_candidate_ids"][
            :max(0, paper_final_capacity)
        ]
    )
    final_track_ids = {"paper": paper_ids, "news": news_ids, "policy": policy_ids}
    assigned_by_id = {str(item["candidate_id"]): item for track in TRACK_CATEGORIES for item in prepared["partitions"][track]}
    decisions_by_id = {str(item["candidate_id"]): item for track in TRACK_CATEGORIES for item in frozen_tracks[track]["decisions"].values()}
    category_by_track = {"paper": "Paper", "news": "News", "policy": "Policy"}
    groups = {}
    for track in TRACK_CATEGORIES:
        grouped = []
        for group_rank, candidate_id in enumerate(final_track_ids[track], start=1):
            grouped.append(_group_candidate(assigned_by_id[candidate_id], decisions_by_id[candidate_id], track, group_rank))
        groups[category_by_track[track]] = {"candidates": grouped}

    artifact = {
        "schema_version": 3,
        "quota_contract": "three-track-v3",
        "production_input": True,
        "production_artifact": True,
        "cycle_id": frozen["cycle_id"],
        "rating_run_id": frozen["run_id"],
        "selection_limit": frozen["selection_limit"],
        "score_domains_comparable": False,
        "track_consensus": {track: {"id": SCORE_SCALES[track], **track_inputs[track]["contract_files"]["consensus"]} for track in TRACK_CATEGORIES},
        "coordinator": {
            "policy_selected_count": len(policy_ids),
            "news_final_capacity": news_final_capacity,
            "news_selected_count": len(news_ids),
            "paper_selected_count": len(paper_ids),
            "cross_track_score_comparison": False,
            "under_target_reasons": {
                track: frozen_tracks[track]["payload"]["under_target_reason"]
                for track in TRACK_CATEGORIES
            },
        },
        "groups": groups,
    }
    validation = validate_grouped_candidates(artifact)
    if not validation["valid"]:
        raise ValueError(f"grouped production artifact is invalid: {validation}")
    artifact_path = frozen["run_dir"] / "grouped_selection.json"
    _immutable_write_json(artifact_path, artifact)
    manifest = _rating_manifest(prepared, artifact_path, validation, frozen_tracks, final_track_ids)
    manifest_path = frozen["run_dir"] / "manifest.json"
    _immutable_write_json(manifest_path, manifest)
    report = {
        "schema_version": 3,
        "quota_contract": "three-track-v3",
        "production_input": True,
        "valid": True,
        "run_id": frozen["run_id"],
        "partition_manifest_path": prepared["partition_manifest_path"],
        "track_outputs": {
            track: {
                "context_id": frozen_tracks[track]["payload"]["context_id"],
                "output_identity": frozen_tracks[track]["payload"]["output_identity"],
                "selected_count": len(frozen_tracks[track]["payload"]["ordered_candidate_ids"]),
                "final_selected_count": len(final_track_ids[track]),
                "terminal_path": frozen_tracks[track]["terminal_path"],
            }
            for track in TRACK_CATEGORIES
        },
        "score_domains_comparable": False,
        "manifest_path": _relative_project_path(manifest_path),
        "grouped_selection_path": _relative_project_path(artifact_path),
        "grouped_validation": validation,
    }
    _immutable_write_json(frozen["run_dir"] / "production_partition_report.json", report)
    _activate_rating_run(frozen, artifact_path, manifest_path, manifest)
    return report


def _parser():
    parser = argparse.ArgumentParser(description="Run the staged three-track production rating coordinator.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare-track", help="freeze one isolated Paper/News/Policy input")
    prepare.add_argument("--run-dir", type=Path, required=True)
    prepare.add_argument("--owner", required=True)
    prepare.add_argument("--track", choices=tuple(TRACK_CATEGORIES), required=True)
    assemble = subparsers.add_parser("assemble", help="validate all track outputs and assemble fixed groups")
    assemble.add_argument("--run-dir", type=Path, required=True)
    assemble.add_argument("--owner", required=True)
    assemble.add_argument("--paper-output", type=Path)
    assemble.add_argument("--news-output", type=Path)
    assemble.add_argument("--policy-output", type=Path)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.command == "prepare-track":
        result = rating_command(args, lambda: prepare_track(args.run_dir, args.track))
    else:
        result = rating_command(args, lambda: assemble_production(args.run_dir, {"paper": args.paper_output, "news": args.news_output, "policy": args.policy_output}))
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
