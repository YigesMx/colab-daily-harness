#!/usr/bin/env python3
"""Fetch recent official AI company news from configurable feeds and sitemaps."""

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
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse, urljoin
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from colab_daily.adapters import source_command
from colab_daily.config import Config


try:
    import feedparser
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError as exc:  # pragma: no cover - CLI dependency guard
    raise SystemExit(
        "Missing dependency: run `uv sync` from the repository root."
    ) from exc


LOGGER = logging.getLogger("ai_company_official_crawler")
DEFAULT_DAYS = 3
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
DEFAULT_MAX_ENTRIES = 200
REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = "colab-daily-company-crawler/0.1"
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime


@dataclass
class SourceReport:
    source_name: str
    publisher: str
    source_type: str
    url: str
    status: str
    fetched_item_count: int = 0
    page_fetch_count: int = 0
    window_item_count: int = 0
    record_count: int = 0
    skipped_item_count: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ArticleRecord:
    source_name: str
    publisher: str
    source_id: str
    title: str
    authors: list[str]
    published_at: str
    updated_at: Optional[str]
    retrieved_at: str
    url: str
    feed_url: str
    homepage_url: str
    categories: list[str]
    image_urls: list[str]
    summary: str
    content: str
    content_type: str
    category_hint: str = "News"
    content_status: str = "full"
    source_role: str = "primary"
    status: str = "complete"
    attachments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class MetadataParser(HTMLParser):
    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.canonical: Optional[str] = None
        self.json_ld: list[object] = []
        self._json_ld_depth = 0
        self._json_ld_chunks: list[str] = []
        self._skip_depth = 0
        self._content_depth = 0
        self._title_depth = 0
        self._head_depth = 0
        self._body_chunks: list[str] = []
        self._fallback_chunks: list[str] = []
        self._title_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta[key.lower()] = values["content"]
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical = values.get("href") or self.canonical
        if tag == "script" and "ld+json" in values.get("type", "").lower():
            self._json_ld_depth = 1
            self._json_ld_chunks = []
            return
        if self._json_ld_depth:
            self._json_ld_depth += 1
            return
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in {"article", "main"}:
            self._content_depth += 1
        if tag == "title":
            self._title_depth += 1
        if tag == "head":
            self._head_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._json_ld_depth:
            self._json_ld_depth -= 1
            if self._json_ld_depth == 0:
                raw = "".join(self._json_ld_chunks).strip()
                if raw:
                    try:
                        self.json_ld.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
            return
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in {"article", "main"} and self._content_depth:
            self._content_depth -= 1
        if tag == "title" and self._title_depth:
            self._title_depth -= 1
        if tag == "head" and self._head_depth:
            self._head_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._json_ld_depth:
            self._json_ld_chunks.append(data)
        elif not self._skip_depth and not self._head_depth:
            if self._content_depth:
                self._body_chunks.append(data)
            else:
                self._fallback_chunks.append(data)
            if self._title_depth:
                self._title_chunks.append(data)

    @property
    def body(self) -> str:
        # Some official sites (Framer, pi.website) have no article/main wrappers.
        chunks = self._body_chunks or self._fallback_chunks
        return clean_text(" ".join(chunks))

    @property
    def html_title(self) -> str:
        return clean_text(" ".join(self._title_chunks))


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--since", type=parse_datetime, default=None)
    parser.add_argument("--until", type=parse_datetime, default=None)
    parser.add_argument("--cycle-id", default=None)
    parser.add_argument("--owner")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--days", type=positive_int, default=None)
    parser.add_argument(
        "--max-entries-per-source", type=positive_int, default=DEFAULT_MAX_ENTRIES
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
        raise argparse.ArgumentTypeError(
            f"invalid ISO 8601 datetime: {value}"
        ) from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_any_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip()
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


def load_sources(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"No sources configured in {path}")
    names: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Every source must be an object")
        for required in ("name", "publisher", "type", "url", "homepage"):
            if not clean_text(source.get(required)):
                raise ValueError(f"Source is missing {required}: {source}")
        name = clean_text(source["name"])
        if name in names:
            raise ValueError(f"Duplicate source name: {name}")
        names.add(name)
        if source["type"] not in {"rss", "sitemap", "blog_listing"}:
            raise ValueError(f"Unsupported source type for {name}: {source['type']}")
        if source.get("date_source", "feed") not in {"feed", "page"}:
            raise ValueError(f"Unsupported date source for {name}: {source['date_source']}")
        if source.get("feed_date_granularity", "exact") not in {"exact", "month"}:
            raise ValueError(
                f"Unsupported feed date granularity for {name}: "
                f"{source['feed_date_granularity']}"
            )
        if source.get("page_enrichment", "auto") not in {"auto", "always", "never"}:
            raise ValueError(
                f"Unsupported page enrichment for {name}: {source['page_enrichment']}"
            )
    return sources


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_html(value: str) -> str:
    parser = MetadataParser()
    parser.feed(f"<main>{value}</main>")
    return parser.body


def in_window(value: datetime, window: Window) -> bool:
    return window.since <= value < window.until


def entry_may_overlap_window(
    published: Optional[datetime], source: dict[str, object], window: Window
) -> bool:
    if published is None:
        return True
    if source.get("feed_date_granularity", "exact") != "month":
        return True
    month_start = published.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if month_start.month == 12:
        month_end = month_start.replace(year=month_start.year + 1, month=1)
    else:
        month_end = month_start.replace(month=month_start.month + 1)
    return month_start < window.until and window.since < month_end


def article_host_allowed(source: dict[str, object], url: str) -> bool:
    allowed = {clean_text(value).lower() for value in source.get("allowed_article_hosts") or []}
    if not allowed:
        return True
    return (urlparse(url).hostname or "").lower() in allowed


def stable_identity(source_name: str, url: str) -> tuple[str, str]:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{source_name}:{digest}", f"{source_name}--{digest}"


def entry_datetime(entry: object, field: str) -> Optional[datetime]:
    parsed = struct_time_to_datetime(entry.get(f"{field}_parsed"))
    return parsed or parse_any_datetime(clean_text(entry.get(field)) or None)


def entry_content(entry: object) -> str:
    contents = entry.get("content") or []
    values = [item.get("value", "") for item in contents if isinstance(item, dict)]
    return strip_html(" ".join(values))


def feed_entry_needs_page(source: dict[str, object], entry: object) -> bool:
    if source.get("date_source", "feed") == "page":
        return True
    policy = source.get("page_enrichment", "auto")
    if policy == "never":
        return False
    return policy == "always" or len(entry_content(entry)) < 400


def entry_images(entry: object) -> list[str]:
    urls: set[str] = set()
    for key in ("media_content", "media_thumbnail"):
        for item in entry.get(key) or []:
            if isinstance(item, dict) and clean_text(item.get("url")):
                urls.add(clean_text(item["url"]))
    return sorted(urls)


def metadata_value(parser: MetadataParser, *keys: str) -> Optional[str]:
    for key in keys:
        value = clean_text(parser.meta.get(key.lower()))
        if value:
            return value
    return None


def iter_json_objects(value: object) -> Iterable[dict[str, object]]:
    if isinstance(value, dict):
        yield value
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                yield from iter_json_objects(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_objects(item)


def author_names(value: object) -> list[str]:
    if isinstance(value, str):
        return [clean_text(value)] if clean_text(value) else []
    if isinstance(value, dict):
        name = clean_text(value.get("name"))
        return [name] if name else []
    if isinstance(value, list):
        return [name for item in value for name in author_names(item)]
    return []


def page_metadata(page: str, fallback_url: str) -> dict[str, object]:
    parser = MetadataParser()
    parser.feed(page)
    result: dict[str, object] = {
        "url": parser.canonical or metadata_value(parser, "og:url") or fallback_url,
        "title": metadata_value(parser, "og:title", "twitter:title") or parser.html_title,
        "description": metadata_value(parser, "og:description", "description") or "",
        "published": metadata_value(parser, "article:published_time"),
        "updated": metadata_value(parser, "article:modified_time"),
        "authors": author_names(metadata_value(parser, "author")),
        "image": metadata_value(parser, "og:image", "twitter:image"),
        "categories": [],
        "content": parser.body,
    }
    for data in parser.json_ld:
        for item in iter_json_objects(data):
            item_type = item.get("@type")
            types = item_type if isinstance(item_type, list) else [item_type]
            if not any(value in {"Article", "NewsArticle", "BlogPosting"} for value in types):
                continue
            result["title"] = clean_text(item.get("headline")) or result["title"]
            result["description"] = clean_text(item.get("description")) or result["description"]
            result["published"] = clean_text(item.get("datePublished")) or result["published"]
            result["updated"] = clean_text(item.get("dateModified")) or result["updated"]
            result["content"] = clean_text(item.get("articleBody")) or result["content"]
            authors = author_names(item.get("author"))
            if authors:
                result["authors"] = authors
            keywords = item.get("keywords")
            if isinstance(keywords, list):
                result["categories"] = [clean_text(value) for value in keywords if clean_text(value)]
            elif clean_text(keywords):
                result["categories"] = [value.strip() for value in clean_text(keywords).split(",")]
    if not result["published"]:
        match = re.search(r'\\?"publishedOn\\?"\s*:\s*\\?"([^"\\]+)', page)
        if match:
            result["published"] = match.group(1)
    if not result["updated"]:
        match = re.search(r'\\?"_updatedAt\\?"\s*:\s*\\?"([^"\\]+)', page)
        if match:
            result["updated"] = match.group(1)
    return result


def fetch_page(session: requests.Session, url: str) -> dict[str, object]:
    response = session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return page_metadata(response.text, response.url)


def record_from_feed_entry(
    source: dict[str, object],
    entry: object,
    retrieved_at: datetime,
    session: requests.Session,
) -> Optional[ArticleRecord]:
    url = clean_text(entry.get("link"))
    published = entry_datetime(entry, "published") or entry_datetime(entry, "updated")
    if not url or (published is None and source.get("date_source", "feed") != "page"):
        return None
    if not article_host_allowed(source, url):
        return None
    updated = entry_datetime(entry, "updated")
    summary = strip_html(clean_text(entry.get("summary")))
    content = entry_content(entry)
    title = clean_text(entry.get("title"))
    images = entry_images(entry)
    errors: list[str] = []
    page_authoritative_date = source.get("date_source", "feed") == "page"
    if page_authoritative_date:
        updated = None
    page_authors: list[str] = []
    page_categories: list[str] = []
    if (
        source.get("page_enrichment", "auto") == "never"
        and len(content) < 400
    ):
        errors.append("article enrichment disabled; record contains feed summary only")
    if feed_entry_needs_page(source, entry):
        try:
            metadata = fetch_page(session, url)
            page_published = parse_any_datetime(clean_text(metadata.get("published")) or None)
            if page_authoritative_date:
                if page_published is None:
                    LOGGER.warning("Official page is missing a published date: %s", url)
                    return None
                published = page_published
            url = clean_text(metadata["url"]) or url
            title = clean_text(metadata["title"]) or title
            summary = clean_text(metadata["description"]) or summary
            content = clean_text(metadata["content"]) or content
            page_authors = [
                clean_text(value)
                for value in metadata.get("authors") or []
                if clean_text(value)
            ]
            page_categories = [
                clean_text(value)
                for value in metadata.get("categories") or []
                if clean_text(value)
            ]
            page_updated = parse_any_datetime(clean_text(metadata.get("updated")) or None)
            if page_updated is not None:
                updated = page_updated
            if clean_text(metadata.get("image")):
                images.append(clean_text(metadata["image"]))
        except requests.RequestException as exc:
            LOGGER.warning("Could not enrich %s: %s", url, exc)
            if page_authoritative_date:
                return None
            errors.append(f"article enrichment failed: {exc}")
    if published is None:
        return None
    source_id, _ = stable_identity(clean_text(source["name"]), url)
    authors = [
        clean_text(author.get("name"))
        for author in entry.get("authors") or []
        if isinstance(author, dict) and clean_text(author.get("name"))
    ]
    if not authors and clean_text(entry.get("author")):
        authors = [clean_text(entry["author"])]
    if page_authors:
        authors = page_authors
    authors = list(dict.fromkeys(authors))
    categories = sorted(
        {
            clean_text(tag.get("term"))
            for tag in entry.get("tags") or []
            if isinstance(tag, dict) and clean_text(tag.get("term"))
        }
    )
    if not categories:
        categories = sorted(set(page_categories))
    return ArticleRecord(
        source_name=clean_text(source["name"]),
        publisher=clean_text(source["publisher"]),
        source_id=source_id,
        title=title,
        authors=authors,
        published_at=isoformat(published),
        updated_at=isoformat(updated) if updated else None,
        retrieved_at=isoformat(retrieved_at),
        url=url,
        feed_url=clean_text(source["url"]),
        homepage_url=clean_text(source["homepage"]),
        categories=categories,
        image_urls=sorted(set(images)),
        summary=summary,
        content=content or summary,
        content_type="feed_entry",
        content_status="summary_only" if errors else "full",
        status="partial" if errors else "complete",
        errors=errors,
    )


def sitemap_candidates(
    content: bytes,
    source: dict[str, object],
    window: Window,
    limit: int,
) -> list[tuple[str, Optional[datetime]]]:
    root = ET.fromstring(content)
    prefixes = tuple(clean_text(value) for value in source.get("include_url_prefixes") or [])
    candidates: list[tuple[str, Optional[datetime]]] = []
    for item in root.findall(f"{{{SITEMAP_NS}}}url"):
        url = clean_text(item.findtext(f"{{{SITEMAP_NS}}}loc"))
        if not url or (prefixes and not url.startswith(prefixes)):
            continue
        modified = parse_any_datetime(item.findtext(f"{{{SITEMAP_NS}}}lastmod"))
        if modified is not None and modified < window.since:
            continue
        candidates.append((url, modified))
    candidates.sort(key=lambda value: value[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return candidates[:limit]


class ListingParser(HTMLParser):
    """Collect ordered article links and visible text from a listing page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.events: list[tuple[str, str]] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in MetadataParser.SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "a":
            href = next((value for key, value in attrs if key.lower() == "href" and value), None)
            if href:
                self.events.append(("link", href))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in MetadataParser.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and clean_text(data):
            self.events.append(("text", clean_text(data)))


LISTING_DATE_RE = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?"
    r"|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b",
    re.IGNORECASE,
)


def parse_listing_date(value: str) -> Optional[datetime]:
    match = LISTING_DATE_RE.search(clean_text(value))
    if not match:
        return None
    normalized = re.sub(r"(\d{1,2})(?:st|nd|rd|th)\b", r"\1", match.group(0), flags=re.IGNORECASE)
    normalized = normalized.replace(".", "").replace(",", "")
    for pattern in ("%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(normalized, pattern).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def listing_candidates(
    content: str,
    source: dict[str, object],
    limit: int,
) -> list[tuple[str, datetime]]:
    listing_url = clean_text(source["url"])
    listing_host = (urlparse(listing_url).hostname or "").lower()
    listing_path = urlparse(listing_url).path.rstrip("/")
    prefixes = tuple(
        clean_text(value) for value in source.get("article_url_prefixes") or ["/blog/"]
    )
    parser = ListingParser()
    parser.feed(content)
    candidates: list[tuple[str, datetime]] = []
    seen: set[str] = set()
    current_url: Optional[str] = None
    for kind, value in parser.events:
        if kind == "link":
            resolved = urljoin(listing_url, value)
            parsed = urlparse(resolved)
            host = (parsed.hostname or "").lower()
            path = parsed.path or "/"
            if host != listing_host or path.rstrip("/") == listing_path:
                current_url = None
                continue
            current_url = resolved if path.startswith(prefixes) else None
            continue
        if current_url is None or current_url in seen:
            continue
        published = parse_listing_date(value)
        if published is None:
            continue
        seen.add(current_url)
        candidates.append((current_url, published))
        current_url = None
    candidates.sort(key=lambda item: item[1], reverse=True)
    return candidates[:limit]


def topic_matches(metadata: dict[str, object], source: dict[str, object]) -> bool:
    keywords = [clean_text(value) for value in source.get("topic_keywords") or []]
    if not keywords:
        return True
    text = " ".join(
        [
            clean_text(metadata.get("title")),
            clean_text(metadata.get("description")),
            clean_text(metadata.get("content"))[:5000],
            " ".join(clean_text(value) for value in metadata.get("categories") or []),
        ]
    )
    return any(re.search(re.escape(keyword), text, re.IGNORECASE) for keyword in keywords)


def record_from_sitemap_page(
    source: dict[str, object],
    metadata: dict[str, object],
    retrieved_at: datetime,
    content_type: str = "sitemap_article",
) -> Optional[ArticleRecord]:
    published = parse_any_datetime(clean_text(metadata.get("published")) or None)
    if published is None:
        return None
    url = clean_text(metadata.get("url"))
    if not url:
        return None
    source_id, _ = stable_identity(clean_text(source["name"]), url)
    authors = [
        clean_text(value)
        for value in metadata.get("authors") or []
        if clean_text(value)
    ]
    image = clean_text(metadata.get("image"))
    categories = sorted(
        {clean_text(value) for value in metadata.get("categories") or [] if clean_text(value)}
    )
    return ArticleRecord(
        source_name=clean_text(source["name"]),
        publisher=clean_text(source["publisher"]),
        source_id=source_id,
        title=clean_text(metadata.get("title")),
        authors=list(dict.fromkeys(authors)),
        published_at=isoformat(published),
        updated_at=(
            isoformat(updated)
            if (updated := parse_any_datetime(clean_text(metadata.get("updated")) or None))
            else None
        ),
        retrieved_at=isoformat(retrieved_at),
        url=url,
        feed_url=clean_text(source["url"]),
        homepage_url=clean_text(source["homepage"]),
        categories=categories,
        image_urls=[image] if image else [],
        summary=clean_text(metadata.get("description")),
        content=clean_text(metadata.get("content")),
        content_type=content_type,
    )


def crawl_rss(
    source: dict[str, object],
    session: requests.Session,
    window: Window,
    limit: int,
    retrieved_at: datetime,
) -> tuple[list[ArticleRecord], SourceReport]:
    report = SourceReport(
        clean_text(source["name"]),
        clean_text(source["publisher"]),
        "rss",
        clean_text(source["url"]),
        "failed",
    )
    records: list[ArticleRecord] = []
    try:
        response = session.get(clean_text(source["url"]), timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if feed.bozo and not feed.entries:
            raise ValueError(f"feed parse failed: {feed.bozo_exception}")
        entries = feed.entries[:limit]
        report.fetched_item_count = len(entries)
        for entry in entries:
            entry_url = clean_text(entry.get("link"))
            if not article_host_allowed(source, entry_url):
                report.skipped_item_count += 1
                continue
            published = entry_datetime(entry, "published") or entry_datetime(entry, "updated")
            page_authoritative_date = source.get("date_source", "feed") == "page"
            if published is None and not page_authoritative_date:
                report.skipped_item_count += 1
                report.errors.append(f"entry missing date: {clean_text(entry.get('title'))}")
                continue
            if not page_authoritative_date and not in_window(published, window):
                continue
            if page_authoritative_date and not entry_may_overlap_window(
                published, source, window
            ):
                continue
            if feed_entry_needs_page(source, entry):
                report.page_fetch_count += 1
            record = record_from_feed_entry(source, entry, retrieved_at, session)
            if record is None:
                report.skipped_item_count += 1
                reason = (
                    "entry missing official page date or page fetch failed"
                    if page_authoritative_date
                    else "entry missing required fields"
                )
                report.errors.append(f"{reason}: {clean_text(entry.get('title'))}")
                continue
            record_published = parse_any_datetime(record.published_at)
            if record_published is None or not in_window(record_published, window):
                continue
            report.window_item_count += 1
            records.append(record)
            report.errors.extend(f"{record.url}: {error}" for error in record.errors)
        report.record_count = len(records)
        report.status = "partial" if report.errors else "success"
    except (requests.RequestException, ValueError, ET.ParseError) as exc:
        report.errors.append(str(exc))
    return records, report


def crawl_blog_listing(
    source: dict[str, object],
    session: requests.Session,
    window: Window,
    limit: int,
    retrieved_at: datetime,
) -> tuple[list[ArticleRecord], SourceReport]:
    report = SourceReport(
        clean_text(source["name"]),
        clean_text(source["publisher"]),
        "blog_listing",
        clean_text(source["url"]),
        "failed",
    )
    records: list[ArticleRecord] = []
    try:
        response = session.get(clean_text(source["url"]), timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        candidates = listing_candidates(response.text, source, limit)
        report.fetched_item_count = len(candidates)
        for url, published in candidates:
            if not in_window(published, window):
                continue
            report.page_fetch_count += 1
            try:
                metadata = fetch_page(session, url)
            except requests.RequestException as exc:
                report.skipped_item_count += 1
                report.errors.append(f"{url}: {exc}")
                continue
            # The listing page is the authoritative date and identity source
            # because these article pages may expose homepage-root canonicals.
            metadata["published"] = isoformat(published)
            metadata["url"] = url
            report.window_item_count += 1
            record = record_from_sitemap_page(
                source, metadata, retrieved_at, content_type="blog_listing_article"
            )
            if record is not None:
                records.append(record)
        report.record_count = len(records)
        report.status = "partial" if report.errors else "success"
    except (requests.RequestException, ValueError, ET.ParseError) as exc:
        report.errors.append(str(exc))
    return records, report


def crawl_sitemap(
    source: dict[str, object],
    session: requests.Session,
    window: Window,
    limit: int,
    retrieved_at: datetime,
) -> tuple[list[ArticleRecord], SourceReport]:
    report = SourceReport(
        clean_text(source["name"]),
        clean_text(source["publisher"]),
        "sitemap",
        clean_text(source["url"]),
        "failed",
    )
    records: list[ArticleRecord] = []
    try:
        response = session.get(clean_text(source["url"]), timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        candidates = sitemap_candidates(response.content, source, window, limit)
        report.fetched_item_count = len(candidates)
        for url, _ in candidates:
            report.page_fetch_count += 1
            try:
                metadata = fetch_page(session, url)
            except requests.RequestException as exc:
                report.skipped_item_count += 1
                report.errors.append(f"{url}: {exc}")
                continue
            published = parse_any_datetime(clean_text(metadata.get("published")) or None)
            if published is None:
                report.skipped_item_count += 1
                report.errors.append(f"page missing published date: {url}")
                continue
            if not in_window(published, window):
                continue
            report.window_item_count += 1
            if not topic_matches(metadata, source):
                report.skipped_item_count += 1
                continue
            record = record_from_sitemap_page(source, metadata, retrieved_at)
            if record is not None:
                records.append(record)
        report.record_count = len(records)
        report.status = "partial" if report.errors else "success"
    except (requests.RequestException, ValueError, ET.ParseError) as exc:
        report.errors.append(str(exc))
    return records, report


def crawl(
    sources: list[dict[str, object]],
    window: Window,
    limit: int,
) -> tuple[dict[str, ArticleRecord], list[SourceReport]]:
    retrieved_at = datetime.now(timezone.utc)
    records: dict[str, ArticleRecord] = {}
    reports: list[SourceReport] = []

    def crawl_source(
        source: dict[str, object],
    ) -> tuple[list[ArticleRecord], SourceReport]:
        session = build_session()
        if source["type"] == "rss":
            return crawl_rss(source, session, window, limit, retrieved_at)
        if source["type"] == "blog_listing":
            return crawl_blog_listing(source, session, window, limit, retrieved_at)
        return crawl_sitemap(source, session, window, limit, retrieved_at)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(sources))) as executor:
        results = executor.map(crawl_source, sources)
        for source_records, report in results:
            reports.append(report)
            for record in source_records:
                records[record.source_id] = record
    return records, reports


def safe_directory(record: ArticleRecord) -> str:
    _, digest = record.source_id.split(":", 1)
    return f"{record.source_name}--{digest}"


def yaml_scalar(value: Optional[str]) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def yaml_list(values: Iterable[object]) -> str:
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def record_markdown(record: ArticleRecord, window: Window) -> str:
    lines = [
        "---",
        "source: ai_company_official",
        f"category_hint: {yaml_scalar(record.category_hint)}",
        f"content_status: {yaml_scalar(record.content_status)}",
        f"source_role: {yaml_scalar(record.source_role)}",
        f"source_name: {yaml_scalar(record.source_name)}",
        f"publisher: {yaml_scalar(record.publisher)}",
        f"source_id: {yaml_scalar(record.source_id)}",
        f"title: {yaml_scalar(record.title)}",
        f"authors: {yaml_list(record.authors)}",
        f"published_at: {yaml_scalar(record.published_at)}",
        f"updated_at: {yaml_scalar(record.updated_at)}",
        f"retrieved_at: {yaml_scalar(record.retrieved_at)}",
        f"window_since: {yaml_scalar(isoformat(window.since))}",
        f"window_until: {yaml_scalar(isoformat(window.until))}",
        f"url: {yaml_scalar(record.url)}",
        f"feed_url: {yaml_scalar(record.feed_url)}",
        f"homepage_url: {yaml_scalar(record.homepage_url)}",
        f"categories: {yaml_list(record.categories)}",
        f"image_urls: {yaml_list(record.image_urls)}",
        f"content_type: {yaml_scalar(record.content_type)}",
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
        "## Summary",
        "",
        record.summary or "Not provided",
        "",
        "## Original Content",
        "",
        record.content or "Not provided",
        "",
        "## Source",
        "",
        f"- Article: {record.url}",
        f"- Feed or sitemap: {record.feed_url}",
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


def manifest(
    window: Window,
    sources_path: Path,
    records: dict[str, ArticleRecord],
    reports: list[SourceReport],
    cycle_id: Optional[str] = None,
) -> dict[str, object]:
    succeeded = sum(report.status == "success" for report in reports)
    partial = sum(report.status == "partial" for report in reports)
    failed = sum(report.status == "failed" for report in reports)
    status = "success" if succeeded == len(reports) else "failed" if failed == len(reports) else "partial"
    content_statuses = {record.content_status for record in records.values()}
    manifest_content_status = (
        next(iter(content_statuses)) if len(content_statuses) == 1 else "mixed"
    )
    return {
        "source": "ai_company_official",
        "cycle_id": cycle_id,
        "category_hint": "News",
        "content_status": manifest_content_status,
        "source_role": "primary",
        "status": status,
        "retrieved_at": isoformat(datetime.now(timezone.utc)),
        "window_since": isoformat(window.since),
        "window_until": isoformat(window.until),
        "sources_config": str(sources_path),
        "source_count": len(reports),
        "successful_source_count": succeeded,
        "partial_source_count": partial,
        "failed_source_count": failed,
        "record_count": len(records),
        "record_directories": [safe_directory(record) for record in records.values()],
        "sources": [asdict(report) for report in reports],
    }


def write_outputs(
    output_dir: Path,
    window: Window,
    sources_path: Path,
    records: dict[str, ArticleRecord],
    reports: list[SourceReport],
    dry_run: bool,
    cycle_id: Optional[str] = None,
) -> None:
    ordered = sorted(records.values(), key=lambda record: record.published_at, reverse=True)
    if dry_run:
        for record in ordered:
            print(f"{record.publisher} {record.published_at}: {record.title}")
        print(f"dry-run: {len(records)} records from {len(reports)} sources")
        return
    source_dir = output_dir / "ai_company_official"
    source_dir.mkdir(parents=True, exist_ok=True)
    for record in ordered:
        write_text_atomic(
            source_dir / safe_directory(record) / "record.md",
            record_markdown(record, window),
        )
    write_text_atomic(
        source_dir / "crawl_manifest.json",
        json.dumps(manifest(window, sources_path, records, reports, cycle_id), ensure_ascii=False, indent=2)
        + "\n",
    )


def _run_owned(args) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    repo_root = Config.load(args.project_root).project_root
    skill_root = Path(__file__).resolve().parents[1]
    sources_path = (args.sources or skill_root / "sources.json").resolve()
    output_dir = (args.output_dir or repo_root / "working_tmp").resolve()
    if not sources_path.is_file():
        LOGGER.error("Sources file does not exist: %s", sources_path)
        return 2
    try:
        window = resolve_window(args)
        sources = load_sources(sources_path)
        records, reports = crawl(sources, window, args.max_entries_per_source)
        write_outputs(output_dir, window, sources_path, records, reports, args.dry_run, args.cycle_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("AI company crawler failed: %s", exc)
        return 2
    if reports and all(report.status == "failed" for report in reports):
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return source_command(args, _run_owned, "ai_company_official")


if __name__ == "__main__":
    sys.exit(main())
