#!/usr/bin/env python3
"""Fetch and consensus-filter recent AI media and community RSS feeds."""

from __future__ import annotations

import argparse
import calendar
import concurrent.futures
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from colab_daily.adapters import source_command
from colab_daily.config import Config


try:
    import feedparser
    import requests
    from dotenv import load_dotenv
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:  # pragma: no cover
    raise SystemExit("Missing dependency: run `uv sync --locked` from the repository root.") from exc


LOGGER = logging.getLogger("ai_info_source_crawler")
DEFAULT_DAYS = 3
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_MAX_ENTRIES = 100
DEFAULT_MAX_API_PAGES = 2
DEFAULT_MAX_ARTICLE_FETCHES = 10
REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = "colab-daily-ai-info-crawler/0.1"
DEFAULT_SCOPE = "consensus"
VALID_SCOPES = {DEFAULT_SCOPE, "broad_news"}
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
class SourceReport:
    source_name: str
    publisher: str
    url: str
    date_policy: str
    status: str
    access_mode: str = "rss"
    source_role: str = "ai_media"
    category_hint: str = "News"
    scope: str = DEFAULT_SCOPE
    http_request_count: int = 0
    feed_request_count: int = 0
    api_request_count: int = 0
    page_fetch_count: int = 0
    article_fetch_count: int = 0
    fetched_item_count: int = 0
    dated_item_count: int = 0
    window_item_count: int = 0
    relevant_item_count: int = 0
    record_count: int = 0
    partial_record_count: int = 0
    skipped_item_count: int = 0
    feed_updated_at: Optional[str] = None
    errors: list[str] = field(default_factory=list)


@dataclass
class InfoRecord:
    source_name: str
    publisher: str
    source_id: str
    title: str
    authors: list[str]
    published_at: Optional[str]
    listed_at: str
    date_basis: str
    retrieved_at: str
    url: str
    feed_url: str
    homepage_url: str
    categories: list[str]
    image_urls: list[str]
    summary: str
    content: str
    matched_query_groups: list[str]
    matched_keywords: list[str]
    relevance_reasons: list[str]
    status: str = "complete"
    attachments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    content_status: str = "full"
    source_role: str = "ai_media"
    category_hint: str = "News"
    scope: str = DEFAULT_SCOPE


class TextParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks: list[str] = []
        self.images: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif tag == "img" and values.get("src"):
            self.images.add(values["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return clean_text(" ".join(self._chunks))


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=None)
    parser.add_argument("--consensus", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--since", type=parse_datetime, default=None)
    parser.add_argument("--until", type=parse_datetime, default=None)
    parser.add_argument("--cycle-id", default=None)
    parser.add_argument("--owner")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--days", type=positive_int, default=None)
    parser.add_argument("--max-entries-per-source", type=positive_int, default=DEFAULT_MAX_ENTRIES)
    parser.add_argument("--max-api-pages", type=positive_int, default=DEFAULT_MAX_API_PAGES)
    parser.add_argument("--max-article-fetches", type=positive_int, default=DEFAULT_MAX_ARTICLE_FETCHES)
    parser.add_argument(
        "--skip-source",
        action="append",
        default=[],
        metavar="SOURCE_NAME",
        help="skip a configured source without issuing a request; may be repeated",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO 8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_any_datetime(value: object) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(value, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def struct_time_to_datetime(value: object) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def html_text(value: str) -> tuple[str, list[str]]:
    parser = TextParser()
    parser.feed(value)
    return parser.text, sorted(parser.images)


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def load_project_environment(repo_root: Path) -> None:
    load_dotenv(repo_root / ".env", override=False)


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
    return Window(since, until)


def load_sources(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"No sources configured in {path}")
    names: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Every source must be an object")
        for required in ("name", "publisher", "url", "homepage", "date_policy"):
            if not clean_text(source.get(required)):
                raise ValueError(f"Source is missing {required}: {source}")
        name = clean_text(source["name"])
        if name in names:
            raise ValueError(f"Duplicate source name: {name}")
        names.add(name)
        if source["date_policy"] not in {"entry", "feed_updated_snapshot"}:
            raise ValueError(f"Unsupported date policy for {name}: {source['date_policy']}")
        access_mode = clean_text(source.get("access_mode") or "rss")
        if access_mode not in {"rss", "zhihu_column_api", "rss_plus_wordpress_api"}:
            raise ValueError(f"Unsupported access mode for {name}: {access_mode}")
        if source.get("source_role") and not clean_text(source.get("source_role")):
            raise ValueError(f"Invalid source role for {name}")
        if source.get("category_hint") != "News":
            raise ValueError(f"category_hint must be News for {name}")
        if source.get("scope") is not None and clean_text(source.get("scope")) not in VALID_SCOPES:
            raise ValueError(f"Unsupported scope for {name}: {source['scope']}")
        for bounded in ("page_size", "max_pages"):
            if source.get(bounded) is not None and (
                not isinstance(source[bounded], int) or isinstance(source[bounded], bool)
                or source[bounded] <= 0
            ):
                raise ValueError(f"{bounded} must be a positive integer for {name}")
        if access_mode == "rss_plus_wordpress_api" and not clean_text(source.get("api_url")):
            raise ValueError(f"WordPress API source is missing api_url: {name}")
        if source.get("token_env") and not clean_text(source.get("token_env")):
            raise ValueError(f"Invalid token environment variable for {name}")
    return sources


def parse_consensus(path: Path) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    section: Optional[str] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("### 关键词组"):
            section = "keywords"
            continue
        if line.startswith(("### 资讯匹配别名", "### 中文资讯别名")):
            section = "aliases"
            continue
        if line.startswith("### "):
            section = None
            continue
        if section not in {"keywords", "aliases"}:
            continue
        match = re.match(r"- `([^`]+)`[:：](.+)", line)
        if match:
            group = match.group(1)
            groups.setdefault(group, []).extend(re.findall(r"`([^`]+)`", match.group(2)))
    groups = {group: list(dict.fromkeys(values)) for group, values in groups.items()}
    required = DIRECT_GROUPS | {SECONDARY_GROUP}
    if not required.issubset(groups):
        raise ValueError(f"Consensus is missing keyword groups: {sorted(required - groups.keys())}")
    return groups


def build_session(retry_server_errors: bool = True) -> requests.Session:
    retry = Retry(
        total=3 if retry_server_errors else 0,
        backoff_factor=1.0,
        status_forcelist=(500, 502, 503, 504) if retry_server_errors else (),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def source_access_mode(source: dict[str, object]) -> str:
    return clean_text(source.get("access_mode") or "rss")


def source_role(source: dict[str, object]) -> str:
    return clean_text(source.get("source_role") or "ai_media")


def category_hint(source: dict[str, object]) -> str:
    return clean_text(source.get("category_hint") or "News")


def source_scope(source: dict[str, object]) -> str:
    return clean_text(source.get("scope") or DEFAULT_SCOPE)


def bounded_source_int(source: dict[str, object], key: str, default: int, maximum: int) -> int:
    value = source.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(1, min(value, maximum))


def entry_datetime(entry: object) -> tuple[Optional[datetime], Optional[str]]:
    for field in ("published", "updated", "created"):
        parsed = struct_time_to_datetime(entry.get(f"{field}_parsed"))
        if parsed:
            return parsed, f"entry_{field}"
        parsed = parse_any_datetime(entry.get(field))
        if parsed:
            return parsed, f"entry_{field}"
    return None, None


def feed_datetime(feed: object) -> Optional[datetime]:
    parsed = struct_time_to_datetime(feed.get("updated_parsed"))
    return parsed or parse_any_datetime(clean_text(feed.get("updated")) or None)


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    escaped = re.escape(keyword)
    if re.fullmatch(r"[A-Z][A-Z0-9-]{1,5}", keyword):
        return re.compile(
            rf"(?i:(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9]))|(?<![A-Z]){escaped}(?![A-Z])"
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
    title: str,
    body: str,
    groups: dict[str, list[str]],
    scope: str = DEFAULT_SCOPE,
) -> tuple[list[str], list[str], list[str]]:
    matched_groups: set[str] = set()
    keywords: set[str] = set()
    reasons: list[str] = []
    for group, group_keywords in groups.items():
        title_matches = matched_keywords(title, group_keywords)
        body_matches = matched_keywords(body, group_keywords)
        accepted = group in DIRECT_GROUPS and bool(title_matches or body_matches)
        if group == SECONDARY_GROUP:
            accepted = bool(title_matches) or len(body_matches) >= 2
        if not accepted:
            continue
        matched_groups.add(group)
        keywords.update(title_matches)
        keywords.update(body_matches)
        reasons.extend(f"keyword in title ({group}): {keyword}" for keyword in title_matches)
        reasons.extend(f"keyword in feed text ({group}): {keyword}" for keyword in body_matches)
    if scope == "broad_news" and not reasons:
        reasons.append("broad_news scope: retained for downstream rating")
    return sorted(matched_groups), sorted(keywords, key=str.casefold), sorted(set(reasons))


def entry_parts(entry: object) -> tuple[str, str, list[str]]:
    summary, summary_images = html_text(
        clean_text(entry.get("summary") or entry.get("excerpt"))
    )
    content_values = [
        clean_text(item.get("value"))
        for item in entry.get("content") or []
        if isinstance(item, dict) and clean_text(item.get("value"))
    ]
    content, content_images = html_text(" ".join(content_values))
    return summary, content, sorted(set(summary_images + content_images))


def json_response(response: requests.Response) -> object:
    try:
        return response.json()
    except (ValueError, AttributeError) as exc:
        raise ValueError("JSON API response could not be decoded") from exc


def api_get(
    session: requests.Session,
    url: str,
    params: Optional[dict[str, object]],
    report: SourceReport,
) -> object:
    report.http_request_count += 1
    report.api_request_count += 1
    report.page_fetch_count += 1
    response = session.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    if response.status_code >= 400:
        raise ValueError(f"API request failed with HTTP {response.status_code}")
    return json_response(response)


def zhihu_article_entry(item: object) -> dict[str, object]:
    item = item if isinstance(item, dict) else {}
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    return {
        "title": item.get("title"),
        "link": item.get("url"),
        "created": item.get("created"),
        "updated": item.get("updated"),
        "summary": item.get("excerpt"),
        "content": [{"value": item.get("content")}],
        "author": author.get("name"),
        "title_image": item.get("title_image") or item.get("image_url"),
        "tags": [{"term": "知乎专栏"}],
    }


def fetch_zhihu_entries(
    source: dict[str, object],
    session: requests.Session,
    report: SourceReport,
    limit: int,
    max_pages_override: int = DEFAULT_MAX_API_PAGES,
) -> list[object]:
    page_size = min(
        bounded_source_int(source, "page_size", min(limit, 20), 100),
        limit,
    )
    max_pages = min(
        bounded_source_int(source, "max_pages", DEFAULT_MAX_API_PAGES, DEFAULT_MAX_API_PAGES),
        max_pages_override,
    )
    entries: list[object] = []
    base_url = clean_text(source["url"])
    reached_end = False
    for page in range(max_pages):
        offset = page * page_size
        data = api_get(session, base_url, {"limit": page_size, "offset": offset}, report)
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise ValueError("Zhihu column API response has no data list")
        page_entries = [zhihu_article_entry(item) for item in data["data"]]
        entries.extend(page_entries)
        paging = data.get("paging") if isinstance(data.get("paging"), dict) else {}
        if len(entries) >= limit or not page_entries or paging.get("is_end", True):
            reached_end = True
            break
    if not reached_end:
        report.errors.append("Zhihu API page limit reached before the column was exhausted")
    return entries[:limit]


def apply_wordpress_post(entry: object, post: dict[str, object]) -> object:
    if not post:
        return entry
    updates = dict(entry)
    title = post.get("title") if isinstance(post.get("title"), dict) else {}
    content = post.get("content") if isinstance(post.get("content"), dict) else {}
    excerpt = post.get("excerpt") if isinstance(post.get("excerpt"), dict) else {}
    if clean_text(title.get("rendered")):
        updates["title"] = title["rendered"]
    if clean_text(content.get("rendered")):
        updates["content"] = [{"value": content["rendered"]}]
    if clean_text(excerpt.get("rendered")):
        updates["summary"] = excerpt["rendered"]
    return updates


def wordpress_post_id(entry_url: str) -> Optional[str]:
    matches = re.findall(r"/([0-9]+)(?:\.html)?(?:[/?#]|$)", entry_url)
    return matches[-1] if matches else None


def fetch_wordpress_posts(
    source: dict[str, object],
    entries: list[object],
    session: requests.Session,
    report: SourceReport,
    max_article_fetches: int,
    max_pages_override: int = DEFAULT_MAX_API_PAGES,
) -> tuple[dict[str, dict[str, object]], set[str], list[str]]:
    candidates: list[tuple[str, str]] = []
    errors: list[str] = []
    for entry in entries:
        entry_url = clean_text(entry.get("link"))
        post_id = wordpress_post_id(entry_url)
        if post_id:
            candidates.append((entry_url, post_id))
        else:
            errors.append(f"entry URL has no numeric WordPress post id: {entry_url}")
    candidates = candidates[:max_article_fetches]
    page_size = bounded_source_int(source, "page_size", 20, 100)
    max_pages = min(
        bounded_source_int(source, "max_pages", DEFAULT_MAX_API_PAGES, DEFAULT_MAX_API_PAGES),
        max_pages_override,
    )
    posts_by_url: dict[str, dict[str, object]] = {}
    fetched_urls: set[str] = set()
    for page_index, start in enumerate(range(0, len(candidates), page_size)):
        if page_index >= max_pages:
            errors.append("WordPress API page limit reached before all entries were enriched")
            break
        batch = candidates[start : start + page_size]
        try:
            data = api_get(
                session,
                clean_text(source["api_url"]),
                {
                    "include": ",".join(post_id for _, post_id in batch),
                    "per_page": len(batch),
                    "context": "view",
                },
                report,
            )
        except (requests.RequestException, ValueError) as exc:
            errors.append(str(exc))
            continue
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            errors.append("WordPress API response is not a list")
            continue
        for post in data:
            if not isinstance(post, dict):
                continue
            post_url = clean_text(post.get("link"))
            for entry_url, post_id in batch:
                if post_url == entry_url or str(post.get("id")) == post_id:
                    posts_by_url[entry_url] = post
                    fetched_urls.add(entry_url)
                    break
    return posts_by_url, fetched_urls, errors


def stable_identity(source_name: str, url: str) -> tuple[str, str]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{source_name}:{digest}", f"{source_name}--{digest}"


def record_from_entry(
    source: dict[str, object],
    entry: object,
    published: Optional[datetime],
    listed: datetime,
    date_basis: str,
    retrieved_at: datetime,
    groups: dict[str, list[str]],
    content_status: str = "full",
    content_errors: Optional[list[str]] = None,
) -> Optional[InfoRecord]:
    url = clean_text(entry.get("link"))
    title = clean_text(entry.get("title"))
    if not url or not title:
        return None
    summary, content, images = entry_parts(entry)
    title_image = clean_text(entry.get("title_image") or entry.get("image_url"))
    if title_image:
        images = sorted(set(images + [title_image]))
    categories = sorted(
        {
            clean_text(tag.get("term"))
            for tag in entry.get("tags") or []
            if isinstance(tag, dict) and clean_text(tag.get("term"))
        }
    )
    body = " ".join([summary, content, " ".join(categories)])
    scope = source_scope(source)
    matched_groups, keywords, reasons = assess_relevance(title, body, groups, scope)
    if not matched_groups and scope != "broad_news":
        return None
    authors = [
        clean_text(author.get("name"))
        for author in entry.get("authors") or []
        if isinstance(author, dict) and clean_text(author.get("name"))
    ]
    if not authors and clean_text(entry.get("author")):
        authors = [clean_text(entry.get("author"))]
    errors: list[str] = list(dict.fromkeys(content_errors or []))
    status = "complete"
    if published is None:
        status = "partial"
        errors.append("entry has no publication date; feed update time only bounds the snapshot")
    if not content:
        status = "partial"
        if not any("body" in error for error in errors):
            errors.append("feed provides summary or title only; article body was not fetched")
        content_status = "summary_only"
    if content_status != "full":
        status = "partial"
    source_id, _ = stable_identity(clean_text(source["name"]), url)
    return InfoRecord(
        source_name=clean_text(source["name"]),
        publisher=clean_text(source["publisher"]),
        source_id=source_id,
        title=title,
        authors=list(dict.fromkeys(authors)),
        published_at=isoformat(published) if published else None,
        listed_at=isoformat(listed),
        date_basis=date_basis,
        retrieved_at=isoformat(retrieved_at),
        url=url,
        feed_url=clean_text(source["url"]),
        homepage_url=clean_text(source["homepage"]),
        categories=categories,
        image_urls=images,
        summary=summary or title,
        content=content or summary or title,
        matched_query_groups=matched_groups,
        matched_keywords=keywords,
        relevance_reasons=reasons,
        status=status,
        errors=errors,
        content_status=content_status,
        source_role=source_role(source),
        category_hint=category_hint(source),
        scope=scope,
    )


def crawl_source(
    source: dict[str, object],
    window: Window,
    groups: dict[str, list[str]],
    limit: int,
    retrieved_at: datetime,
    max_api_pages: int = DEFAULT_MAX_API_PAGES,
    max_article_fetches: int = DEFAULT_MAX_ARTICLE_FETCHES,
) -> tuple[list[InfoRecord], SourceReport]:
    report = SourceReport(
        clean_text(source["name"]),
        clean_text(source["publisher"]),
        clean_text(source["url"]),
        clean_text(source["date_policy"]),
        "failed",
        access_mode=source_access_mode(source),
        source_role=source_role(source),
        category_hint=category_hint(source),
        scope=source_scope(source),
    )
    records: list[InfoRecord] = []
    token: Optional[str] = None
    enriched_urls: set[str] = set()
    try:
        access_mode = source_access_mode(source)
        token_env = clean_text(source.get("token_env"))
        if access_mode == "zhihu_column_api":
            session = build_session(retry_server_errors=False)
            entries = fetch_zhihu_entries(source, session, report, limit, max_api_pages)
            parsed_feed = None
        else:
            request_params = None
            if token_env:
                token = os.environ.get(token_env)
                if not token:
                    raise ValueError(f"required environment variable is not set: {token_env}")
                request_params = {"token": token}
            session = build_session(retry_server_errors=not bool(token_env))
            report.http_request_count += 1
            report.feed_request_count += 1
            response = session.get(
                clean_text(source["url"]),
                params=request_params,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if response.status_code >= 400:
                raise ValueError(f"feed request failed with HTTP {response.status_code}")
            parsed_feed = feedparser.parse(response.content)
            if parsed_feed.bozo and not parsed_feed.entries:
                raise ValueError(f"feed parse failed: {parsed_feed.bozo_exception}")
            entries = parsed_feed.entries[:limit]
            if access_mode == "rss_plus_wordpress_api":
                max_fetches = min(max_article_fetches, limit)
                report.article_fetch_count = min(
                    sum(bool(wordpress_post_id(clean_text(entry.get("link")))) for entry in entries),
                    max_fetches,
                )
                api_session = build_session(retry_server_errors=False)
                posts, fetched_urls, api_errors = fetch_wordpress_posts(
                    source, entries, api_session, report, max_fetches, max_api_pages
                )
                enriched_urls = fetched_urls
                report.errors.extend(api_errors)
                entries = [apply_wordpress_post(entry, posts.get(clean_text(entry.get("link")), {})) for entry in entries]
        report.fetched_item_count = len(entries)
        updated = feed_datetime(parsed_feed.feed) if parsed_feed is not None else None
        report.feed_updated_at = isoformat(updated) if updated else None
        snapshot_policy = source["date_policy"] == "feed_updated_snapshot"
        if snapshot_policy and updated is None:
            raise ValueError("snapshot feed is missing channel updated/lastBuildDate")
        if snapshot_policy and not (window.since <= updated < window.until):
            report.status = "success"
            return records, report
        for entry in entries:
            published, entry_date_basis = entry_datetime(entry)
            if published is not None:
                report.dated_item_count += 1
            if snapshot_policy:
                listed = updated
                date_basis = "feed_updated_snapshot"
            else:
                if published is None:
                    report.skipped_item_count += 1
                    report.errors.append(f"entry missing date: {clean_text(entry.get('title'))}")
                    continue
                listed = published
                date_basis = entry_date_basis or "entry_date"
            if not (window.since <= listed < window.until):
                continue
            report.window_item_count += 1
            content_errors: list[str] = []
            if access_mode == "rss_plus_wordpress_api":
                entry_url = clean_text(entry.get("link"))
                if entry_url not in enriched_urls:
                    content_errors.append("WordPress API did not return full article content")
            has_content = bool(entry_parts(entry)[1])
            content_status = "full" if has_content else "summary_only"
            if access_mode == "rss_plus_wordpress_api" and not has_content:
                content_errors.append("WordPress API did not return full article content")
            record = record_from_entry(
                source,
                entry,
                published,
                listed,
                date_basis,
                retrieved_at,
                groups,
                content_status,
                content_errors,
            )
            if record is None:
                continue
            report.relevant_item_count += 1
            records.append(record)
        report.record_count = len(records)
        report.partial_record_count = sum(record.status == "partial" for record in records)
        if snapshot_policy:
            report.errors.append(
                "entries have no publication dates; results are bounded by feed update time"
            )
        report.status = "partial" if report.errors or report.partial_record_count else "success"
    except (requests.RequestException, ValueError) as exc:
        error = str(exc)
        if token:
            error = error.replace(token, "<redacted>")
        report.errors.append(error)
        if token_env and ("not set" in error or "HTTP 401" in error):
            report.status = "partial"
    return records, report


def crawl(
    sources: list[dict[str, object]],
    window: Window,
    groups: dict[str, list[str]],
    limit: int,
    skip_sources: Optional[set[str]] = None,
    max_api_pages: int = DEFAULT_MAX_API_PAGES,
    max_article_fetches: int = DEFAULT_MAX_ARTICLE_FETCHES,
) -> tuple[dict[str, InfoRecord], list[SourceReport]]:
    retrieved_at = datetime.now(timezone.utc)
    records: dict[str, InfoRecord] = {}
    reports_by_name: dict[str, SourceReport] = {}
    skipped = skip_sources or set()
    configured_names = {clean_text(source["name"]) for source in sources}
    unknown = skipped - configured_names
    if unknown:
        raise ValueError(f"Unknown source(s) requested for skip: {', '.join(sorted(unknown))}")
    active_sources = []
    for source in sources:
        name = clean_text(source["name"])
        if name in skipped:
            reports_by_name[name] = SourceReport(
                name,
                clean_text(source["publisher"]),
                clean_text(source["url"]),
                clean_text(source["date_policy"]),
                "skipped",
                access_mode=source_access_mode(source),
                source_role=source_role(source),
                category_hint=category_hint(source),
                scope=source_scope(source),
                errors=["source skipped by request"],
            )
        else:
            active_sources.append(source)
    if active_sources:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(active_sources))
        ) as executor:
            results = executor.map(
                lambda source: crawl_source(
                    source,
                    window,
                    groups,
                    limit,
                    retrieved_at,
                    max_api_pages,
                    max_article_fetches,
                ),
                active_sources,
            )
            for source_records, report in results:
                reports_by_name[report.source_name] = report
                for record in source_records:
                    records[record.source_id] = record
    reports = [reports_by_name[clean_text(source["name"])] for source in sources]
    return records, reports


def safe_directory(record: InfoRecord) -> str:
    return record.source_id.replace(":", "--", 1)


def yaml_scalar(value: Optional[str]) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def yaml_list(values: Iterable[object]) -> str:
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def record_markdown(record: InfoRecord, window: Window) -> str:
    lines = [
        "---",
        "source: ai_info_source",
        f"source_name: {yaml_scalar(record.source_name)}",
        f"publisher: {yaml_scalar(record.publisher)}",
        f"source_id: {yaml_scalar(record.source_id)}",
        f"title: {yaml_scalar(record.title)}",
        f"authors: {yaml_list(record.authors)}",
        f"published_at: {yaml_scalar(record.published_at)}",
        f"listed_at: {yaml_scalar(record.listed_at)}",
        f"date_basis: {yaml_scalar(record.date_basis)}",
        f"retrieved_at: {yaml_scalar(record.retrieved_at)}",
        f"window_since: {yaml_scalar(isoformat(window.since))}",
        f"window_until: {yaml_scalar(isoformat(window.until))}",
        f"url: {yaml_scalar(record.url)}",
        f"feed_url: {yaml_scalar(record.feed_url)}",
        f"homepage_url: {yaml_scalar(record.homepage_url)}",
        f"content_status: {yaml_scalar(record.content_status)}",
        f"source_role: {yaml_scalar(record.source_role)}",
        f"category_hint: {yaml_scalar(record.category_hint)}",
        f"scope: {yaml_scalar(record.scope)}",
        f"categories: {yaml_list(record.categories)}",
        f"image_urls: {yaml_list(record.image_urls)}",
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
        "## Publisher",
        "",
        record.publisher,
        "",
        "## Authors",
        "",
        ", ".join(record.authors) or "Not provided",
        "",
        "## Feed Summary",
        "",
        record.summary or "Not provided",
        "",
        "## Original Feed Content",
        "",
        record.content or "Not provided",
        "",
        "## Relevance",
        "",
        *[f"- {reason}" for reason in record.relevance_reasons],
        "",
        "## Source",
        "",
        f"- Article: {record.url}",
        f"- Feed: {record.feed_url}",
        f"- Publisher home: {record.homepage_url}",
    ]
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


def build_manifest(
    window: Window,
    sources_path: Path,
    consensus_path: Path,
    records: dict[str, InfoRecord],
    reports: list[SourceReport],
    cycle_id: Optional[str] = None,
) -> dict[str, object]:
    succeeded = sum(report.status == "success" for report in reports)
    partial = sum(report.status == "partial" for report in reports)
    failed = sum(report.status == "failed" for report in reports)
    skipped = sum(report.status == "skipped" for report in reports)
    status = "success" if succeeded == len(reports) else "failed" if failed == len(reports) else "partial"
    return {
        "source": "ai_info_source",
        "cycle_id": cycle_id,
        "status": status,
        "retrieved_at": isoformat(datetime.now(timezone.utc)),
        "window_since": isoformat(window.since),
        "window_until": isoformat(window.until),
        "sources_config": str(sources_path),
        "consensus": str(consensus_path),
        "http_request_count": sum(report.http_request_count for report in reports),
        "source_count": len(reports),
        "successful_source_count": succeeded,
        "partial_source_count": partial,
        "failed_source_count": failed,
        "skipped_source_count": skipped,
        "record_count": len(records),
        "record_directories": [safe_directory(record) for record in records.values()],
        "sources": [asdict(report) for report in reports],
    }


def write_outputs(
    output_dir: Path,
    window: Window,
    sources_path: Path,
    consensus_path: Path,
    records: dict[str, InfoRecord],
    reports: list[SourceReport],
    dry_run: bool,
    cycle_id: Optional[str] = None,
) -> None:
    ordered = sorted(records.values(), key=lambda record: record.listed_at, reverse=True)
    if dry_run:
        for record in ordered:
            print(f"{record.publisher} {record.listed_at}: {record.title}")
        for report in reports:
            print(
                f"{report.source_name}: status={report.status} fetched={report.fetched_item_count} "
                f"window={report.window_item_count} relevant={report.relevant_item_count}"
            )
        print(f"dry-run: {len(records)} relevant records from {len(reports)} feeds")
        return
    source_dir = output_dir / "ai_info_source"
    source_dir.mkdir(parents=True, exist_ok=True)
    for record in ordered:
        write_text_atomic(
            source_dir / safe_directory(record) / "record.md",
            record_markdown(record, window),
        )
    write_text_atomic(
        source_dir / "crawl_manifest.json",
        json.dumps(
            build_manifest(window, sources_path, consensus_path, records, reports, cycle_id),
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _run_owned(args) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    repo_root = Config.load(args.project_root).project_root
    load_project_environment(repo_root)
    skill_root = Path(__file__).resolve().parents[1]
    sources_path = (args.sources or skill_root / "sources.json").resolve()
    consensus_path = (args.consensus or repo_root / "consensus.md").resolve()
    output_dir = (args.output_dir or repo_root / "working_tmp").resolve()
    try:
        window = resolve_window(args)
        sources = load_sources(sources_path)
        groups = parse_consensus(consensus_path)
        records, reports = crawl(
            sources,
            window,
            groups,
            args.max_entries_per_source,
            set(args.skip_source),
            args.max_api_pages,
            args.max_article_fetches,
        )
        write_outputs(
            output_dir,
            window,
            sources_path,
            consensus_path,
            records,
            reports,
            args.dry_run,
            args.cycle_id,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("AI info source crawler failed: %s", exc)
        return 2
    if reports and all(report.status == "failed" for report in reports):
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return source_command(args, _run_owned, "ai_info_source")


if __name__ == "__main__":
    sys.exit(main())
