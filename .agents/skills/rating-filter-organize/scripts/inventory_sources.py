#!/usr/bin/env python3
"""Build a deterministic source inventory without making semantic decisions."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit


SCALAR_METADATA_FIELDS = (
    "category_hint",
    "source_category_hint",
    "content_status",
    "source_role",
)
ALLOWED_SOURCE_STATUSES = frozenset(
    {
        "success",
        "success_empty",
        "success_stale",
        "partial",
        "skipped",
        "failed",
        "blocked",
        "not_yet_published",
    }
)
MISSING_MANIFEST_STATUSES = frozenset(
    {"skipped", "failed", "blocked", "not_yet_published"}
)
MAX_RECEIPT_TEXT_LENGTH = 2000
SOURCE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from colab_daily.inventory_adapter import inventory_command
from colab_daily.config import Config

PROJECT_ROOT = Config.load().project_root
RECEIPT_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "cycle_id",
        "window_since",
        "window_until",
        "discovered_count",
        "completed_count",
        "record_count",
        "status_counts",
        "sources",
    }
)
RECEIPT_SOURCE_FIELDS = frozenset(
    {
        "skill",
        "source_name",
        "source_directory",
        "status",
        "error",
        "detail",
        "record_count",
        "error_count",
        "cycle_id",
        "window_since",
        "window_until",
        "manifest_path",
        "record_paths",
    }
)
ARXIV_ID_RE = re.compile(r"^(\d{4}\.\d{4,5})(?:v\d+)?$")
ARXIV_URL_RE = re.compile(
    r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf|src)/(\d{4}\.\d{4,5})(?:v\d+)?/?$",
    re.IGNORECASE,
)


def front_matter(text: str) -> dict[str, object]:
    if not text.startswith("---\n"):
        raise ValueError("record is missing YAML front matter")
    block = text.split("\n---\n", 1)[0][4:]
    values: dict[str, object] = {}
    for line in block.splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not match:
            continue
        key, raw = match.groups()
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            try:
                values[key] = json.loads(raw)
            except json.JSONDecodeError:
                values[key] = raw
        elif raw.startswith("[") or raw.startswith("{"):
            # Inventory only needs scalar front-matter values. Lists and maps
            # remain available in the source record, but are not flattened.
            continue
        elif raw == "null":
            values[key] = None
        elif raw == "true":
            values[key] = True
        elif raw == "false":
            values[key] = False
        elif raw == "":
            values[key] = ""
        else:
            values[key] = raw
    return values


def _plain_object(value: object, location: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a JSON object")
    return value


def _non_empty_text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a non-empty string")
    return value


def _required_fields(
    value: dict[str, object], required: frozenset[str], location: str
) -> None:
    missing = sorted(required - value.keys())
    if missing:
        raise ValueError(f"{location} is missing required fields: {missing}")


def _non_negative_int(value: object, location: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError(f"{location} must be a non-negative integer")
    return value


def _bounded_text(value: object, location: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{location} must be text or null")
    if len(value) > MAX_RECEIPT_TEXT_LENGTH:
        raise ValueError(
            f"{location} exceeds {MAX_RECEIPT_TEXT_LENGTH} characters"
        )
    return value


def _bounded_non_empty_text(value: object, location: str) -> str:
    text = _non_empty_text(value, location)
    if len(text) > MAX_RECEIPT_TEXT_LENGTH:
        raise ValueError(
            f"{location} exceeds {MAX_RECEIPT_TEXT_LENGTH} characters"
        )
    return text


def _aware_datetime(value: object, location: str) -> tuple[str, datetime]:
    text = _non_empty_text(value, location)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{location} must be an ISO 8601 datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{location} must include a timezone offset")
    return text, parsed


def _reject_symlinks(path: Path, boundary: Path, location: str) -> None:
    try:
        relative = path.relative_to(boundary)
    except ValueError as error:
        raise ValueError(f"{location} escapes project root") from error
    current = boundary
    if current.is_symlink():
        raise ValueError(f"{location} uses a symlinked project root")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{location} must not use symlinks: {current}")


def _project_path(
    value: object,
    *,
    project_root: Path,
    location: str,
    require_file: bool = False,
    require_directory: bool = False,
) -> Path:
    text = value.as_posix() if isinstance(value, Path) else _non_empty_text(value, location)
    raw = Path(text)
    if ".." in raw.parts:
        raise ValueError(f"{location} must not contain parent traversal")
    path = (raw if raw.is_absolute() else project_root / raw).absolute()
    _reject_symlinks(path, project_root, location)
    if require_file and not path.is_file():
        raise FileNotFoundError(f"{location} is not a file: {path}")
    if require_directory and not path.is_dir():
        raise FileNotFoundError(f"{location} is not a directory: {path}")
    return path


def _crawl_path(
    value: object,
    *,
    project_root: Path,
    crawl_root: Path,
    location: str,
    require_file: bool = False,
    require_directory: bool = False,
) -> Path:
    path = _project_path(
        value,
        project_root=project_root,
        location=location,
        require_file=require_file,
        require_directory=require_directory,
    )
    try:
        path.relative_to(crawl_root)
    except ValueError as error:
        raise ValueError(f"{location} must be under crawl root") from error
    return path


def _read_json_object(path: Path, location: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{location} is not valid JSON: {path}") from error
    return _plain_object(value, location)


def _source_directory(value: object, location: str) -> str:
    directory = _non_empty_text(value, location)
    if not SOURCE_NAME_RE.fullmatch(directory) or directory in {".", ".."}:
        raise ValueError(f"{location} must be a single safe directory name")
    return directory


def production_source_receipt_path(
    cycle_id: str, *, project_root: Path = PROJECT_ROOT
) -> Path:
    safe_cycle_id = _source_directory(cycle_id, "cycle_id")
    return (
        project_root
        / "working_tmp"
        / ".phase_prepare_candidates"
        / f"source-discovery-{safe_cycle_id}.json"
    ).absolute()


def validate_production_source_receipt_path(
    value: object,
    *,
    cycle_id: str,
    project_root: Path = PROJECT_ROOT,
    crawl_root: Path,
) -> Path:
    canonical_crawl_root = (project_root / "working_tmp").absolute()
    if crawl_root.absolute() != canonical_crawl_root:
        raise ValueError("production crawl root must be the project working_tmp directory")
    receipt_path = _crawl_path(
        value,
        project_root=project_root,
        crawl_root=canonical_crawl_root,
        location="source discovery receipt",
        require_file=True,
    )
    expected = production_source_receipt_path(cycle_id, project_root=project_root)
    if receipt_path != expected:
        raise ValueError(
            "source discovery receipt must be "
            "working_tmp/.phase_prepare_candidates/source-discovery-<CYCLE_ID>.json"
        )
    return receipt_path


def discover_source_skills(project_root: Path = PROJECT_ROOT) -> list[str]:
    skills_root = project_root / ".agents" / "skills" / "sources"
    if not skills_root.is_dir() or skills_root.is_symlink():
        raise ValueError("project source skills directory is missing or unsafe")
    skills = []
    for child in sorted(skills_root.iterdir(), key=lambda path: path.name):
        if child.is_dir() and not child.is_symlink() and (child / "SKILL.md").is_file():
            skills.append(child.name)
    return skills


def _text_value(value: object | None, default: str | None = None) -> str | None:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _record_metadata(fields: dict[str, object], manifest: dict[str, object]) -> dict[str, object]:
    metadata = {}
    for field in SCALAR_METADATA_FIELDS:
        if field in fields:
            metadata[field] = fields[field]
        elif field in manifest and (
            manifest[field] is None
            or isinstance(manifest[field], (bool, int, float, str))
        ):
            metadata[field] = manifest[field]
    return metadata


def _declared_record_path(source_root: Path, directory: object) -> Path:
    if not isinstance(directory, str) or not directory:
        raise ValueError("manifest record_directories must contain non-empty strings")
    relative = Path(directory)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"record directory escapes source directory: {directory!r}")
    record_root = source_root / relative
    record_paths = [record_root / "record.md", record_root / "source_record.md"]
    existing = [path for path in record_paths if path.is_file()]
    if len(existing) > 1:
        raise ValueError(f"record has both record.md and source_record.md: {record_root}")
    if not existing:
        raise FileNotFoundError(f"manifest record is missing: {record_root}")
    return existing[0]


def arxiv_id(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    match = ARXIV_ID_RE.fullmatch(value)
    if match:
        return match.group(1)
    match = ARXIV_URL_RE.fullmatch(value)
    return match.group(1) if match else None


def canonical_url(
    value: str | None, *, url_fragment_identity: bool = False
) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                parsed.query,
                parsed.fragment if url_fragment_identity else "",
            )
        )
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "source" and not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path or "/",
            urlencode(query, doseq=True),
            parsed.fragment if url_fragment_identity else "",
        )
    )


def make_candidate_id(
    *,
    source_directory: str,
    source: str,
    source_id: str,
    url: str | None,
    normalized_arxiv_id: str | None,
) -> tuple[str, str]:
    if normalized_arxiv_id:
        return f"arxiv--{normalized_arxiv_id}", "normalized_arxiv_id"
    if url:
        return f"url--{quote(url, safe='')}", "canonical_url"
    encoded_source = quote(source or source_directory, safe="")
    encoded_source_id = quote(source_id, safe="")
    return (
        f"source--source={encoded_source}&source_id={encoded_source_id}",
        "source_identity",
    )


def _same_datetime(left: object, right: object, location: str) -> bool:
    _, parsed_left = _aware_datetime(left, f"{location} left")
    _, parsed_right = _aware_datetime(right, f"{location} right")
    return parsed_left == parsed_right


def load_source_receipt(
    path: Path,
    *,
    project_root: Path = PROJECT_ROOT,
    crawl_root: Path,
) -> dict[str, object]:
    receipt_path = _crawl_path(
        path,
        project_root=project_root,
        crawl_root=crawl_root,
        location="source discovery receipt",
        require_file=True,
    )
    return _read_json_object(receipt_path, "source discovery receipt")


def write_json_immutable(path: Path, payload: object) -> bool:
    """Atomically create JSON, or reuse an existing exact JSON value."""
    serialized = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"immutable JSON target must not be a symlink: {path}")

    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"immutable JSON target is not a regular file: {path}")
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                raise ValueError(
                    f"immutable JSON target is not valid JSON: {path}"
                ) from error
            existing_serialized = (
                json.dumps(existing, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
            )
            if existing_serialized != serialized:
                raise ValueError(
                    f"immutable JSON target already exists with different content: {path}"
                )
            return False
    finally:
        temporary.unlink(missing_ok=True)


def build_inventory(
    root: Path,
    source_receipt: dict[str, object],
    *,
    cycle_id: str,
    run_id: str,
    project_root: Path = PROJECT_ROOT,
    expected_source_skills: list[str] | None = None,
) -> dict:
    project_root = project_root.absolute()
    if expected_source_skills is None:
        expected_source_skills = discover_source_skills(project_root)
    root = _project_path(
        root,
        project_root=project_root,
        location="crawl root",
        require_directory=True,
    )
    receipt = _plain_object(source_receipt, "source discovery receipt")
    _required_fields(
        receipt, RECEIPT_TOP_LEVEL_FIELDS, "source discovery receipt"
    )
    if receipt["schema_version"] != 2:
        raise ValueError("source discovery receipt schema_version must be 2")
    if receipt.get("cycle_id") != cycle_id:
        raise ValueError("source discovery receipt cycle_id does not match rating run")
    _non_empty_text(run_id, "rating run_id")
    window_since, _ = _aware_datetime(
        receipt.get("window_since"), "source discovery receipt window_since"
    )
    window_until, _ = _aware_datetime(
        receipt.get("window_until"), "source discovery receipt window_until"
    )
    source_entries = receipt.get("sources")
    if not isinstance(source_entries, list):
        raise ValueError("source discovery receipt sources must be a list")
    discovered_count = _non_negative_int(
        receipt["discovered_count"], "source discovery receipt discovered_count"
    )
    completed_count = _non_negative_int(
        receipt["completed_count"], "source discovery receipt completed_count"
    )
    receipt_record_count = _non_negative_int(
        receipt["record_count"], "source discovery receipt record_count"
    )
    if discovered_count != len(source_entries):
        raise ValueError(
            "source discovery receipt discovered_count does not match sources"
        )
    if completed_count != len(source_entries):
        raise ValueError(
            "source discovery receipt must be terminal with completed_count equal to discovered_count"
        )
    raw_status_counts = _plain_object(
        receipt["status_counts"], "source discovery receipt status_counts"
    )
    declared_status_counts = {}
    for raw_status, raw_count in raw_status_counts.items():
        if raw_status not in ALLOWED_SOURCE_STATUSES:
            raise ValueError(
                f"source discovery receipt status_counts has invalid status: {raw_status}"
            )
        declared_status_counts[raw_status] = _non_negative_int(
            raw_count,
            f"source discovery receipt status_counts {raw_status}",
        )

    records = []
    manifests = []
    sources = []
    discovered_sources = []
    seen_names = set()
    seen_directories = set()
    seen_skills = set()
    for source_index, raw_source in enumerate(source_entries):
        source_receipt_entry = _plain_object(
            raw_source, f"source discovery receipt sources[{source_index}]"
        )
        _required_fields(
            source_receipt_entry,
            RECEIPT_SOURCE_FIELDS,
            f"source discovery receipt sources[{source_index}]",
        )
        source_name = _non_empty_text(
            source_receipt_entry.get("source_name"),
            f"source receipt {source_index} source_name",
        )
        skill = _source_directory(
            source_receipt_entry.get("skill"),
            f"source receipt {source_name} skill",
        )
        source_dir = _source_directory(
            source_receipt_entry.get("source_directory"),
            f"source receipt {source_name} source_directory",
        )
        if (
            source_name in seen_names
            or source_dir in seen_directories
            or skill in seen_skills
        ):
            raise ValueError("source receipt skills, names, and directories must be unique")
        seen_names.add(source_name)
        seen_directories.add(source_dir)
        seen_skills.add(skill)
        discovered_sources.append(source_name)
        status = source_receipt_entry.get("status")
        if status not in ALLOWED_SOURCE_STATUSES:
            raise ValueError(f"source receipt {source_name} has an invalid status")
        error_text = _bounded_text(
            source_receipt_entry.get("error"), f"source receipt {source_name} error"
        )
        detail = _bounded_non_empty_text(
            source_receipt_entry.get("detail"),
            f"source receipt {source_name} detail",
        )
        source_record_count = _non_negative_int(
            source_receipt_entry.get("record_count"),
            f"source receipt {source_name} record_count",
        )
        error_count = _non_negative_int(
            source_receipt_entry.get("error_count"),
            f"source receipt {source_name} error_count",
        )
        if bool(error_text) != (error_count > 0):
            raise ValueError(
                f"source receipt {source_name} error and error_count do not agree"
            )
        if source_receipt_entry.get("cycle_id") != cycle_id:
            raise ValueError(f"source receipt {source_name} has the wrong cycle_id")
        if not _same_datetime(
            source_receipt_entry.get("window_since"),
            window_since,
            f"source receipt {source_name} window_since",
        ) or not _same_datetime(
            source_receipt_entry.get("window_until"),
            window_until,
            f"source receipt {source_name} window_until",
        ):
            raise ValueError(f"source receipt {source_name} has the wrong frozen window")
        receipt_record_values = source_receipt_entry.get("record_paths")
        if not isinstance(receipt_record_values, list):
            raise ValueError(f"source receipt {source_name} record_paths must be a list")
        if source_record_count != len(receipt_record_values):
            raise ValueError(
                f"source receipt {source_name} record_count does not match record_paths"
            )

        source_root = root / source_dir
        manifest_value = source_receipt_entry.get("manifest_path")
        if manifest_value is None:
            if status not in MISSING_MANIFEST_STATUSES:
                raise ValueError(f"source receipt {source_name} requires a manifest")
            if receipt_record_values:
                raise ValueError(
                    f"source receipt {source_name} cannot declare records without a manifest"
                )
            sources.append(
                {
                    "source_name": source_name,
                    "skill": skill,
                    "source_directory": source_dir,
                    "status": status,
                    "error": error_text,
                    "detail": detail,
                    "record_count": source_record_count,
                    "error_count": error_count,
                    "cycle_id": cycle_id,
                    "window_since": window_since,
                    "window_until": window_until,
                    "manifest_path": None,
                    "record_paths": [],
                }
            )
            continue

        manifest_path = _crawl_path(
            manifest_value,
            project_root=project_root,
            crawl_root=root,
            location=f"source receipt {source_name} manifest_path",
            require_file=True,
        )
        expected_manifest = (source_root / "crawl_manifest.json").absolute()
        if manifest_path != expected_manifest:
            raise ValueError(
                f"source receipt {source_name} manifest path does not match its source directory"
            )
        manifest = _read_json_object(manifest_path, f"source manifest {source_name}")
        _required_fields(
            manifest,
            frozenset({"cycle_id", "window_since", "window_until", "status"}),
            f"source manifest {source_name}",
        )
        if manifest.get("status") != status:
            raise ValueError(f"source receipt {source_name} status does not match its manifest")
        if manifest["cycle_id"] != cycle_id:
            raise ValueError(f"source receipt {source_name} cycle_id does not match its manifest")
        for field, frozen_value in (
            ("window_since", window_since),
            ("window_until", window_until),
        ):
            if not _same_datetime(
                manifest[field], frozen_value, f"source manifest {source_name} {field}"
            ):
                raise ValueError(
                    f"source receipt {source_name} window does not match its manifest"
                )
        manifest_entry = {
            "source_name": source_name,
            "source_directory": source_dir,
            "path": manifest_path.as_posix(),
            "status": status,
            "cycle_id": cycle_id,
            "window_since": window_since,
            "window_until": window_until,
        }
        for field in SCALAR_METADATA_FIELDS:
            value = manifest.get(field)
            if field in manifest and (
                value is None or isinstance(value, (bool, int, float, str))
            ):
                manifest_entry[field] = value
        manifests.append(manifest_entry)
        declared = manifest.get("record_directories")
        if not isinstance(declared, list):
            raise ValueError(f"manifest record_directories must be a list: {manifest_path}")
        if len(declared) != len(set(declared)):
            raise ValueError(f"manifest record_directories must be unique: {manifest_path}")
        manifest_record_paths = [
            _declared_record_path(source_root, directory).absolute().as_posix()
            for directory in sorted(declared, key=lambda value: str(value))
        ]
        receipt_record_paths = [
            _crawl_path(
                value,
                project_root=project_root,
                crawl_root=root,
                location=f"source receipt {source_name} record path",
                require_file=True,
            ).as_posix()
            for value in receipt_record_values
        ]
        if len(receipt_record_paths) != len(set(receipt_record_paths)) or set(
            receipt_record_paths
        ) != set(manifest_record_paths):
            raise ValueError(
                f"source receipt {source_name} record paths do not match its manifest"
            )
        if source_record_count != len(manifest_record_paths):
            raise ValueError(
                f"source receipt {source_name} record_count does not match its manifest"
            )
        sources.append(
            {
                "source_name": source_name,
                "skill": skill,
                "source_directory": source_dir,
                "status": status,
                "error": error_text,
                "detail": detail,
                "record_count": source_record_count,
                "error_count": error_count,
                "cycle_id": cycle_id,
                "window_since": window_since,
                "window_until": window_until,
                "manifest_path": manifest_path.as_posix(),
                "record_paths": manifest_record_paths,
            }
        )
        for directory in sorted(declared, key=lambda value: str(value)):
            record_path = _declared_record_path(source_root, directory)
            text = record_path.read_text(encoding="utf-8")
            fields = front_matter(text)
            for field, frozen_value in (
                ("window_since", window_since),
                ("window_until", window_until),
            ):
                if field not in fields or not _same_datetime(
                    fields.get(field),
                    frozen_value,
                    f"source record {record_path} {field}",
                ):
                    raise ValueError(
                        f"source record {record_path} does not match the frozen window"
                    )
            record_status_fields = [
                field for field in ("status", "crawl_status") if field in fields
            ]
            if not record_status_fields:
                raise ValueError(
                    f"source record {record_path} requires status or crawl_status"
                )
            for field in record_status_fields:
                _non_empty_text(fields[field], f"source record {record_path} {field}")
            source = _text_value(fields.get("source"), source_dir)
            source_id = _text_value(fields.get("source_id"), directory)
            # Infer exact arXiv identity from declared scalar fields only. The
            # source directory name is not an identity convention for new
            # crawlers, and the body is intentionally never searched.
            normalized_arxiv_id = next(
                (
                    normalized
                    for value in (
                        source_id,
                        _text_value(fields.get("arxiv_url")),
                        _text_value(fields.get("abstract_url")),
                    )
                    if (normalized := arxiv_id(value)) is not None
                ),
                None,
            )
            url_fragment_identity = fields.get("url_fragment_identity") is True
            url = canonical_url(
                _text_value(fields.get("abstract_url"))
                or _text_value(fields.get("url"))
                or _text_value(fields.get("huggingface_url")),
                url_fragment_identity=url_fragment_identity,
            )
            object_id, identity_basis = make_candidate_id(
                source_directory=source_dir,
                source=source,
                source_id=source_id,
                url=url,
                normalized_arxiv_id=normalized_arxiv_id,
            )
            record = {
                "path": record_path.as_posix(),
                "source_directory": source_dir,
                "inventory_source_name": source_name,
                "source": source,
                "source_name": _text_value(fields.get("source_name")),
                "source_id": source_id,
                "title": _text_value(fields.get("title"), ""),
                "url": url,
                "url_fragment_identity": url_fragment_identity,
                "normalized_arxiv_id": normalized_arxiv_id,
                "candidate_id": object_id,
                "identity_basis": identity_basis,
            }
            for field in record_status_fields:
                record[field] = fields[field]
            record.update(_record_metadata(fields, manifest))
            records.append(record)
    if seen_skills != set(expected_source_skills):
        missing = sorted(set(expected_source_skills) - seen_skills)
        unexpected = sorted(seen_skills - set(expected_source_skills))
        raise ValueError(
            "source discovery receipt does not match dynamically discovered skills; "
            f"missing={missing}, unexpected={unexpected}"
        )
    actual_status_counts: dict[str, int] = {}
    for source in sources:
        status = source["status"]
        actual_status_counts[status] = actual_status_counts.get(status, 0) + 1
    if {
        status: count
        for status, count in declared_status_counts.items()
        if count
    } != actual_status_counts:
        raise ValueError(
            "source discovery receipt status_counts does not match source statuses"
        )
    if receipt_record_count != sum(source["record_count"] for source in sources):
        raise ValueError(
            "source discovery receipt record_count does not match source record counts"
        )
    records.sort(key=lambda item: (item["candidate_id"], item["source_directory"], item["path"]))
    groups: dict[str, list[str]] = {}
    for record in records:
        groups.setdefault(record["candidate_id"], []).append(record["path"])
    payload = {
        "schema_version": 2,
        "run_id": run_id,
        "cycle_id": cycle_id,
        "window_since": window_since,
        "window_until": window_until,
        "discovered_count": discovered_count,
        "completed_count": completed_count,
        "status_counts": declared_status_counts,
        "discovered_sources": discovered_sources,
        "discovered_skills": sorted(seen_skills),
        "sources": sources,
        "manifests": manifests,
        "source_record_count": len(records),
        "canonical_identity_count": len(groups),
        "exact_identity_groups": [
            {"candidate_id": candidate_id, "record_paths": paths}
            for candidate_id, paths in sorted(groups.items())
        ],
        "records": records,
    }
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--crawl-root", type=Path, default=Path("working_tmp"))
    parser.add_argument("--source-receipt", type=Path, required=True)
    parser.add_argument("--cycle-id", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return inventory_command(args, _run_owned)


def _run_owned(args):
    run_id = _source_directory(args.run_id, "rating run_id")
    crawl_root = _project_path(
        args.crawl_root,
        project_root=PROJECT_ROOT,
        location="crawl root",
        require_directory=True,
    )
    source_receipt_path = validate_production_source_receipt_path(
        args.source_receipt,
        cycle_id=args.cycle_id,
        project_root=PROJECT_ROOT,
        crawl_root=crawl_root,
    )
    source_receipt = load_source_receipt(
        source_receipt_path,
        project_root=PROJECT_ROOT,
        crawl_root=crawl_root,
    )
    payload = build_inventory(
        crawl_root,
        source_receipt,
        cycle_id=args.cycle_id,
        run_id=run_id,
        project_root=PROJECT_ROOT,
        expected_source_skills=discover_source_skills(PROJECT_ROOT),
    )
    expected_output = (
        PROJECT_ROOT
        / "working_tmp"
        / "rating_filter_organize"
        / "runs"
        / run_id
        / "shared"
        / "source_inventory.json"
    ).absolute()
    output = _project_path(
        args.output,
        project_root=PROJECT_ROOT,
        location="inventory output",
    )
    if output != expected_output:
        raise ValueError(
            "inventory output must be the declared rating run shared/source_inventory.json"
        )
    write_json_immutable(output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
