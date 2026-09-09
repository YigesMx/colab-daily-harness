#!/usr/bin/env python3
"""Fetch recent relevant papers from Hugging Face daily and weekly rankings."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from colab_daily.adapters import source_command
from colab_daily.config import Config


try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:  # pragma: no cover - exercised by CLI users
    raise SystemExit(
        "Missing dependency: run `uv sync` from the repository root."
    ) from exc


LOGGER = logging.getLogger("huggingface_trending_crawler")
BASE_URL = "https://huggingface.co"
DAILY_API_URL = f"{BASE_URL}/api/daily_papers"
DEFAULT_DAYS = 3
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_DAILY_LIMIT = 50
DEFAULT_WEEKLY_LIMIT = 100
REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = "colab-daily-huggingface-crawler/0.1"
DIRECT_GROUPS = {
    "embodied_robotics",
    "arm_vla",
    "drone_vla_vln",
    "embodied_infra",
    "world_model",
}
SECONDARY_GROUP = "llm_vlm_methods"


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime


@dataclass
class FetchReport:
    source: str
    period: str
    url: str
    status: str
    item_count: int = 0
    error: Optional[str] = None


@dataclass
class PaperRecord:
    source_id: str
    title: str
    authors: list[str]
    organization: Optional[str]
    summary: str
    published_at: Optional[str]
    listed_at: str
    retrieved_at: str
    huggingface_url: str
    arxiv_url: Optional[str]
    pdf_url: Optional[str]
    project_url: Optional[str]
    thumbnail_url: Optional[str]
    media_urls: list[str]
    upvotes: int
    num_comments: int
    daily_positions: dict[str, int] = field(default_factory=dict)
    weekly_positions: dict[str, int] = field(default_factory=dict)
    matched_query_groups: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    relevance_reasons: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    status: str = "complete"


class DailyPapersPropsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.props: Optional[str] = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if self.props is not None:
            return
        values = dict(attrs)
        if values.get("data-target") == "DailyPapers" and values.get("data-props"):
            self.props = values["data-props"]


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--consensus", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--since", type=parse_datetime, default=None)
    parser.add_argument("--until", type=parse_datetime, default=None)
    parser.add_argument("--cycle-id", default=None)
    parser.add_argument("--owner")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--days", type=positive_int, default=None)
    parser.add_argument("--daily-limit", type=positive_int, default=DEFAULT_DAILY_LIMIT)
    parser.add_argument("--weekly-limit", type=positive_int, default=DEFAULT_WEEKLY_LIMIT)
    parser.add_argument("--min-upvotes", type=nonnegative_int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid ISO 8601 datetime: {value}"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_api_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def resolve_window(args: argparse.Namespace, now: Optional[datetime] = None) -> Window:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if args.since is None and args.until is None and args.days is None:
        local_today = current.astimezone(LOCAL_TIMEZONE).date()
        until = datetime.combine(local_today, datetime.min.time(), LOCAL_TIMEZONE)
        since = (until - timedelta(days=1)).astimezone(timezone.utc)
        until = until.astimezone(timezone.utc)
    else:
        since = args.since or current - timedelta(days=args.days or DEFAULT_DAYS)
        until = args.until or current
    if since >= until:
        raise ValueError("window must satisfy since < until")
    return Window(since=since, until=until)


def parse_consensus(path: Path) -> dict[str, list[str]]:
    text = path.read_text(encoding="utf-8")
    groups: dict[str, list[str]] = {}
    in_keyword_section = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "### 关键词组":
            in_keyword_section = True
            continue
        if in_keyword_section and line.startswith("### "):
            break
        if not in_keyword_section:
            continue
        match = re.match(r"- `([^`]+)`[:：](.+)", line)
        if match:
            groups[match.group(1)] = re.findall(r"`([^`]+)`", match.group(2))
    if not groups:
        raise ValueError(f"No keyword groups found in {path}")
    return groups


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json,text/html"})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def dates_overlapping(window: Window) -> list[date]:
    current = window.since.date()
    last = (window.until - timedelta(microseconds=1)).date()
    dates: list[date] = []
    while current <= last:
        dates.append(current)
        current += timedelta(days=1)
    return dates


def iso_weeks_overlapping(window: Window) -> list[str]:
    return sorted(
        {
            f"{day.isocalendar().year}-W{day.isocalendar().week:02d}"
            for day in dates_overlapping(window)
        }
    )


def fetch_daily(
    session: requests.Session,
    day: date,
    limit: int,
) -> tuple[list[dict[str, object]], FetchReport]:
    params = {"date": day.isoformat(), "sort": "publishedAt", "limit": limit}
    prepared = requests.Request("GET", DAILY_API_URL, params=params).prepare().url or DAILY_API_URL
    report = FetchReport("daily", day.isoformat(), prepared, "failed")
    try:
        response = session.get(DAILY_API_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError("daily API response is not a list")
        items = data[:limit]
        report.status = "success"
        report.item_count = len(items)
        return items, report
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        report.error = str(exc)
        return [], report


def extract_weekly_items(page: str) -> list[dict[str, object]]:
    parser = DailyPapersPropsParser()
    parser.feed(page)
    if parser.props is None:
        raise ValueError("DailyPapers data-props not found in weekly page")
    props = json.loads(html.unescape(parser.props))
    items = props.get("dailyPapers")
    if not isinstance(items, list):
        raise ValueError("weekly DailyPapers payload has no dailyPapers list")
    return items


def fetch_weekly(
    session: requests.Session,
    period: str,
    limit: int,
) -> tuple[list[dict[str, object]], FetchReport]:
    url = f"{BASE_URL}/papers/week/{period}"
    report = FetchReport("weekly", period, url, "failed")
    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        items = extract_weekly_items(response.text)[:limit]
        report.status = "success"
        report.item_count = len(items)
        return items, report
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        report.error = str(exc)
        return [], report


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_paper_id(value: object) -> str:
    paper_id = clean_text(value)
    match = re.match(r"(.+?)(v\d+)$", paper_id)
    return match.group(1) if match else paper_id


def listed_datetime(item: dict[str, object]) -> Optional[datetime]:
    paper = item.get("paper")
    if not isinstance(paper, dict):
        return None
    listed = parse_api_datetime(clean_text(paper.get("submittedOnDailyAt")) or None)
    return listed or parse_api_datetime(clean_text(item.get("publishedAt")) or None)


def item_in_window(item: dict[str, object], window: Window) -> bool:
    listed = listed_datetime(item)
    return listed is not None and window.since <= listed < window.until


def paper_from_item(item: dict[str, object], retrieved_at: datetime) -> PaperRecord:
    paper = item.get("paper")
    if not isinstance(paper, dict):
        raise ValueError("ranking item is missing paper data")
    source_id = normalize_paper_id(paper.get("id"))
    if not source_id:
        raise ValueError("paper is missing an id")
    authors_data = paper.get("authors") if isinstance(paper.get("authors"), list) else []
    authors = [
        clean_text(author.get("name"))
        for author in authors_data
        if isinstance(author, dict) and clean_text(author.get("name"))
    ]
    organization_data = item.get("organization") or paper.get("organization")
    organization = None
    if isinstance(organization_data, dict):
        organization = clean_text(
            organization_data.get("fullname") or organization_data.get("name")
        ) or None
    listed = listed_datetime(item)
    if listed is None:
        raise ValueError(f"paper {source_id} has no listing date")
    is_arxiv = bool(re.fullmatch(r"\d{4}\.\d{4,5}", source_id))
    media = paper.get("mediaUrls") or item.get("mediaUrls") or []
    return PaperRecord(
        source_id=source_id,
        title=clean_text(paper.get("title") or item.get("title")),
        authors=authors,
        organization=organization,
        summary=clean_text(paper.get("summary") or item.get("summary")),
        published_at=(
            isoformat(parse_api_datetime(clean_text(item.get("publishedAt"))) or listed)
        ),
        listed_at=isoformat(listed),
        retrieved_at=isoformat(retrieved_at),
        huggingface_url=f"{BASE_URL}/papers/{source_id}",
        arxiv_url=f"https://arxiv.org/abs/{source_id}" if is_arxiv else None,
        pdf_url=f"https://arxiv.org/pdf/{source_id}" if is_arxiv else None,
        project_url=clean_text(paper.get("projectPage")) or None,
        thumbnail_url=clean_text(item.get("thumbnail")) or None,
        media_urls=[clean_text(value) for value in media if clean_text(value)],
        upvotes=int(paper.get("upvotes") or item.get("upvotes") or 0),
        num_comments=int(item.get("numComments") or 0),
    )


def merge_item(
    records: dict[str, PaperRecord],
    item: dict[str, object],
    source: str,
    period: str,
    rank: int,
    retrieved_at: datetime,
) -> None:
    incoming = paper_from_item(item, retrieved_at)
    existing = records.get(incoming.source_id)
    if existing is None:
        existing = incoming
        records[incoming.source_id] = existing
    else:
        existing.upvotes = max(existing.upvotes, incoming.upvotes)
        existing.num_comments = max(existing.num_comments, incoming.num_comments)
        existing.media_urls = sorted(set(existing.media_urls + incoming.media_urls))
        for attr in ("organization", "summary", "project_url", "thumbnail_url"):
            if not getattr(existing, attr) and getattr(incoming, attr):
                setattr(existing, attr, getattr(incoming, attr))
        if incoming.listed_at > existing.listed_at:
            existing.listed_at = incoming.listed_at
    positions = existing.daily_positions if source == "daily" else existing.weekly_positions
    positions[period] = min(rank, positions.get(period, rank))


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    if re.fullmatch(r"[A-Z][A-Z0-9-]{1,5}", keyword):
        # Match standalone acronyms case-insensitively and branded CamelCase
        # forms such as TurboVLA without matching arbitrary lowercase substrings.
        return re.compile(
            rf"(?i:(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9]))|"
            rf"(?<![A-Z]){escaped}(?![A-Z])"
        )
    if re.fullmatch(r"[A-Za-z0-9-]+", keyword):
        return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def matched_keywords(text: str, keywords: Iterable[str]) -> list[str]:
    return sorted(
        {keyword for keyword in keywords if keyword_pattern(keyword).search(text)},
        key=str.casefold,
    )


def assess_relevance(
    record: PaperRecord,
    groups: dict[str, list[str]],
) -> tuple[list[str], list[str], list[str]]:
    matched_groups: set[str] = set()
    keywords: set[str] = set()
    reasons: list[str] = []
    for group, group_keywords in groups.items():
        title_matches = matched_keywords(record.title, group_keywords)
        summary_matches = matched_keywords(record.summary, group_keywords)
        if group in DIRECT_GROUPS and (title_matches or summary_matches):
            matched_groups.add(group)
            keywords.update(title_matches)
            keywords.update(summary_matches)
            for keyword in title_matches:
                reasons.append(f"direct keyword in title: {keyword}")
            for keyword in summary_matches:
                reasons.append(f"direct keyword in summary: {keyword}")
        elif group == SECONDARY_GROUP:
            best_rank = min(
                [*record.daily_positions.values(), *record.weekly_positions.values()],
                default=10_000,
            )
            if title_matches or len(summary_matches) >= 2 or (summary_matches and best_rank <= 10):
                matched_groups.add(group)
                keywords.update(title_matches)
                keywords.update(summary_matches)
                for keyword in title_matches:
                    reasons.append(f"related method keyword in title: {keyword}")
                for keyword in summary_matches:
                    reasons.append(f"related method keyword in summary: {keyword}")
                if not title_matches and len(summary_matches) == 1 and best_rank <= 10:
                    reasons.append(f"single related-method match accepted at source rank {best_rank}")
    return sorted(matched_groups), sorted(keywords, key=str.casefold), sorted(set(reasons))


def safe_record_directory(source_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source_id).strip("._")
    if not safe:
        raise ValueError("paper id cannot be converted to a safe directory")
    return safe


def yaml_scalar(value: Optional[str]) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def yaml_list(values: Iterable[object]) -> str:
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def record_markdown(record: PaperRecord, window: Window) -> str:
    daily_dates = sorted(record.daily_positions)
    weekly_periods = sorted(record.weekly_positions)
    lines = [
        "---",
        "source: huggingface",
        "category_hint: Paper",
        "content_status: summary_only",
        "source_role: secondary",
        f"source_id: {yaml_scalar(record.source_id)}",
        f"title: {yaml_scalar(record.title)}",
        f"authors: {yaml_list(record.authors)}",
        f"organization: {yaml_scalar(record.organization)}",
        f"published_at: {yaml_scalar(record.published_at)}",
        f"listed_at: {yaml_scalar(record.listed_at)}",
        f"retrieved_at: {yaml_scalar(record.retrieved_at)}",
        f"window_since: {yaml_scalar(isoformat(window.since))}",
        f"window_until: {yaml_scalar(isoformat(window.until))}",
        f"huggingface_url: {yaml_scalar(record.huggingface_url)}",
        f"arxiv_url: {yaml_scalar(record.arxiv_url)}",
        f"pdf_url: {yaml_scalar(record.pdf_url)}",
        f"project_url: {yaml_scalar(record.project_url)}",
        f"thumbnail_url: {yaml_scalar(record.thumbnail_url)}",
        f"media_urls: {yaml_list(record.media_urls)}",
        f"upvotes: {record.upvotes}",
        f"num_comments: {record.num_comments}",
        f"daily_dates: {yaml_list(daily_dates)}",
        f"daily_ranks: {yaml_list([record.daily_positions[value] for value in daily_dates])}",
        f"weekly_periods: {yaml_list(weekly_periods)}",
        f"weekly_ranks: {yaml_list([record.weekly_positions[value] for value in weekly_periods])}",
        f"matched_query_groups: {yaml_list(record.matched_query_groups)}",
        f"matched_keywords: {yaml_list(record.matched_keywords)}",
        f"relevance_reasons: {yaml_list(record.relevance_reasons)}",
        f"status: {record.status}",
        f"attachments: {yaml_list(record.attachments)}",
        f"errors: {yaml_list(record.errors)}",
        "---",
        "",
        f"# {record.title}",
        "",
        "## Authors",
        "",
        ", ".join(record.authors) or "Unknown",
        "",
        "## Organization",
        "",
        record.organization or "Not provided by Hugging Face",
        "",
        "## Summary",
        "",
        record.summary,
        "",
        "## Trending signals",
        "",
        f"- Upvotes: {record.upvotes}",
        f"- Comments: {record.num_comments}",
        f"- Daily ranks: {json.dumps(record.daily_positions, ensure_ascii=False, sort_keys=True)}",
        f"- Weekly ranks: {json.dumps(record.weekly_positions, ensure_ascii=False, sort_keys=True)}",
        "",
        "## Consensus matches",
        "",
        f"- Groups: {', '.join(record.matched_query_groups)}",
        f"- Keywords: {', '.join(record.matched_keywords)}",
        f"- Reasons: {'; '.join(record.relevance_reasons)}",
        "",
        "## Source",
        "",
        f"- Hugging Face: {record.huggingface_url}",
    ]
    if record.arxiv_url:
        lines.append(f"- arXiv: {record.arxiv_url}")
    if record.pdf_url:
        lines.append(f"- PDF: {record.pdf_url}")
    if record.project_url:
        lines.append(f"- Project: {record.project_url}")
    return "\n".join(lines) + "\n"


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def manifest(
    window: Window,
    consensus_path: Path,
    reports: list[FetchReport],
    raw_item_count: int,
    records_before_filter: int,
    records: dict[str, PaperRecord],
    cycle_id: Optional[str] = None,
) -> dict[str, object]:
    succeeded = sum(report.status == "success" for report in reports)
    failed = sum(report.status == "failed" for report in reports)
    status = "success" if failed == 0 else "failed" if succeeded == 0 else "partial"
    return {
        "source": "huggingface",
        "cycle_id": cycle_id,
        "category_hint": "Paper",
        "content_status": "summary_only",
        "source_role": "secondary",
        "status": status,
        "retrieved_at": isoformat(datetime.now(timezone.utc)),
        "window_since": isoformat(window.since),
        "window_until": isoformat(window.until),
        "consensus": str(consensus_path),
        "daily_dates": [value.isoformat() for value in dates_overlapping(window)],
        "weekly_periods": iso_weeks_overlapping(window),
        "request_count": len(reports),
        "successful_request_count": succeeded,
        "failed_request_count": failed,
        "raw_ranked_item_count": raw_item_count,
        "unique_recent_item_count": records_before_filter,
        "filtered_record_count": len(records),
        "record_directories": [safe_record_directory(value) for value in sorted(records)],
        "requests": [asdict(report) for report in reports],
    }


def crawl(
    consensus_path: Path,
    window: Window,
    daily_limit: int,
    weekly_limit: int,
    min_upvotes: int,
) -> tuple[dict[str, PaperRecord], list[FetchReport], int, int]:
    groups = parse_consensus(consensus_path)
    session = build_session()
    reports: list[FetchReport] = []
    records: dict[str, PaperRecord] = {}
    raw_item_count = 0
    retrieved_at = datetime.now(timezone.utc)

    for day in dates_overlapping(window):
        items, report = fetch_daily(session, day, daily_limit)
        reports.append(report)
        raw_item_count += len(items)
        for rank, item in enumerate(items, 1):
            if item_in_window(item, window):
                merge_item(records, item, "daily", day.isoformat(), rank, retrieved_at)

    for period in iso_weeks_overlapping(window):
        items, report = fetch_weekly(session, period, weekly_limit)
        reports.append(report)
        raw_item_count += len(items)
        for rank, item in enumerate(items, 1):
            if item_in_window(item, window):
                merge_item(records, item, "weekly", period, rank, retrieved_at)

    records_before_filter = len(records)
    relevant: dict[str, PaperRecord] = {}
    for source_id, record in records.items():
        if record.upvotes < min_upvotes:
            continue
        groups_matched, keywords, reasons = assess_relevance(record, groups)
        if not groups_matched:
            continue
        record.matched_query_groups = groups_matched
        record.matched_keywords = keywords
        record.relevance_reasons = reasons
        relevant[source_id] = record
    return relevant, reports, raw_item_count, records_before_filter


def write_outputs(
    output_dir: Path,
    window: Window,
    consensus_path: Path,
    records: dict[str, PaperRecord],
    reports: list[FetchReport],
    raw_item_count: int,
    records_before_filter: int,
    dry_run: bool,
    cycle_id: Optional[str] = None,
) -> None:
    ordered = sorted(
        records.values(),
        key=lambda record: (
            min(
                [*record.daily_positions.values(), *record.weekly_positions.values()],
                default=10_000,
            ),
            -record.upvotes,
            record.source_id,
        ),
    )
    if dry_run:
        for record in ordered:
            groups = ",".join(record.matched_query_groups)
            print(f"{record.source_id} upvotes={record.upvotes} groups={groups}: {record.title}")
        print(f"dry-run: {len(records)} relevant records from {records_before_filter} recent papers")
        return

    source_dir = output_dir / "huggingface"
    source_dir.mkdir(parents=True, exist_ok=True)
    for record in ordered:
        directory = source_dir / safe_record_directory(record.source_id)
        write_text_atomic(directory / "record.md", record_markdown(record, window))
    write_text_atomic(
        source_dir / "crawl_manifest.json",
        json.dumps(
            manifest(
                window,
                consensus_path,
                reports,
                raw_item_count,
                records_before_filter,
                records,
                cycle_id,
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _run_owned(args) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    repo_root = Config.load(args.project_root).project_root
    consensus_path = (args.consensus or repo_root / "consensus.md").resolve()
    output_dir = (args.output_dir or repo_root / "working_tmp").resolve()
    if not consensus_path.is_file():
        LOGGER.error("Consensus file does not exist: %s", consensus_path)
        return 2
    try:
        window = resolve_window(args)
        records, reports, raw_item_count, records_before_filter = crawl(
            consensus_path,
            window,
            args.daily_limit,
            args.weekly_limit,
            args.min_upvotes,
        )
        write_outputs(
            output_dir,
            window,
            consensus_path,
            records,
            reports,
            raw_item_count,
            records_before_filter,
            args.dry_run,
            args.cycle_id,
        )
    except (OSError, ValueError) as exc:
        LOGGER.error("Hugging Face crawler failed: %s", exc)
        return 2
    if reports and all(report.status == "failed" for report in reports):
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return source_command(args, _run_owned, "huggingface")


if __name__ == "__main__":
    sys.exit(main())
