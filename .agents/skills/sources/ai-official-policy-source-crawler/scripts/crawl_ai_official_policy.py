#!/usr/bin/env python3
"""Fetch public AI policy material from Chinese and international official sources."""

from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import logging
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
from colab_daily.adapters import source_command
from colab_daily.config import Config


try:
    import feedparser
    import requests
except ImportError as exc:  # pragma: no cover - dependency guard
    raise SystemExit("Missing dependency: run `uv sync --locked` from the repository root.") from exc


LOGGER = logging.getLogger("ai_official_policy_source_crawler")
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RECORDS = 40
MAX_REQUESTS_PER_SOURCE = 16
USER_AGENT = "colab-daily-ai-official-policy-crawler/0.1"
STATUS_VALUES = {
    "success",
    "success_empty",
    "success_stale",
    "partial",
    "failed",
    "blocked",
    "not_yet_published",
}
POLICY_SECTIONS = {
    "policy tracker",
    "global & geopolitics",
    "global and geopolitics",
    "regulation",
    "policy",
}
AI_TERMS = (
    "artificial intelligence",
    "ai",
    "人工智能",
    "智能化",
    "大模型",
    "生成式",
    "智能体",
    "具身智能",
    "机器人",
    "robot",
    "robotics",
    "embodied ai",
    "foundation model",
    "large model",
    "llm",
)
POLICY_TERMS = (
    "policy",
    "policies",
    "regulation",
    "regulatory",
    "regulator",
    "governance",
    "standard",
    "standards",
    "law",
    "bill",
    "act",
    "rule",
    "rules",
    "guidance",
    "enforcement",
    "notice",
    "consultation",
    "call for evidence",
    "request for information",
    "agency",
    "agencies",
    "government",
    "commission",
    "congress",
    "court",
    "export control",
    "safety",
    "privacy",
    "监管",
    "治理",
    "政策",
    "法规",
    "法律",
    "标准",
    "规范",
    "规划",
    "通知",
    "意见",
    "方案",
    "部门",
    "国务院",
    "工信部",
)


@dataclass(frozen=True)
class Window:
    since: datetime
    until: datetime


@dataclass
class SourceReport:
    source_name: str
    publisher: str
    source_type: str
    status: str = "failed"
    request_count: int = 0
    fetched_item_count: int = 0
    dated_item_count: int = 0
    window_item_count: int = 0
    detail_fetch_count: int = 0
    record_count: int = 0
    skipped_item_count: int = 0
    discovery_status: Optional[str] = None
    errors: list[str] = field(default_factory=list)


@dataclass
class PolicyRecord:
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
    discovery_url: str
    homepage_url: str
    source_category_hint: str
    source_role: str
    content_scope: str
    summary: str
    content: str
    url_fragment_identity: bool = False
    section: Optional[str] = None
    status: str = "complete"
    attachments: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ReadableHTMLParser(HTMLParser):
    """Keep metadata and readable article text without external HTML packages."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "header", "footer", "form"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.canonical: Optional[str] = None
        self._skip_depth = 0
        self._content_depth = 0
        self._open_tags: list[tuple[str, bool]] = []
        self._title_depth = 0
        self._title_chunks: list[str] = []
        self._content_chunks: list[str] = []
        self._body_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta[key.lower()] = values["content"]
        elif tag == "link" and "canonical" in values.get("rel", "").lower():
            self.canonical = values.get("href") or self.canonical
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        elif not self._skip_depth:
            classes = values.get("class", "").casefold()
            element_id = values.get("id", "").casefold()
            is_content_region = (
                tag in {"article", "main"}
                or "pages_content" in element_id
                or "pages_content" in classes
                or "ucap-content" in element_id
                or "ucap-content" in classes
            )
            if tag not in self.VOID_TAGS:
                self._open_tags.append((tag, is_content_region))
            if is_content_region:
                self._content_depth += 1
        if not self._skip_depth and tag == "title":
            self._title_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self.VOID_TAGS:
            return
        if tag in self.SKIP_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif not self._skip_depth:
            while self._open_tags:
                open_tag, is_content_region = self._open_tags.pop()
                if is_content_region:
                    self._content_depth -= 1
                if open_tag == tag:
                    break
        if not self._skip_depth and tag == "title" and self._title_depth:
            self._title_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._content_depth:
            self._content_chunks.append(data)
        self._body_chunks.append(data)
        if self._title_depth:
            self._title_chunks.append(data)

    @property
    def title(self) -> str:
        return clean_text(" ".join(self._title_chunks))

    @property
    def content(self) -> str:
        return clean_text(" ".join(self._content_chunks or self._body_chunks))

    @property
    def fallback_text(self) -> str:
        return clean_text(" ".join(self._content_chunks or self._body_chunks or self._title_chunks))


def clean_text(value: object) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO 8601 datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_any_datetime(value: object) -> Optional[datetime]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = float(value)
        if timestamp <= 0:
            return None
        if timestamp > 100_000_000_000:
            timestamp /= 1000
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = clean_text(value)
    if not text:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return parse_any_datetime(float(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
        except (TypeError, ValueError, OverflowError):
            match = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", text)
            if not match:
                return None
            parsed = datetime(
                int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=timezone.utc
            )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_struct_time(value: object) -> Optional[datetime]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(calendar.timegm(value), timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def entry_datetime(entry: object) -> Optional[datetime]:
    for name in ("published", "updated", "created", "date", "time"):
        parsed = parse_struct_time(entry.get(f"{name}_parsed"))
        if parsed:
            return parsed
        parsed = parse_any_datetime(entry.get(name))
        if parsed:
            return parsed
    return None


def in_window(value: Optional[datetime], window: Window) -> bool:
    return value is not None and window.since <= value < window.until


def default_repo_root() -> Path:
    return Path(__file__).resolve().parents[5]


def resolve_window(args: argparse.Namespace, now: Optional[datetime] = None) -> Window:
    if (args.since is None) != (args.until is None):
        raise ValueError("--since and --until must be supplied together")
    if args.since is not None and args.until is not None:
        window = Window(args.since, args.until)
    else:
        current = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TIMEZONE)
        until_local = datetime.combine(current.date(), datetime.min.time(), LOCAL_TIMEZONE)
        window = Window(
            (until_local - timedelta(days=1)).astimezone(timezone.utc),
            until_local.astimezone(timezone.utc),
        )
    if window.since >= window.until:
        raise ValueError("window must satisfy since < until")
    return window


def load_sources(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"No sources configured in {path}")
    names: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("every source must be an object")
        for required in ("name", "publisher", "homepage", "role", "type"):
            if not clean_text(source.get(required)):
                raise ValueError(f"source is missing {required}")
        name = clean_text(source["name"])
        if name in names:
            raise ValueError(f"duplicate source name: {name}")
        names.add(name)
        if source["role"] not in {"primary", "secondary"}:
            raise ValueError(f"invalid source role for {name}")
        if source["type"] not in {
            "state_council_search",
            "miit_dynamic",
            "ai_policy_daily",
            "federal_register_api",
            "ec_digital_strategy_rss",
            "govuk_policy_atom",
            "samr_national_standard_api",
        }:
            raise ValueError(f"invalid source type for {name}")
        if source["type"] == "state_council_search" and clean_text(source.get("search_url")) != (
            "https://sousuo.www.gov.cn/search-gov/data"
        ):
            raise ValueError(f"State Council source must use tested search endpoint: {name}")
        if source["type"] == "miit_dynamic":
            if clean_text(source.get("category_url")) != (
                "https://www.miit.gov.cn/search-front-server/api/structure/list-category"
            ):
                raise ValueError(f"MIIT source must use tested category endpoint: {name}")
            if clean_text(source.get("search_url")) != (
                "https://www.miit.gov.cn/search-front-server/api/search/info"
            ):
                raise ValueError(f"MIIT source must use tested search endpoint: {name}")
            if not clean_text(source.get("website_id")) or int(source.get("search_id", 0)) != 51:
                raise ValueError(f"MIIT source is missing tested search configuration: {name}")
        if source["type"] == "federal_register_api" and clean_text(source.get("api_url")) != (
            "https://www.federalregister.gov/api/v1/documents.json"
        ):
            raise ValueError(f"Federal Register source must use tested API endpoint: {name}")
        if source["type"] == "ec_digital_strategy_rss" and clean_text(source.get("feed_url")) != (
            "https://digital-strategy.ec.europa.eu/en/rss.xml"
        ):
            raise ValueError(f"EU Digital Strategy source must use tested RSS endpoint: {name}")
        if source["type"] == "govuk_policy_atom":
            feed_url = clean_text(source.get("feed_url"))
            if feed_url != (
                "https://www.gov.uk/search/policy-papers-and-consultations.atom"
                "?keywords=%22artificial+intelligence%22"
                "&organisations%5B%5D=department-for-science-innovation-and-technology"
            ):
                raise ValueError(f"GOV.UK source must use tested DSIT policy Atom endpoint: {name}")
        if source["type"] == "samr_national_standard_api":
            if clean_text(source.get("api_url")) != (
                "https://std.samr.gov.cn/noc/search/nocGBPage"
            ):
                raise ValueError(f"SAMR source must use tested national-standard API endpoint: {name}")
            if clean_text(source.get("listing_url")) != "https://std.samr.gov.cn/noc/nocGB":
                raise ValueError(f"SAMR source must use the official listing page: {name}")
        raw = json.dumps(source, ensure_ascii=False).lower()
        if "jiqizhixin" in raw or "machine-heart" in raw or "token" in raw:
            raise ValueError(f"restricted tokenized source is not allowed: {name}")
    required_types = {
        "state_council_search",
        "miit_dynamic",
        "ai_policy_daily",
        "federal_register_api",
        "ec_digital_strategy_rss",
        "govuk_policy_atom",
        "samr_national_standard_api",
    }
    if {source["type"] for source in sources} != required_types:
        raise ValueError("the policy source set must contain every configured source type")
    return sources


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5"})
    return session


def response_text(response: object) -> str:
    value = getattr(response, "text", None)
    if value is not None:
        return str(value)
    content = getattr(response, "content", b"")
    return content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)


def response_json(response: object) -> object:
    method = getattr(response, "json", None)
    if callable(method):
        return method()
    return json.loads(response_text(response))


def classify_http(response: object) -> Optional[str]:
    code = int(getattr(response, "status_code", 200))
    body = response_text(response).lower()
    if code in {401, 403, 429} or any(term in body for term in ("captcha", "verify you are human", "access denied")):
        return "blocked"
    if code >= 400:
        return "failed"
    return None


def request(
    session: requests.Session,
    report: SourceReport,
    method: str,
    url: str,
    *,
    params: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
) -> object:
    if report.request_count >= MAX_REQUESTS_PER_SOURCE:
        raise RuntimeError("source request budget exhausted")
    report.request_count += 1
    kwargs: dict[str, Any] = {"timeout": REQUEST_TIMEOUT_SECONDS}
    if params is not None:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    if headers:
        kwargs["headers"] = headers
    call = getattr(session, method.lower())
    response = call(url, **kwargs)
    classification = classify_http(response)
    if classification:
        code = int(getattr(response, "status_code", 0) or 0)
        raise HTTPSourceError(classification, f"HTTP {code or '?'}", code or None)
    return response


class HTTPSourceError(Exception):
    def __init__(self, status: str, message: str, code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def stable_identity(source_name: str, url: str) -> tuple[str, str]:
    # AI Policy Daily exposes multiple stories on one dated page; keep anchors
    # in the identity so separate stories do not overwrite each other.
    canonical = url.rstrip("/")
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return f"{source_name}:{digest}", f"{source_name}--{digest}"


def content_scope(content: str, *, authoritative: bool = False) -> str:
    length = len(clean_text(content))
    if authoritative and length >= 1000:
        return "full"
    if length >= 300:
        return "substantial"
    return "summary_only"


def policy_relevant(title: str, body: str, section: Optional[str] = None) -> bool:
    if section and clean_text(section).casefold() in POLICY_SECTIONS:
        return True
    text = f"{title} {body}".casefold()
    def has_term(term: str) -> bool:
        if term.casefold() == "ai":
            return re.search(r"(?<![A-Za-z])AI(?![A-Za-z])", f"{title} {body}") is not None
        escaped = re.escape(term.casefold())
        if re.fullmatch(r"[a-z][a-z -]*", term.casefold()):
            return re.search(rf"(?<![a-z]){escaped}(?![a-z])", text) is not None
        return term.casefold() in text

    return any(has_term(term) for term in AI_TERMS) and any(has_term(term) for term in POLICY_TERMS)


def normalize_url(url: str, base: str, allowed_host: Optional[str] = None) -> Optional[str]:
    value = urljoin(base, clean_text(url))
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if allowed_host and (parsed.hostname or "").lower() != allowed_host.lower():
        return None
    return value


def metadata_from_html(raw: str, fallback_url: str) -> dict[str, Any]:
    parser = ReadableHTMLParser()
    parser.feed(raw)
    title = clean_text(parser.meta.get("og:title") or parser.meta.get("twitter:title") or parser.title)
    description = clean_text(parser.meta.get("og:description") or parser.meta.get("description"))
    published = (
        parser.meta.get("article:published_time")
        or parser.meta.get("date")
        or parser.meta.get("publishdate")
        or parser.meta.get("pubdate")
        or parser.meta.get("published")
    )
    if not published:
        date_match = re.search(
            r"(?:发布时间|发布日期|发稿时间|Published|Date)\s*[:：]?\s*([^<|\n]{8,40})",
            raw,
            flags=re.IGNORECASE,
        )
        published = clean_text(date_match.group(1)) if date_match else None
    return {
        "url": normalize_url(parser.canonical or parser.meta.get("og:url") or fallback_url, fallback_url)
        or fallback_url,
        "title": title,
        "description": description,
        "published": published,
        "content": parser.content or parser.fallback_text or description,
    }


def candidate_value(item: dict[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if isinstance(value, dict):
            value = value.get("value") or value.get("text")
        if clean_text(value):
            return clean_text(value)
    return ""


def recursive_candidates(value: object, base_url: str, allowed_host: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[str] = set()

    def visit(node: object) -> None:
        if isinstance(node, dict):
            title = candidate_value(node, "title", "title_no_tag", "name", "headline")
            url = normalize_url(candidate_value(node, "url", "link", "linkUrl", "externalUrl"), base_url, allowed_host)
            if title and url and url not in seen:
                seen.add(url)
                found.append(
                    {
                        "title": title,
                        "url": url,
                        "summary": candidate_value(node, "summary", "content", "description"),
                        "published": candidate_value(node, "time", "date", "published", "publishTime"),
                    }
                )
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return found


def state_council_candidates(value: object, base_url: str, allowed_host: str) -> list[dict[str, Any]]:
    """Read the tested search-gov response under searchVO.catMap.*.listVO."""
    if not isinstance(value, dict):
        return []
    search_vo = value.get("searchVO")
    cat_map = search_vo.get("catMap") if isinstance(search_vo, dict) else None
    if not isinstance(cat_map, dict):
        return []
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category in cat_map.values():
        if not isinstance(category, dict):
            continue
        list_vo = category.get("listVO")
        if not isinstance(list_vo, list):
            continue
        for item in list_vo:
            if not isinstance(item, dict):
                continue
            url = normalize_url(candidate_value(item, "url", "link"), base_url, allowed_host)
            title = candidate_value(item, "title", "title_no_tag", "name")
            if not url or not title or url in seen:
                continue
            seen.add(url)
            candidates.append(
                {
                    "title": title,
                    "url": url,
                    "summary": candidate_value(item, "summary", "content", "description"),
                    "published": (
                        item.get("pubtime")
                        or candidate_value(item, "pubtimeStr", "time", "date")
                        or item.get("ptime")
                    ),
                }
            )
    return candidates


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: Optional[str] = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        if tag.lower() == "a":
            values = {key.lower(): value or "" for key, value in attrs}
            self._href = values.get("href") or None
            self._chunks = []

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href:
            self.links.append((self._href, clean_text(" ".join(self._chunks))))
            self._href = None
            self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._chunks.append(data)


def html_candidates(raw: str, base_url: str, allowed_host: str) -> list[dict[str, Any]]:
    parser = LinkParser()
    parser.feed(raw)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for href, title in parser.links:
        url = normalize_url(href, base_url, allowed_host)
        if not url or not title or url in seen:
            continue
        seen.add(url)
        result.append({"title": title, "url": url, "summary": "", "published": ""})
    return result


def record_from_candidate(
    source: dict[str, Any],
    candidate: dict[str, Any],
    metadata: dict[str, Any],
    window: Window,
    retrieved_at: datetime,
    *,
    discovery_url: str,
    date_basis: str,
    section: Optional[str] = None,
    authoritative: bool = False,
) -> Optional[PolicyRecord]:
    allowed_host = {
        "state_council_search": "www.gov.cn",
        "miit_dynamic": "www.miit.gov.cn",
    }.get(clean_text(source.get("type")))
    url = normalize_url(candidate.get("url") or metadata.get("url"), discovery_url, allowed_host)
    title = clean_text(metadata.get("title") or candidate.get("title"))
    summary = clean_text(metadata.get("description") or candidate.get("summary"))
    content = clean_text(metadata.get("content") or summary)
    published = parse_any_datetime(metadata.get("published") or candidate.get("published"))
    if not url or not title or not published or not in_window(published, window):
        return None
    if not policy_relevant(title, f"{summary} {content}", section):
        return None
    source_id, _ = stable_identity(clean_text(source["name"]), url)
    return PolicyRecord(
        source_name=clean_text(source["name"]),
        publisher=clean_text(source["publisher"]),
        source_id=source_id,
        title=title,
        authors=[],
        published_at=isoformat(published),
        listed_at=isoformat(published),
        date_basis=date_basis,
        retrieved_at=isoformat(retrieved_at),
        url=url,
        discovery_url=discovery_url,
        homepage_url=clean_text(source["homepage"]),
        source_category_hint="Policy",
        source_role=clean_text(source["role"]),
        content_scope=content_scope(content, authoritative=authoritative),
        summary=summary or title,
        content=content or summary or title,
        section=section,
    )


def state_payload(source: dict[str, Any], keyword: str, window: Window) -> dict[str, Any]:
    since_local = window.since.astimezone(LOCAL_TIMEZONE)
    last_local = (window.until - timedelta(microseconds=1)).astimezone(LOCAL_TIMEZONE)
    return {
        "t": "zhengcelibrary",
        "q": keyword,
        "searchfield": "title",
        "timetype": "2",
        "mintime": since_local.date().isoformat(),
        "maxtime": last_local.date().isoformat(),
        "sort": "time",
        "sortType": "1",
        "p": 1,
        "n": min(int(source.get("page_size", 20)), 20),
    }


def fetch_detail(
    session: requests.Session,
    report: SourceReport,
    candidate: dict[str, Any],
    allowed_host: str,
) -> dict[str, Any]:
    url = normalize_url(candidate["url"], candidate["url"], allowed_host)
    if not url:
        raise ValueError("detail URL is outside the configured official host")
    report.detail_fetch_count += 1
    response = request(session, report, "get", url)
    return metadata_from_html(response_text(response), url)


def crawl_state_council(
    source: dict[str, Any], window: Window, retrieved_at: datetime, session: requests.Session
) -> tuple[list[PolicyRecord], SourceReport]:
    report = SourceReport(clean_text(source["name"]), clean_text(source["publisher"]), source["type"])
    records: dict[str, PolicyRecord] = {}
    candidates: list[dict[str, Any]] = []
    try:
        for keyword in list(source.get("keywords") or [])[:2]:
            response = request(
                session,
                report,
                "get",
                clean_text(source["search_url"]),
                params=state_payload(source, clean_text(keyword), window),
            )
            candidates.extend(
                state_council_candidates(response_json(response), clean_text(source["homepage"]), "www.gov.cn")
            )
        report.fetched_item_count = len(candidates)
        unique: dict[str, dict[str, Any]] = {item["url"]: item for item in candidates}
        for candidate in list(unique.values())[: int(source.get("max_details", 12))]:
            listed = parse_any_datetime(candidate.get("published"))
            if listed:
                report.dated_item_count += 1
                if not in_window(listed, window):
                    continue
            try:
                metadata = fetch_detail(session, report, candidate, "www.gov.cn")
            except HTTPSourceError as exc:
                report.skipped_item_count += 1
                report.errors.append(f"{candidate['url']}: {exc}")
                if exc.status == "blocked":
                    report.status = "blocked"
                    return [], report
                continue
            except (requests.RequestException, ValueError) as exc:
                report.skipped_item_count += 1
                report.errors.append(f"{candidate['url']}: {exc}")
                continue
            record = record_from_candidate(
                source,
                candidate,
                metadata,
                window,
                retrieved_at,
                discovery_url=clean_text(source["search_url"]),
                date_basis="detail_published",
                authoritative=True,
            )
            if record:
                report.window_item_count += 1
                records[record.source_id] = record
        report.record_count = len(records)
        if report.status == "blocked":
            return list(records.values()), report
        report.status = "partial" if report.errors else ("success" if records else "success_empty")
    except HTTPSourceError as exc:
        report.status = exc.status
        report.errors.append(str(exc))
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        report.status = "failed"
        report.errors.append(str(exc))
    return list(records.values()), report


def feed_entries(raw: bytes | str) -> tuple[list[object], Optional[datetime]]:
    parsed = feedparser.parse(raw)
    entries = list(parsed.entries)
    feed = parsed.feed
    updated = parse_struct_time(feed.get("updated_parsed")) or parse_any_datetime(feed.get("updated"))
    if not updated:
        updated = parse_any_datetime(feed.get("lastBuildDate"))
    return entries, updated


def miit_category_payload(source: dict[str, Any]) -> dict[str, Any]:
    return {
        "websiteid": clean_text(source.get("website_id")),
        "searchid": int(source.get("search_id", 51)),
    }


def extract_miit_category_ids(value: object) -> list[int]:
    if not isinstance(value, dict) or value.get("success") is not True:
        raise ValueError("MIIT category API did not report success")
    data = value.get("data")
    categories = data.get("categories") if isinstance(data, dict) else None
    if not isinstance(categories, list) or not categories:
        raise ValueError("MIIT category API returned no categories")
    category_ids: list[int] = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        iid = category.get("iid")
        if isinstance(iid, int) and iid > 0:
            category_ids.append(iid)
        elif clean_text(iid).isdigit() and int(clean_text(iid)) > 0:
            category_ids.append(int(clean_text(iid)))
    if not category_ids:
        raise ValueError("MIIT category API returned no usable iid")
    return category_ids


def miit_search_payload(
    source: dict[str, Any], keyword: str, window: Window, category_id: int
) -> dict[str, Any]:
    since_local = window.since.astimezone(LOCAL_TIMEZONE)
    last_local = (window.until - timedelta(microseconds=1)).astimezone(LOCAL_TIMEZONE)
    page_size = min(int(source.get("page_size", 10)), 10)
    return {
        "websiteid": clean_text(source.get("website_id")),
        "scope": "basic",
        "q": keyword,
        "pg": page_size,
        "cateid": category_id,
        "pos": "title_text,titlepy,infocontent,filenumbername,keyword,contentdescribe",
        "pq": "",
        "oq": "",
        "eq": "",
        "begin": since_local.date().isoformat(),
        "end": last_local.date().isoformat(),
        "dateField": "deploytime",
        "selectFields": (
            "title,content,deploytime,_index,url,cdate,infoextends,infocontentattribute,"
            "keyword,contentdescribe,sectitle,picpath,columnname,themename,publishgroupname,"
            "publishtime,metaid,bexxgk,columnid"
        ),
        "group": "distinct",
        "highlightConfigs": json.dumps(
            [
                {
                    "field": "infocontent",
                    "numberOfFragments": 2,
                    "fragmentOffset": 0,
                    "fragmentSize": 110,
                    "noMatchSize": 110,
                }
            ],
            separators=(",", ":"),
        ),
        "highlightFields": "title_text,infocontent,webid",
        "level": 6,
        "sortFields": '[{"name":"extend1","type":"desc"},{"name":"jsearch_score","type":"desc"}]',
        "hidCol": "fbafd13557fa453d9c59432567d2b150",
        "p": 1,
    }


def miit_search_candidates(value: object, base_url: str, allowed_host: str) -> list[dict[str, Any]]:
    if not isinstance(value, dict) or value.get("success") is not True:
        raise ValueError("MIIT search API did not report success")
    data = value.get("data")
    result = data.get("searchResult") if isinstance(data, dict) else None
    results = result.get("dataResults") if isinstance(result, dict) else None
    if results is None:
        return []
    if not isinstance(results, list):
        raise ValueError("MIIT search API returned invalid dataResults")
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result_item in results:
        group_data = result_item.get("groupData") if isinstance(result_item, dict) else None
        if not isinstance(group_data, list):
            continue
        for group_item in group_data:
            item = group_item.get("data") if isinstance(group_item, dict) else None
            if not isinstance(item, dict):
                continue
            url = normalize_url(candidate_value(item, "url"), base_url, allowed_host)
            title = candidate_value(item, "title_text", "title")
            if not url or not title or url in seen:
                continue
            seen.add(url)
            candidates.append(
                {
                    "title": title,
                    "url": url,
                    "summary": candidate_value(item, "contentdescribe", "infocontent", "content"),
                    "published": item.get("deploytime") or item.get("jsearch_date") or item.get("publishtime"),
                }
            )
    return candidates


def rss_candidate_url(candidate: object) -> tuple[str, str]:
    if isinstance(candidate, dict):
        return clean_text(candidate.get("url")), clean_text(candidate.get("name") or "optional RRSdy RSS")
    return clean_text(candidate), "optional RRSdy RSS"


def crawl_miit(
    source: dict[str, Any], window: Window, retrieved_at: datetime, session: requests.Session
) -> tuple[list[PolicyRecord], SourceReport]:
    report = SourceReport(clean_text(source["name"]), clean_text(source["publisher"]), source["type"])
    records: dict[str, PolicyRecord] = {}
    candidates: dict[str, dict[str, Any]] = {}
    dynamic_ok = False
    rss_statuses: list[str] = []
    category_ids: list[int] = []
    try:
        category_response = request(
            session,
            report,
            "get",
            clean_text(source["category_url"]),
            params=miit_category_payload(source),
        )
        dynamic_ok = True
        category_ids = extract_miit_category_ids(response_json(category_response))
    except HTTPSourceError as exc:
        report.errors.append(f"category API: {exc}")
        if exc.status == "blocked":
            report.status = "blocked"
            return [], report
    except (requests.RequestException, ValueError) as exc:
        report.errors.append(f"category API: {exc}")

    try:
        if not category_ids:
            raise ValueError("no dynamic MIIT category iid")
        search_response = request(
            session,
            report,
            "get",
            clean_text(source["search_url"]),
            params=miit_search_payload(
                source, clean_text(source["keywords"][0]), window, category_ids[0]
            ),
        )
        dynamic_ok = True
        for item in miit_search_candidates(
            response_json(search_response), clean_text(source["homepage"]), "www.miit.gov.cn"
        ):
            candidates[item["url"]] = item
    except HTTPSourceError as exc:
        report.errors.append(f"search API: {exc}")
        if exc.status == "blocked" and not dynamic_ok:
            report.status = "blocked"
            return [], report
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        report.errors.append(f"search API: {exc}")

    report.fetched_item_count = len(candidates)
    for rss_candidate in list(source.get("rss_candidates") or [])[:2]:
        rss_url, rss_name = rss_candidate_url(rss_candidate)
        if not rss_url:
            continue
        try:
            response = request(session, report, "get", clean_text(rss_url))
            entries, updated = feed_entries(getattr(response, "content", response_text(response)))
            entries = entries[:20]
            report.fetched_item_count += len(entries)
            dated = [entry_datetime(entry) for entry in entries]
            dated_values = [value for value in dated if value]
            newest = max(dated_values, default=updated)
            if newest is not None and newest < window.since:
                rss_statuses.append("success_stale")
            else:
                rss_statuses.append("success")
            for entry in entries[:20]:
                url = normalize_url(entry.get("link"), clean_text(source["homepage"]), "www.miit.gov.cn")
                title = clean_text(entry.get("title"))
                if url and title:
                    candidates.setdefault(
                        url,
                        {"title": title, "url": url, "summary": clean_text(entry.get("summary")), "published": entry_datetime(entry)},
                    )
        except HTTPSourceError as exc:
            rss_status = "stale_404" if exc.code == 404 else exc.status
            rss_statuses.append(rss_status)
            report.errors.append(f"RSS {rss_name} {rss_url}: {exc}")
        except (requests.RequestException, ValueError) as exc:
            rss_statuses.append("failed")
            report.errors.append(f"RSS {rss_name} {rss_url}: {exc}")
    report.discovery_status = ",".join(rss_statuses) if rss_statuses else None
    for candidate in list(candidates.values())[: int(source.get("max_details", 12))]:
        listed = parse_any_datetime(candidate.get("published"))
        if listed:
            report.dated_item_count += 1
            if not in_window(listed, window):
                continue
        try:
            metadata = fetch_detail(session, report, candidate, "www.miit.gov.cn")
        except HTTPSourceError as exc:
            report.errors.append(f"{candidate['url']}: {exc}")
            if exc.status == "blocked":
                report.status = "blocked"
                break
            continue
        except (requests.RequestException, ValueError) as exc:
            report.errors.append(f"{candidate['url']}: {exc}")
            continue
        record = record_from_candidate(
            source,
            candidate,
            metadata,
            window,
            retrieved_at,
            discovery_url=clean_text(source["search_url"]),
            date_basis="detail_published",
            authoritative=True,
        )
        if record:
            report.window_item_count += 1
            records[record.source_id] = record
    report.record_count = len(records)
    if report.status == "blocked":
        return list(records.values()), report
    if not dynamic_ok and not records:
        report.status = "blocked" if rss_statuses and all(s == "blocked" for s in rss_statuses) else "failed"
    elif records and report.errors:
        report.status = "partial"
    elif (
        not records
        and rss_statuses
        and all(status in {"success_stale", "stale_404"} for status in rss_statuses)
        and any(status in {"success_stale", "stale_404"} for status in rss_statuses)
        and dynamic_ok
        and all(error.startswith("RSS ") for error in report.errors)
    ):
        report.status = "success_stale"
    elif not records:
        report.status = "partial" if report.errors else ("success_empty" if dynamic_ok else "failed")
    else:
        report.status = "success"
    return list(records.values()), report


def markdown_date(raw: str) -> Optional[date]:
    match = re.search(r"^date:\s*['\"]?(\d{4}-\d{2}-\d{2})", raw, re.MULTILINE)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def split_daily_stories(raw: str) -> list[tuple[Optional[str], str, str]]:
    lines = raw.replace("\r\n", "\n").splitlines()
    has_markdown_markers = any(
        re.match(r"^#{1,3}\s+", line) or re.match(r"^\s*-\s+\*\*", line)
        for line in lines
    )
    if not has_markdown_markers:
        return split_plain_daily_stories(lines)
    section: Optional[str] = None
    stories: list[tuple[Optional[str], str, str]] = []
    current: Optional[list[str]] = None
    current_title = ""

    def finish() -> None:
        nonlocal current, current_title
        if current is not None and current_title:
            body = clean_text(" ".join(current))
            if body:
                stories.append((section, current_title, body))
        current = None
        current_title = ""

    for line in lines:
        heading = re.match(r"^#{1,3}\s+(.+?)\s*$", line)
        if heading:
            finish()
            section = clean_text(re.sub(r"^[IVX]+[.]\s*", "", heading.group(1)))
            continue
        bullet = re.match(r"^\s*-\s+\*\*(.+?)\*\*\s*(?:—|-|:)\s*(.*)$", line)
        if bullet:
            finish()
            current_title = clean_text(bullet.group(1))
            current = [clean_text(bullet.group(2))]
            continue
        if current is not None and clean_text(line):
            current.append(clean_text(line))
    finish()
    return stories


def split_plain_daily_stories(lines: list[str]) -> list[tuple[Optional[str], str, str]]:
    """Parse the text archive representation, which has no Markdown markers."""
    section = None
    stories: list[tuple[Optional[str], str, str]] = []
    current: Optional[list[str]] = None
    for line in lines:
        value = clean_text(line)
        if not value or value.casefold().startswith("read at "):
            if current:
                title = current[0]
                body = clean_text(" ".join(current[1:]))
                if body:
                    stories.append((section, title, body))
                current = None
            continue
        heading = re.match(r"^[IVXLCDM]+\.\s*(.+)$", value, re.IGNORECASE)
        if heading:
            if current:
                title = current[0]
                body = clean_text(" ".join(current[1:]))
                if body:
                    stories.append((section, title, body))
                current = None
            section = clean_text(heading.group(1))
            continue
        if current is None:
            current = [value]
        else:
            current.append(value)
    if current:
        title = current[0]
        body = clean_text(" ".join(current[1:]))
        if body:
            stories.append((section, title, body))
    return stories


def daily_record(
    source: dict[str, Any],
    issue_url: str,
    issue_date: date,
    story_position: int,
    section: Optional[str],
    title: str,
    content: str,
    listed_at: datetime,
    retrieved_at: datetime,
) -> Optional[PolicyRecord]:
    if not policy_relevant(title, content, section):
        return None
    if story_position <= 0:
        raise ValueError("story_position must be greater than zero")
    archive_template = clean_text(source.get("archive_url"))
    stable_issue_url = (
        archive_template.format(date=issue_date.isoformat())
        if archive_template
        else issue_url
    )
    url = f"{stable_issue_url.rstrip('/')}/#story-{story_position:04d}"
    source_id, _ = stable_identity(clean_text(source["name"]), url)
    return PolicyRecord(
        source_name=clean_text(source["name"]),
        publisher=clean_text(source["publisher"]),
        source_id=source_id,
        title=title,
        authors=[],
        published_at=isoformat(listed_at),
        listed_at=isoformat(listed_at),
        date_basis="daily_issue_date",
        retrieved_at=isoformat(retrieved_at),
        url=url,
        discovery_url=clean_text(source["feed_url"]),
        homepage_url=clean_text(source["homepage"]),
        source_category_hint="Policy",
        source_role=clean_text(source["role"]),
        content_scope=content_scope(content),
        summary=content[:400],
        content=content,
        url_fragment_identity=True,
        section=section,
    )


def crawl_ai_policy_daily(
    source: dict[str, Any], window: Window, retrieved_at: datetime, session: requests.Session
) -> tuple[list[PolicyRecord], SourceReport]:
    report = SourceReport(clean_text(source["name"]), clean_text(source["publisher"]), source["type"])
    records: dict[str, PolicyRecord] = {}
    try:
        feed_response = request(session, report, "get", clean_text(source["feed_url"]))
        entries, feed_updated = feed_entries(getattr(feed_response, "content", response_text(feed_response)))
        report.fetched_item_count = len(entries)
        in_window_entries: list[tuple[object, datetime]] = []
        for entry in entries[: int(source.get("max_feed_items", 40))]:
            published = entry_datetime(entry)
            if published:
                report.dated_item_count += 1
            if published and in_window(published, window):
                in_window_entries.append((entry, published))
        if not in_window_entries:
            if feed_updated and feed_updated < window.since:
                report.status = "success_stale"
            else:
                report.status = "success_empty"
            return [], report
        for entry, published in in_window_entries[: int(source.get("max_details", 5))]:
            issue_url = normalize_url(entry.get("link"), clean_text(source["homepage"]))
            issue_match = re.search(r"/(\d{4}-\d{2}-\d{2})/?$", issue_url or "")
            issue_date = date.fromisoformat(issue_match.group(1)) if issue_match else published.date()
            raw: Optional[str] = None
            page_url = ""
            for template_key in ("markdown_url", "text_url"):
                page_url = clean_text(source[template_key]).format(date=issue_date.isoformat())
                report.detail_fetch_count += 1
                try:
                    response = request(session, report, "get", page_url)
                    candidate_raw = response_text(response)
                    if candidate_raw.strip():
                        raw = candidate_raw
                        break
                except HTTPSourceError as exc:
                    if exc.status == "blocked":
                        report.errors.append(f"{page_url}: {exc}")
                        report.status = "blocked"
                        break
                    # A missing Markdown representation is expected before text is published.
                    continue
                except (requests.RequestException, ValueError) as exc:
                    report.errors.append(f"{page_url}: {exc}")
                    continue
            if report.status == "blocked":
                break
            if raw is None:
                report.errors.append(f"issue not published: {issue_date.isoformat()}")
                report.status = "not_yet_published"
                continue
            page_date = markdown_date(raw)
            if page_date and page_date != issue_date:
                report.errors.append(f"issue date mismatch: {issue_date.isoformat()} vs {page_date.isoformat()}")
                continue
            for story_position, (section, title, content) in enumerate(
                split_daily_stories(raw), start=1
            ):
                record = daily_record(
                    source,
                    issue_url or page_url,
                    issue_date,
                    story_position,
                    section,
                    title,
                    content,
                    published,
                    retrieved_at,
                )
                if record:
                    if record.source_id in records:
                        raise ValueError(
                            f"duplicate AI Policy Daily source_id: {record.source_id}"
                        )
                    records[record.source_id] = record
                    report.window_item_count += 1
        report.record_count = len(records)
        if report.status == "blocked":
            return list(records.values()), report
        if records and report.errors:
            report.status = "partial"
        elif records:
            report.status = "success"
        elif report.status != "not_yet_published":
            report.status = "success_empty"
    except HTTPSourceError as exc:
        report.status = exc.status
        report.errors.append(str(exc))
    except (requests.RequestException, ValueError, ET.ParseError) as exc:
        report.status = "failed"
        report.errors.append(str(exc))
    return list(records.values()), report


def direct_record(
    source: dict[str, Any],
    window: Window,
    *,
    title: str,
    url: str,
    summary: str,
    content: str,
    published: Optional[datetime],
    listed_at: Optional[datetime],
    retrieved_at: datetime,
    discovery_url: str,
    date_basis: str,
    section: Optional[str] = None,
) -> Optional[PolicyRecord]:
    """Build a source record from an official API/feed payload without a detail fetch."""
    title = clean_text(title)
    summary = clean_text(summary)
    content = clean_text(content)
    published = published or listed_at
    if not title or not url or not published or not in_window(published, window):
        return None
    if not policy_relevant(title, f"{summary} {content}", section):
        return None
    return PolicyRecord(
        source_name=clean_text(source["name"]),
        publisher=clean_text(source["publisher"]),
        source_id=stable_identity(clean_text(source["name"]), url)[0],
        title=title,
        authors=[],
        published_at=isoformat(published),
        listed_at=isoformat(listed_at or published),
        date_basis=date_basis,
        retrieved_at=isoformat(retrieved_at),
        url=url,
        discovery_url=discovery_url,
        homepage_url=clean_text(source["homepage"]),
        source_category_hint="Policy",
        source_role=clean_text(source["role"]),
        content_scope=content_scope(content),
        summary=summary or title,
        content=content or summary or title,
        section=section,
    )


def local_date_bounds(window: Window) -> tuple[date, date]:
    since = window.since.astimezone(LOCAL_TIMEZONE).date()
    until = (window.until - timedelta(microseconds=1)).astimezone(LOCAL_TIMEZONE).date()
    return since, until


def federal_register_payload(source: dict[str, Any], window: Window) -> dict[str, Any]:
    since_date, until_date = local_date_bounds(window)
    fields = [
        "title",
        "abstract",
        "html_url",
        "body_html_url",
        "publication_date",
        "type",
        "agencies",
        "document_number",
    ]
    return {
        "conditions[term]": clean_text(source.get("term", "artificial intelligence")),
        "conditions[publication_date][gte]": since_date.isoformat(),
        "conditions[publication_date][lte]": until_date.isoformat(),
        "order": "newest",
        "per_page": min(int(source.get("page_size", 40)), 100),
        "fields[]": fields,
    }


def crawl_federal_register(
    source: dict[str, Any], window: Window, retrieved_at: datetime, session: requests.Session
) -> tuple[list[PolicyRecord], SourceReport]:
    report = SourceReport(clean_text(source["name"]), clean_text(source["publisher"]), source["type"])
    records: dict[str, PolicyRecord] = {}
    try:
        response = request(
            session,
            report,
            "get",
            clean_text(source["api_url"]),
            params=federal_register_payload(source, window),
        )
        payload = response_json(response)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            if isinstance(payload, dict) and payload.get("count") == 0:
                results = []
            else:
                raise ValueError("Federal Register API returned no results array")
        report.fetched_item_count = len(results)
        for item in results[: int(source.get("max_records", 12))]:
            if not isinstance(item, dict):
                continue
            published = parse_any_datetime(item.get("publication_date"))
            if published:
                report.dated_item_count += 1
            agencies = ", ".join(
                clean_text(agency.get("name"))
                for agency in item.get("agencies", [])
                if isinstance(agency, dict) and clean_text(agency.get("name"))
            )
            document_type = clean_text(item.get("type"))
            abstract = clean_text(item.get("abstract"))
            url = normalize_url(item.get("html_url") or item.get("body_html_url"), clean_text(source["homepage"]), "www.federalregister.gov")
            record = direct_record(
                source,
                window,
                title=clean_text(item.get("title")),
                url=url or "",
                summary=abstract,
                content=" | ".join(
                    part
                    for part in (
                        f"Document type: {document_type}" if document_type else "",
                        f"Agencies: {agencies}" if agencies else "",
                        abstract,
                    )
                    if part
                ),
                published=published,
                listed_at=published,
                retrieved_at=retrieved_at,
                discovery_url=clean_text(source["api_url"]),
                date_basis="official_publication_date",
            )
            if record:
                report.window_item_count += 1
                records[record.source_id] = record
        report.record_count = len(records)
        report.status = "success" if records else "success_empty"
    except HTTPSourceError as exc:
        report.status = exc.status
        report.errors.append(str(exc))
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        report.status = "failed"
        report.errors.append(str(exc))
    return list(records.values()), report


def crawl_official_feed(
    source: dict[str, Any], window: Window, retrieved_at: datetime, session: requests.Session
) -> tuple[list[PolicyRecord], SourceReport]:
    report = SourceReport(clean_text(source["name"]), clean_text(source["publisher"]), source["type"])
    records: dict[str, PolicyRecord] = {}
    allowed_host = (
        "digital-strategy.ec.europa.eu"
        if source["type"] == "ec_digital_strategy_rss"
        else "www.gov.uk"
    )
    try:
        feed_response = request(session, report, "get", clean_text(source["feed_url"]))
        entries, _ = feed_entries(getattr(feed_response, "content", response_text(feed_response)))
        entries = entries[: int(source.get("max_feed_items", 20))]
        report.fetched_item_count = len(entries)
        dated_values: list[datetime] = []
        in_window_entries: list[tuple[object, datetime]] = []
        for entry in entries:
            published = entry_datetime(entry)
            if published:
                report.dated_item_count += 1
                dated_values.append(published)
                if in_window(published, window):
                    in_window_entries.append((entry, published))
        for entry, published in in_window_entries:
            url = normalize_url(entry.get("link"), clean_text(source["homepage"]), allowed_host)
            title = clean_text(entry.get("title"))
            summary = clean_text(entry.get("summary") or entry.get("description"))
            record = direct_record(
                source,
                window,
                title=title,
                url=url or "",
                summary=summary,
                content=summary or title,
                published=published,
                listed_at=published,
                retrieved_at=retrieved_at,
                discovery_url=clean_text(source["feed_url"]),
                date_basis="feed_published",
            )
            if record:
                report.window_item_count += 1
                records[record.source_id] = record
        report.record_count = len(records)
        if records:
            report.status = "success"
        else:
            newest = max(dated_values, default=None)
            report.status = "success_stale" if newest and newest < window.since else "success_empty"
    except HTTPSourceError as exc:
        report.status = exc.status
        report.errors.append(str(exc))
    except (requests.RequestException, ValueError, ET.ParseError) as exc:
        report.status = "failed"
        report.errors.append(str(exc))
    return list(records.values()), report


def samr_candidates(value: object) -> list[dict[str, Any]]:
    rows = value.get("rows") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError("SAMR API returned no rows array")
    candidates: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = clean_text(row.get("TITLE"))
        published = parse_any_datetime(row.get("NOTICE_DATE"))
        code = clean_text(row.get("CODE"))
        row_id = clean_text(row.get("id"))
        if title and published and code:
            candidates.append(
                {"title": title, "published": published, "code": code, "id": row_id}
            )
    return candidates


def crawl_samr_standards(
    source: dict[str, Any], window: Window, retrieved_at: datetime, session: requests.Session
) -> tuple[list[PolicyRecord], SourceReport]:
    report = SourceReport(clean_text(source["name"]), clean_text(source["publisher"]), source["type"])
    records: dict[str, PolicyRecord] = {}
    try:
        response = request(
            session,
            report,
            "get",
            clean_text(source["api_url"]),
            params={
                "searchText": clean_text(source.get("search_text", "人工智能")),
                "pageNumber": 1,
                "pageSize": min(int(source.get("page_size", 20)), 50),
            },
        )
        candidates = samr_candidates(response_json(response))
        report.fetched_item_count = len(candidates)
        listing_url = clean_text(source["listing_url"])
        for candidate in candidates[: int(source.get("max_records", 12))]:
            published: Optional[datetime] = candidate["published"]
            if published:
                report.dated_item_count += 1
            query = f"?noticeCode={candidate['code']}"
            if candidate["id"]:
                query += f"&recordId={candidate['id']}"
            record = direct_record(
                source,
                window,
                title=candidate["title"],
                url=f"{listing_url}{query}",
                summary=candidate["title"],
                content=f"国家标准公告 {candidate['code']}，发布日期 {published.date().isoformat() if published else 'unknown'}。{candidate['title']}",
                published=published,
                listed_at=published,
                retrieved_at=retrieved_at,
                discovery_url=clean_text(source["api_url"]),
                date_basis="official_notice_date",
            )
            if record:
                report.window_item_count += 1
                records[record.source_id] = record
        report.record_count = len(records)
        report.status = "success" if records else "success_empty"
    except HTTPSourceError as exc:
        report.status = exc.status
        report.errors.append(str(exc))
    except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
        report.status = "failed"
        report.errors.append(str(exc))
    return list(records.values()), report


def crawl(
    sources: list[dict[str, Any]], window: Window, max_records: int = DEFAULT_MAX_RECORDS
) -> tuple[dict[str, PolicyRecord], list[SourceReport]]:
    retrieved_at = datetime.now(timezone.utc)
    records: dict[str, PolicyRecord] = {}
    reports: list[SourceReport] = []
    for source in sources:
        session = build_session()
        if source["type"] == "state_council_search":
            source_records, report = crawl_state_council(source, window, retrieved_at, session)
        elif source["type"] == "miit_dynamic":
            source_records, report = crawl_miit(source, window, retrieved_at, session)
        elif source["type"] == "federal_register_api":
            source_records, report = crawl_federal_register(source, window, retrieved_at, session)
        elif source["type"] in {"ec_digital_strategy_rss", "govuk_policy_atom"}:
            source_records, report = crawl_official_feed(source, window, retrieved_at, session)
        elif source["type"] == "samr_national_standard_api":
            source_records, report = crawl_samr_standards(source, window, retrieved_at, session)
        else:
            source_records, report = crawl_ai_policy_daily(source, window, retrieved_at, session)
        reports.append(report)
        for record in source_records:
            if len(records) >= max_records:
                break
            records[record.source_id] = record
    return records, reports


def yaml_scalar(value: Optional[str]) -> str:
    return "null" if value is None else json.dumps(value, ensure_ascii=False)


def yaml_list(values: Iterable[object]) -> str:
    return "[" + ", ".join(json.dumps(value, ensure_ascii=False) for value in values) + "]"


def record_markdown(record: PolicyRecord, window: Window) -> str:
    lines = [
        "---",
        "source: ai_official_policy_source",
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
        f"url_fragment_identity: {'true' if record.url_fragment_identity else 'false'}",
        f"discovery_url: {yaml_scalar(record.discovery_url)}",
        f"homepage_url: {yaml_scalar(record.homepage_url)}",
        f"source_category_hint: {yaml_scalar(record.source_category_hint)}",
        f"source_role: {yaml_scalar(record.source_role)}",
        f"content_scope: {yaml_scalar(record.content_scope)}",
        f"section: {yaml_scalar(record.section)}",
        f"status: {record.status}",
        f"attachments: {yaml_list(record.attachments)}",
        f"errors: {yaml_list(record.errors)}",
        "---",
        "",
        f"# {record.title}",
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
        f"- Article or issue: {record.url}",
        f"- Discovery: {record.discovery_url}",
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


def manifest(window: Window, sources_path: Path, records: dict[str, PolicyRecord], reports: list[SourceReport], cycle_id: Optional[str] = None) -> dict[str, Any]:
    statuses = [report.status for report in reports]
    if statuses and all(status == "blocked" for status in statuses):
        overall = "blocked"
    elif statuses and all(status in {"failed", "blocked"} for status in statuses):
        overall = "failed"
    elif any(status in {"partial", "failed", "blocked", "success_stale", "not_yet_published"} for status in statuses):
        overall = "partial"
    elif records:
        overall = "success"
    else:
        overall = "success_empty"
    return {
        "source": "ai_official_policy_source",
        "cycle_id": cycle_id,
        "status": overall,
        "retrieved_at": isoformat(datetime.now(timezone.utc)),
        "window_since": isoformat(window.since),
        "window_until": isoformat(window.until),
        "sources_config": str(sources_path),
        "source_count": len(reports),
        "request_count": sum(report.request_count for report in reports),
        "record_count": len(records),
        "record_directories": [stable_identity(record.source_name, record.url)[1] for record in records.values()],
        "sources": [asdict(report) for report in reports],
    }


def write_outputs(
    output_dir: Path,
    window: Window,
    sources_path: Path,
    records: dict[str, PolicyRecord],
    reports: list[SourceReport],
    dry_run: bool,
    cycle_id: Optional[str] = None,
) -> None:
    ordered = sorted(records.values(), key=lambda record: record.listed_at or "", reverse=True)
    if dry_run:
        for record in ordered:
            print(f"{record.source_name} {record.published_at}: {record.title}")
        for report in reports:
            print(f"{report.source_name}: status={report.status} records={report.record_count} requests={report.request_count}")
        return
    source_dir = output_dir / "ai_official_policy_source"
    for record in ordered:
        write_text_atomic(source_dir / stable_identity(record.source_name, record.url)[1] / "record.md", record_markdown(record, window))
    write_text_atomic(
        source_dir / "crawl_manifest.json",
        json.dumps(manifest(window, sources_path, records, reports, cycle_id), ensure_ascii=False, indent=2) + "\n",
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=None)
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--since", type=parse_datetime, default=None)
    parser.add_argument("--until", type=parse_datetime, default=None)
    parser.add_argument("--cycle-id", default=None)
    parser.add_argument("--owner")
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--max-records", type=int, default=DEFAULT_MAX_RECORDS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def _run_owned(args) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    repo_root = Config.load(args.project_root).project_root
    skill_root = Path(__file__).resolve().parents[1]
    sources_path = (args.sources or skill_root / "sources.json").resolve()
    output_dir = (args.output_dir or repo_root / "working_tmp").resolve()
    try:
        if args.max_records <= 0:
            raise ValueError("--max-records must be greater than zero")
        window = resolve_window(args)
        sources = load_sources(sources_path)
        records, reports = crawl(sources, window, args.max_records)
        write_outputs(output_dir, window, sources_path, records, reports, args.dry_run, args.cycle_id)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("AI official policy crawler failed: %s", exc)
        return 2
    if reports and all(report.status in {"failed", "blocked"} for report in reports):
        return 1
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    return source_command(args, _run_owned, "ai_official_policy_source")


if __name__ == "__main__":
    sys.exit(main())
