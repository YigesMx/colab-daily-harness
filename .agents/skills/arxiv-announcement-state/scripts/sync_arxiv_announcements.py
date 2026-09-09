#!/usr/bin/env python3
"""Synchronize arXiv announcement feeds into prepare records and local state."""

from __future__ import annotations

import argparse
import sys
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from colab_daily.config import Config
from colab_daily.lifecycle import Lifecycle, aware
from colab_daily.storage import StorageError, digest
from colab_daily.storage.metadata import control_metadata, EVENT_FIELDS
from colab_daily.storage.files import atomic_write as durable_write, canonical, private_dir, read_bytes, relative_name, safe_path, sha256, sync_dir
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RSS_BASE = "https://rss.arxiv.org/atom/"
API_URL = "https://export.arxiv.org/api/query"
ATOM_NS = "http://www.w3.org/2005/Atom"
ARXIV_NS = "http://arxiv.org/schemas/atom"
NS = {"a": ATOM_NS, "x": ARXIV_NS}
RETENTION_DAYS = 30
REQUEST_TIMEOUT = 60
USER_AGENT = "colab-daily-arxiv-announcement-state/0.1"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--display-date")
    parser.add_argument("--since", type=parse_datetime)
    parser.add_argument("--until", type=parse_datetime)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--retry-uncommitted", action="store_true", help="explicitly rerun only after SQLite proves no batch commit; preserve old staging")
    parser.add_argument("--consensus", type=Path)
    parser.add_argument("--retention-days", type=int, default=RETENTION_DAYS)
    return parser.parse_args()


def parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include an offset")
    return parsed.astimezone(timezone.utc)




def normalize_id(value: str) -> tuple[str, str]:
    match = re.search(r"(?:arxiv(?:\.org)?[/:])?(\d{4}\.\d{4,5})(?:v(\d+))?", value)
    if not match:
        raise ValueError(f"unsupported arXiv identifier: {value}")
    return match.group(1), f"v{match.group(2) or '1'}"


def read_keywords(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    text = path.read_text(encoding="utf-8")
    categories = sorted(set(re.findall(r"`([a-z]+\.[A-Z]{2})`", text)))
    groups: dict[str, list[str]] = {}
    active = False
    for line in text.splitlines():
        line = line.strip()
        if line == "### 关键词组":
            active = True
            continue
        if active and line.startswith("### "):
            break
        if active:
            match = re.match(r"- `([^`]+)`[:：](.+)", line)
            if match:
                groups[match.group(1)] = re.findall(r"`([^`]+)`", match.group(2))
    if not categories or not groups:
        raise ValueError("consensus does not contain arXiv categories and keyword groups")
    return categories, groups


def matched_consensus_groups(row: dict[str, Any], groups: dict[str, list[str]]) -> dict[str, list[str]]:
    title = row["title"]
    body = f"{title}\n{row['summary']}"
    matched: dict[str, list[str]] = {}
    for group, terms in groups.items():
        hits = []
        for term in terms:
            if len(term) <= 4 and term.isascii() and term.isalpha():
                found = re.search(rf"\b{re.escape(term)}\b", body, re.I)
            else:
                found = term.lower() in body.lower()
            if found:
                hits.append(term)
        if group != "llm_vlm_methods" and hits:
            matched[group] = hits
        if group == "llm_vlm_methods" and (
            any(term.lower() in title.lower() for term in hits) or len(set(hits)) >= 2
        ):
            matched[group] = hits
    return matched


def fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/atom+xml"})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read()


def feed_rows(categories: list[str], groups: dict[str, list[str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: dict[str, dict[str, Any]] = {}
    reports = []
    for category in categories:
        url = f"{RSS_BASE}{category}"
        report: dict[str, Any] = {"category": category, "url": url, "status": "failed", "entry_count": 0, "error": None}
        try:
            root = ET.fromstring(fetch(url))
            for entry in root.findall("a:entry", NS):
                raw_id = entry.findtext("a:id", "", NS)
                source_id, version = normalize_id(raw_id)
                announce_type = entry.findtext("x:announce_type", "", NS)
                family = "new_or_cross" if announce_type in {"new", "cross"} else "replacement"
                key = f"{source_id}:{family}"
                row = {
                    "source_id": source_id,
                    "version": version,
                    "announce_type": announce_type,
                    "announce_types": [announce_type],
                    "announcement_at": entry.findtext("a:published", "", NS),
                    "feed_updated_at": root.findtext("a:updated", "", NS),
                    "title": " ".join(entry.findtext("a:title", "", NS).split()),
                    "summary": " ".join(entry.findtext("a:summary", "", NS).split()),
                    "source_categories": [category],
                    "abstract_url": f"https://arxiv.org/abs/{source_id}{version}",
                }
                row["matched_query_groups"] = matched_consensus_groups(row, groups)
                if key in rows:
                    rows[key]["source_categories"] = sorted(set(rows[key]["source_categories"] + [category]))
                    rows[key]["announce_types"] = sorted(set(rows[key]["announce_types"] + [announce_type]))
                    if rows[key]["announce_type"] == "cross" and announce_type == "new":
                        rows[key]["announce_type"] = "new"
                else:
                    rows[key] = row
            report["status"] = "success"
            report["entry_count"] = len(root.findall("a:entry", NS))
        except Exception as exc:
            report["error"] = str(exc).splitlines()[0][:240]
        reports.append(report)
        time.sleep(1)
    return list(rows.values()), reports


def state_key(row: dict[str, Any]) -> str:
    family = "new_or_cross" if row["announce_type"] in {"new", "cross", "new_or_cross"} else "replacement"
    return f"{row['source_id']}:{family}"


def in_window(row: dict[str, Any], since: datetime, until: datetime) -> bool:
    value = datetime.fromisoformat(row["announcement_at"].replace("Z", "+00:00")).astimezone(timezone.utc)
    return since <= value < until


def build_record(row: dict[str, Any], metadata: dict[str, Any], args: argparse.Namespace) -> str:
    title = metadata.get("title") or row["title"]
    summary = metadata.get("summary") or row["summary"]
    authors = metadata.get("authors", [])
    categories = metadata.get("categories", row["source_categories"])
    lines = [
        "---",
        "source: arxiv",
        "category_hint: Paper",
        "content_status: substantial",
        "source_role: primary",
        f"source_id: {json.dumps(row['source_id'])}",
        f"version: {json.dumps(metadata.get('version', row['version']))}",
        f"announce_type: {json.dumps(row['announce_type'])}",
        f"announce_types: {json.dumps(row['announce_types'])}",
        f"announcement_at: {json.dumps(row['announcement_at'])}",
        f"title: {json.dumps(title, ensure_ascii=False)}",
        f"authors: {json.dumps(authors, ensure_ascii=False)}",
        f"published_at: {json.dumps(metadata.get('published_at', '') )}",
        f"updated_at: {json.dumps(metadata.get('updated_at', '') )}",
        f"retrieved_at: {json.dumps(datetime.now(timezone.utc).isoformat())}",
        f"window_since: {json.dumps(args.since.isoformat())}",
        f"window_until: {json.dumps(args.until.isoformat())}",
        f"primary_category: {json.dumps(metadata.get('primary_category'))}",
        f"categories: {json.dumps(categories, ensure_ascii=False)}",
        f"source_categories: {json.dumps(row['source_categories'])}",
        f"matched_query_groups: {json.dumps(sorted(row.get('matched_query_groups', {})), ensure_ascii=False)}",
        f"abstract_url: {json.dumps(row['abstract_url'])}",
        f"pdf_url: {json.dumps(metadata.get('pdf_url'))}",
        "status: complete",
        "attachments: []",
        "errors: []",
        "---",
        "",
        f"# {title}",
        "",
        "## Authors",
        "",
        ", ".join(authors) or "Unknown",
        "",
        "## Abstract",
        "",
        summary,
        "",
        "## Source",
        "",
        f"- Abstract: {row['abstract_url']}",
        "",
        "## Announcement",
        "",
        f"- Type: {row['announce_type']}",
        f"- Announced at: {row['announcement_at']}",
        f"- Categories: {', '.join(categories)}",
    ]
    return "\n".join(lines) + "\n"


def api_metadata(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Enrich RSS events with authoritative arXiv API metadata in bounded batches."""
    result: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(rows), 50):
        batch = rows[offset : offset + 50]
        query = urlencode({"id_list": ",".join(row["source_id"] for row in batch), "max_results": len(batch)})
        root = ET.fromstring(fetch(f"{API_URL}?{query}"))
        for entry in root.findall("a:entry", NS):
            source_id, version = normalize_id(entry.findtext("a:id", "", NS))
            category_nodes = entry.findall("a:category", NS)
            categories = [node.attrib.get("term", "") for node in category_nodes if node.attrib.get("term")]
            primary = entry.find("x:primary_category", NS)
            links = entry.findall("a:link", NS)
            pdf = next((link.attrib.get("href") for link in links if link.attrib.get("title") == "pdf"), None)
            result[source_id] = {
                "version": version,
                "title": " ".join(entry.findtext("a:title", "", NS).split()),
                "summary": " ".join(entry.findtext("a:summary", "", NS).split()),
                "authors": [node.findtext("a:name", "", NS).strip() for node in entry.findall("a:author", NS)],
                "categories": categories,
                "primary_category": primary.attrib.get("term") if primary is not None else (categories[0] if categories else None),
                "published_at": entry.findtext("a:published", "", NS),
                "updated_at": entry.findtext("a:updated", "", NS),
                "pdf_url": pdf,
            }
        missing = [row["source_id"] for row in batch if row["source_id"] not in result]
        if missing:
            raise RuntimeError(f"arXiv API metadata missing for {len(missing)} announced IDs")
        if offset + 50 < len(rows):
            time.sleep(3)
    return result




def local_batch(life, receipt):
    """Validate fsynced temporary material; SQLite contains only its receipt."""
    control_metadata(receipt)
    if (len(receipt.get("paths", [])) != 1
            or set(receipt.get("identity", {})) != {"input_sha256", "operation"}
            or set(receipt.get("checksums", {})) != {"update", "manifest"}
            or set(receipt.get("counts", {})) != {"expected_revision", "records"}):
        raise StorageError("invalid arXiv control receipt shape")
    relative_name(receipt["paths"][0])
    root = life.workspace_path(life.working / receipt["paths"][0])
    if root.parent != life.working or not root.name.startswith(".arxiv-batch-"):
        raise StorageError("invalid arXiv staging receipt path")
    output = life.workspace_path(life.working / "arxiv")
    if not output.exists():
        output = life.workspace_path(root / "output")
    update_path = root / "update.json"
    manifest_path = output / "crawl_manifest.json"
    if not update_path.is_file() or not manifest_path.is_file():
        raise StorageError("temporary arXiv batch missing; no document mirror exists in SQLite")
    if sha256(read_bytes(update_path)) != receipt["checksums"]["update"] or sha256(read_bytes(manifest_path)) != receipt["checksums"]["manifest"]:
        raise StorageError("temporary arXiv control files changed")
    update = json.loads(read_bytes(update_path))
    manifest = json.loads(read_bytes(manifest_path))
    expected = manifest["record_sha256"]
    actual = {}
    for path in output.rglob("*"):
        safe_path(path)
        if path.is_file() and path != manifest_path:
            actual[path.relative_to(output).as_posix()] = sha256(read_bytes(path))
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    if actual != expected or len(actual) != manifest["emitted_record_count"]:
        raise StorageError("temporary arXiv manifest/record set is inconsistent")
    if (update["expected_revision"] != receipt["counts"]["expected_revision"]
            or update["input_sha256"] != receipt["identity"]["input_sha256"]
            or manifest["source_operation"] != receipt["identity"]["operation"]
            or manifest["source_revision"] != update["expected_revision"] + 1):
        raise StorageError("arXiv receipt/manifest/CAS identities differ")
    for path in (manifest_path, update_path):
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    sync_dir(output)
    sync_dir(root)
    sync_dir(life.working)  # parent entry durability is required on recovery too
    return root, output, update


def materialize_batch(life, receipt):
    root, output, _ = local_batch(life, receipt)
    final = life.working / "arxiv"
    if output != final:
        os.rename(output, final)
        sync_dir(life.working)


def commit_batch(life, args, run_id, receipt):
    # File verification/fsync BEFORE even the emitted CAS. The same local bytes
    # are retained after a commit whose result/rename might be interrupted.
    _, output, update = local_batch(life, receipt)
    run = life.store.run_state(run_id)
    manifest = json.loads(read_bytes(output / "crawl_manifest.json"))
    operation = receipt["identity"]["operation"]
    if (operation != "arxiv-batch:" + args.cycle_id
            or receipt["identity"]["input_sha256"] != run["context"]["identity"]["input_sha256"]
            or manifest["cycle_id"] != args.cycle_id
            or manifest["window_since"] != args.since.isoformat()
            or manifest["window_until"] != args.until.isoformat()):
        raise StorageError("source operation does not match frozen cycle/window identity")
    if manifest["status"] != "success":
        raise StorageError("partial arXiv feeds require explicit uncommitted retry; no emitted batch committed")
    old = run["stages"].get("batch")
    revision = old["revision"] if old else 0
    saved = life.store.source_batch("arxiv", operation)
    if saved is None:
        revision = life.store.checkpoint_stage(run_id, args.owner, "batch", f"intent:{revision}", revision, "running", receipt)
        # Recheck after committing intent as well: a missing/changed file at this
        # boundary must never advance emitted or the cursor.
        _, _, update = local_batch(life, receipt)
        try:
            life.store.update_source_state("arxiv", operation, update["expected_revision"], update["cursor"], update["events"], receipt=receipt)
        except Exception:
            # An uncertain SQL result is resolved by source_batch on retry. Never
            # infer absence or repeat emitted transitions from a missing temp flag.
            raise
    elif saved["receipt"] != receipt or saved["revision"] != update["expected_revision"] + 1:
        raise StorageError("committed arXiv result differs from local receipt")
    materialize_batch(life, receipt)
    current = life.store.run_state(run_id)["stages"].get("batch")
    if current and current["status"] == "complete":
        if current["payload"] != receipt:
            raise StorageError("arXiv terminal receipt changed")
        return 0
    revision = current["revision"] if current else 0
    life.store.checkpoint_stage(run_id, args.owner, "batch", f"complete:{revision}", revision, "complete", receipt)
    return 0


def event_identity(event):
    return {key: value for key, value in event.items() if key in EVENT_FIELDS}


def run_sync(args, life, categories, groups):
    if type(args.retention_days) is not int or args.retention_days < 14:
        raise StorageError("retention-days must be an integer of at least 14")
    with life.use(args.owner, args.cycle_id) as context:
        frozen = context["input"]
        if args.display_date is not None and args.display_date != frozen["display_date"]:
            raise StorageError("arXiv display date differs from frozen cycle")
        if (args.since is None) != (args.until is None):
            raise StorageError("both arXiv window endpoints are required")
        if args.since is not None and (aware(args.since) != aware(frozen["window_since"]) or aware(args.until) != aware(frozen["window_until"])):
            raise StorageError("arXiv window differs from frozen cycle")
        if args.output_dir is not None and life.workspace_path(args.output_dir) != life.working:
            raise StorageError("arXiv output must use the single owned workspace root")
        args.display_date = frozen["display_date"]
        args.since, args.until = aware(frozen["window_since"]), aware(frozen["window_until"])
        input_hash = digest({"cycle_id": args.cycle_id, "display_date": args.display_date,
                             "since": args.since.isoformat(), "until": args.until.isoformat(),
                             "retention_days": args.retention_days, "categories": categories, "groups": groups})
        run_id = args.cycle_id + ":source:arxiv"
        operation = "arxiv-batch:" + args.cycle_id
        life.store.claim_run(args.cycle_id, run_id, "source:arxiv", args.owner,
                             {"identity": {"cycle_id": args.cycle_id, "input_sha256": input_hash}})
        saved = life.store.source_batch("arxiv", operation)
        if saved is not None:
            receipt = saved["receipt"]
            if not receipt or receipt["identity"]["input_sha256"] != input_hash:
                raise StorageError("arXiv committed batch identity changed")
            return commit_batch(life, args, run_id, receipt)
        old = life.store.run_state(run_id)["stages"].get("batch")
        revision = old["revision"] if old else 0
        root = life.workspace_path(life.working / (".arxiv-batch-" + digest({"cycle": args.cycle_id, "attempt": revision})))
        if old:
            root = life.workspace_path(life.working / old["payload"]["paths"][0])
        local_receipt = root / "receipt.json"
        if old or root.exists():
            if getattr(args, "retry_uncommitted", False):
                # The preceding source_batch lookup proved no emitted commit.
                # Keep old material on disk; this flag never overrides a commit.
                if (life.working / "arxiv").exists():
                    raise StorageError("uncommitted visible arXiv output exists; preserve it")
                if root.exists():
                    archive = life.workspace_path(life.working / ".arxiv-attempts" / str(revision))
                    if archive.exists():
                        raise StorageError("arXiv attempt archive exists")
                    private_dir(archive.parent)
                    os.rename(root, archive)
                    sync_dir(archive.parent)
                metadata = {"identity": {"input_sha256": input_hash}, "paths": [root.relative_to(life.working).as_posix()],
                            "reason": "explicit rerun after verified absence of source commit"}
                revision = life.store.checkpoint_stage(run_id, args.owner, "batch", f"retry:{revision}", revision, "failed", metadata)
                root = life.working / (".arxiv-batch-" + digest({"cycle": args.cycle_id, "attempt": revision}))
            else:
                if not local_receipt.is_file():
                    raise StorageError("interrupted temporary arXiv material missing; explicit uncommitted retry required")
                receipt = json.loads(read_bytes(local_receipt))
                if receipt["identity"]["input_sha256"] != input_hash:
                    raise StorageError("temporary arXiv input identity changed")
                return commit_batch(life, args, run_id, receipt)
        if (life.working / "arxiv").exists():
            raise StorageError("source output exists without a SQLite batch commit")
        state = life.store.source_state("arxiv")
        old_events = {event["key"]: event for event in state["events"]}
        rows, reports = feed_rows(categories, groups)
        successful = [report for report in reports if report["status"] == "success"]
        if not successful:
            raise StorageError("all arXiv feeds failed; source state unchanged")
        pending = {e["source_id"]: e for e in old_events.values() if e.get("status") == "pending" and e.get("announce_type") in {"new", "cross"}}
        current = [row for row in rows if row["announce_type"] in {"new", "cross"} and in_window(row, args.since, args.until)]
        ids = {row["source_id"] for row in current}
        for source_id, event in pending.items():
            if source_id not in ids:
                current.append({**event_identity(event), "source_id": source_id, "title": source_id, "summary": "",
                                "announce_types": event.get("announce_types", [event["announce_type"]]),
                                "source_categories": event.get("source_categories", []), "matched_query_groups": {},
                                "abstract_url": f"https://arxiv.org/abs/{source_id}{event['version']}"})
        current = [row for row in current if old_events.get(state_key(row), {}).get("status") != "emitted"]
        stamp = datetime.now(timezone.utc).isoformat()
        try:
            metadata = api_metadata(current)
            if set(metadata) != {row["source_id"] for row in current}:
                raise StorageError("metadata did not cover current announcements")
        except Exception:
            events = [event_identity({**row, "key": state_key(row), "status": "pending", "last_seen_at": stamp,
                                      "first_seen_at": old_events.get(state_key(row), {}).get("first_seen_at", stamp)}) for row in current]
            life.store.update_source_state("arxiv", f"arxiv-pending:{args.cycle_id}:{state['revision']}", state["revision"], state["cursor"], events)
            raise StorageError("arXiv metadata unavailable; identity-only pending state saved, no emitted batch") from None
        output = private_dir(root / "output")
        hashes = {}
        emitted = set()
        for row in current:
            source_id = row["source_id"]
            if not re.fullmatch(r"\d{4}\.\d{4,5}", source_id):
                raise StorageError("invalid arXiv record directory identity")
            name = f"{source_id}/record.md"
            content = build_record(row, metadata[source_id], args).encode()
            durable_write(output / name, content, immutable=True)
            hashes[name] = sha256(content)
            emitted.add(state_key(row))
        changes = {}
        for row in rows + current:
            key = state_key(row)
            previous = old_events.get(key, {})
            if previous.get("status") == "emitted":
                continue
            changes[key] = event_identity({**row, "key": key, "first_seen_at": previous.get("first_seen_at", stamp),
                                            "last_seen_at": stamp, "status": "emitted" if key in emitted else previous.get("status", "seen"),
                                            "cycle_id": args.cycle_id if key in emitted else previous.get("cycle_id")})
        manifest = {"source": "arxiv", "category_hint": "Paper", "content_status": "substantial", "source_role": "primary",
                    "status": "success" if len(successful) == len(categories) else "partial", "cycle_id": args.cycle_id,
                    "window_since": args.since.isoformat(), "window_until": args.until.isoformat(), "feed_reports": reports,
                    "announcement_count": len(rows), "new_or_cross_count": sum(r["announce_type"] in {"new", "cross"} for r in rows),
                    "replacement_count": sum(r["announce_type"] in {"replace", "replace-cross"} for r in rows),
                    "current_window_count": sum(in_window(r, args.since, args.until) for r in current),
                    "recovered_pending_count": sum(r["source_id"] in pending for r in current),
                    "pending_count": sum(e.get("status") == "pending" for e in {**old_events, **changes}.values()),
                    "emitted_record_count": len(current), "record_directories": [r["source_id"] for r in current],
                    "record_sha256": hashes, "state_backend": "sqlite", "source_revision": state["revision"] + 1, "source_operation": operation}
        update = {"cursor": {**state["cursor"], "schema_version": 1, "retention_days": args.retention_days, "last_successful_sync_at": stamp},
                  "events": list(changes.values()), "expected_revision": state["revision"], "input_sha256": input_hash}
        durable_write(output / "crawl_manifest.json", (canonical(manifest) + "\n").encode(), immutable=True)
        durable_write(root / "update.json", (canonical(update) + "\n").encode(), immutable=True)
        receipt = {"identity": {"input_sha256": input_hash, "operation": operation}, "paths": [root.relative_to(life.working).as_posix()],
                   "checksums": {"manifest": sha256(read_bytes(output / "crawl_manifest.json")), "update": sha256(read_bytes(root / "update.json"))},
                   "counts": {"expected_revision": state["revision"], "records": len(current)}}
        durable_write(root / "receipt.json", (canonical(receipt) + "\n").encode(), immutable=True)
        sync_dir(root)
        sync_dir(life.working)
        if manifest["status"] != "success":
            pending_events = [event_identity({**row, "key": state_key(row), "status": "pending", "last_seen_at": stamp,
                                              "first_seen_at": old_events.get(state_key(row), {}).get("first_seen_at", stamp)})
                              for row in current]
            life.store.update_source_state("arxiv", f"arxiv-partial:{args.cycle_id}:{state['revision']}",
                                           state["revision"], state["cursor"], pending_events)
            raise StorageError("partial arXiv feeds retained; explicit uncommitted retry required")
        return commit_batch(life, args, run_id, receipt)


def main():
    args = parse_args()
    life = Lifecycle(Config.load(args.project_root))
    categories, groups = read_keywords(life.project_path(args.consensus or "consensus.md"))
    try:
        return run_sync(args, life, categories, groups)
    except (StorageError, OSError):
        print("arxiv: ownership/CAS/local-material check failed; inspect private metadata", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
