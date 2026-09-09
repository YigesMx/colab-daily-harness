import importlib.util
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "crawl_ai_info_sources.py"
SPEC = importlib.util.spec_from_file_location("crawl_ai_info_sources", SCRIPT)
assert SPEC and SPEC.loader
crawler = importlib.util.module_from_spec(SPEC)
sys.modules["crawl_ai_info_sources"] = crawler
SPEC.loader.exec_module(crawler)


class AIInfoSourceCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.window = crawler.Window(
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.groups = {
            "embodied_robotics": ["robot learning", "具身智能", "机器人学习"],
            "arm_vla": ["VLA", "机械臂"],
            "drone_vla_vln": ["drone", "无人机"],
            "embodied_infra": ["robot dataset", "机器人数据集"],
            "world_model": ["world model", "世界模型"],
            "llm_vlm_methods": ["LLM", "agent", "大语言模型", "智能体"],
        }

    def test_default_window_is_previous_shanghai_day_in_utc(self):
        args = crawler.parse_args([])
        window = crawler.resolve_window(
            args, datetime(2026, 8, 1, 17, 23, tzinfo=timezone.utc)
        )

        self.assertIsNone(args.days)
        self.assertEqual(
            window.since, datetime(2026, 7, 31, 16, tzinfo=timezone.utc)
        )
        self.assertEqual(
            window.until, datetime(2026, 8, 1, 16, tzinfo=timezone.utc)
        )

    def test_cycle_id_is_parsed_and_written_to_manifest(self):
        args = crawler.parse_args(["--cycle-id", "daily-2026-08-01"])
        result = crawler.build_manifest(
            self.window, Path("sources.json"), Path("consensus.md"), {}, [], args.cycle_id
        )
        self.assertEqual(args.cycle_id, "daily-2026-08-01")
        self.assertEqual(result["cycle_id"], "daily-2026-08-01")

    def test_sources_config_has_eight_sources_without_plaintext_token(self):
        path = SKILL_DIR / "sources.json"
        raw = path.read_text(encoding="utf-8")
        sources = crawler.load_sources(path)
        self.assertEqual(
            {source["name"] for source in sources},
            {
                "mit_technology_review_ai",
                "ai_insider",
                "towards_ai",
                "jiqizhixin_official",
                "paperweekly",
                "embodied_ai_heart",
                "xinzhiyuan",
                "qbitai",
            },
        )
        self.assertNotIn("sk-", raw)
        self.assertTrue(all(source["category_hint"] == "News" for source in sources))
        self.assertTrue(all(source["scope"] == "broad_news" for source in sources))
        jiqizhixin = next(
            source for source in sources if source["name"] == "jiqizhixin_official"
        )
        self.assertEqual(jiqizhixin["token_env"], "JIQIZHIXIN_RSS_TOKEN")
        self.assertEqual(jiqizhixin["access_mode"], "rss")
        embodied = next(source for source in sources if source["name"] == "embodied_ai_heart")
        self.assertEqual(embodied["publisher"], "具身智能之心")
        self.assertEqual(
            embodied["url"],
            "https://www.zhihu.com/api/v4/columns/c_1823331372888109056/articles",
        )
        self.assertEqual(embodied["access_mode"], "zhihu_column_api")
        self.assertEqual(
            next(source for source in sources if source["name"] == "xinzhiyuan")["access_mode"],
            "rss_plus_wordpress_api",
        )
        self.assertEqual(
            next(source for source in sources if source["name"] == "qbitai")["api_url"],
            "https://www.qbitai.com/wp-json/wp/v2/posts",
        )
        self.assertEqual(
            crawler.wordpress_post_id(
                "https://aiera.com.cn/2026/08/06/embodied/admin/107436/article/"
            ),
            "107436",
        )

    def test_project_dotenv_loads_token_without_overriding_process_environment(self):
        key = "DOTENV_TEST_RSS_TOKEN"
        original = os.environ.get(key)
        try:
            os.environ.pop(key, None)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".env").write_text(f"{key}=dotenv-token\n", encoding="utf-8")
                crawler.load_project_environment(root)
                self.assertEqual(os.environ[key], "dotenv-token")

                os.environ[key] = "process-token"
                (root / ".env").write_text(f"{key}=replacement-token\n", encoding="utf-8")
                crawler.load_project_environment(root)
                self.assertEqual(os.environ[key], "process-token")
        finally:
            if original is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = original

    def test_direct_topic_accepts_single_chinese_keyword(self):
        groups, keywords, reasons = crawler.assess_relevance(
            "新的机器人学习系统", "", self.groups
        )
        self.assertEqual(groups, ["embodied_robotics"])
        self.assertEqual(keywords, ["机器人学习"])
        self.assertIn("keyword in title (embodied_robotics): 机器人学习", reasons)

    def test_secondary_body_requires_two_keywords(self):
        self.assertEqual(
            crawler.assess_relevance("General update", "an agent system", self.groups)[0],
            [],
        )
        groups, keywords, _ = crawler.assess_relevance(
            "General update", "an LLM agent system", self.groups
        )
        self.assertEqual(groups, ["llm_vlm_methods"])
        self.assertEqual(keywords, ["agent", "LLM"])

    def test_secondary_title_accepts_one_keyword(self):
        groups, _, _ = crawler.assess_relevance("A safer LLM", "", self.groups)
        self.assertEqual(groups, ["llm_vlm_methods"])

    def test_broad_news_scope_keeps_entry_without_consensus_keyword(self):
        groups, keywords, reasons = crawler.assess_relevance(
            "AI industry update",
            "A general technology announcement",
            self.groups,
            "broad_news",
        )
        self.assertEqual(groups, [])
        self.assertEqual(keywords, [])
        self.assertEqual(reasons, ["broad_news scope: retained for downstream rating"])

    def test_broad_news_record_does_not_require_keyword_match(self):
        record = crawler.record_from_entry(
            {
                "name": "news",
                "publisher": "News",
                "url": "https://example.com/feed",
                "homepage": "https://example.com",
                "date_policy": "entry",
                "category_hint": "News",
                "scope": "broad_news",
            },
            {
                "title": "AI industry update",
                "link": "https://example.com/article",
                "summary": "A general technology announcement",
                "content": [],
            },
            datetime(2026, 7, 30, tzinfo=timezone.utc),
            datetime(2026, 7, 30, tzinfo=timezone.utc),
            "entry_published",
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            self.groups,
        )
        self.assertIsNotNone(record)
        self.assertEqual(record.category_hint, "News")
        self.assertEqual(record.scope, "broad_news")

    def test_entry_date_basis_tracks_fallback_field(self):
        value, basis = crawler.entry_datetime(
            {"updated": "Thu, 30 Jul 2026 12:00:00 GMT"}
        )
        self.assertEqual(value, datetime(2026, 7, 30, 12, tzinfo=timezone.utc))
        self.assertEqual(basis, "entry_updated")

    def test_snapshot_feed_uses_channel_date_without_faking_publication_date(self):
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <title>Wechat mirror</title><lastBuildDate>Thu, 30 Jul 2026 23:13:52 GMT</lastBuildDate>
        <item><title>具身智能新进展</title><link>https://example.com/a</link>
        <description>具身智能新进展</description></item></channel></rss>""".encode()

        class Response:
            content = feed
            status_code = 200

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, params=None, timeout=None):
                return Response()

        original = crawler.build_session
        crawler.build_session = lambda retry_server_errors=True: Session()
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "wechat",
                    "publisher": "Wechat",
                    "url": "https://example.com/feed.xml",
                    "homepage": "https://example.com",
                    "date_policy": "feed_updated_snapshot",
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        finally:
            crawler.build_session = original

        self.assertEqual(len(records), 1)
        self.assertIsNone(records[0].published_at)
        self.assertEqual(records[0].date_basis, "feed_updated_snapshot")
        self.assertEqual(records[0].status, "partial")
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.window_item_count, 1)
        self.assertEqual(report.partial_record_count, 1)

    def test_entry_dated_feed_filters_before_relevance(self):
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>A robot learning release</title><link>https://example.com/new</link>
        <pubDate>Thu, 30 Jul 2026 12:00:00 GMT</pubDate><description>robot learning</description></item>
        <item><title>Old robot learning</title><link>https://example.com/old</link>
        <pubDate>Tue, 28 Jul 2026 12:00:00 GMT</pubDate><description>robot learning</description></item>
        </channel></rss>"""

        class Response:
            content = feed
            status_code = 200

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, params=None, timeout=None):
                return Response()

        original = crawler.build_session
        crawler.build_session = lambda retry_server_errors=True: Session()
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "dated",
                    "publisher": "Dated",
                    "url": "https://example.com/feed.xml",
                    "homepage": "https://example.com",
                    "date_policy": "entry",
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        finally:
            crawler.build_session = original

        self.assertEqual(len(records), 1)
        self.assertEqual(report.fetched_item_count, 2)
        self.assertEqual(report.window_item_count, 1)
        self.assertEqual(report.relevant_item_count, 1)

    def test_record_markdown_contains_filter_evidence(self):
        source_id, _ = crawler.stable_identity("media", "https://example.com/a")
        record = crawler.InfoRecord(
            source_name="media",
            publisher="Media",
            source_id=source_id,
            title="Robot learning",
            authors=["Author"],
            published_at="2026-07-30T12:00:00+00:00",
            listed_at="2026-07-30T12:00:00+00:00",
            date_basis="entry_published",
            retrieved_at="2026-08-01T00:00:00+00:00",
            url="https://example.com/a",
            feed_url="https://example.com/feed.xml",
            homepage_url="https://example.com",
            categories=["AI"],
            image_urls=[],
            summary="Summary",
            content="Original content",
            matched_query_groups=["embodied_robotics"],
            matched_keywords=["robot learning"],
            relevance_reasons=["keyword in title (embodied_robotics): robot learning"],
        )
        output = crawler.record_markdown(record, self.window)
        self.assertIn("source: ai_info_source", output)
        self.assertIn('matched_query_groups: ["embodied_robotics"]', output)
        self.assertIn("Original content", output)

    def test_manifest_reports_exactly_one_request_per_feed(self):
        reports = [
            crawler.SourceReport(
                "a", "A", "https://a", "entry", "success", http_request_count=1
            ),
            crawler.SourceReport(
                "b", "B", "https://b", "entry", "success", http_request_count=1
            ),
        ]
        manifest = crawler.build_manifest(
            self.window, Path("sources.json"), Path("consensus.md"), {}, reports
        )
        self.assertEqual(manifest["http_request_count"], 2)

    def test_skipped_source_is_reported_without_building_a_session(self):
        sources = [
            {
                "name": "limited",
                "publisher": "Limited",
                "url": "https://example.com/rss",
                "homepage": "https://example.com",
                "date_policy": "entry",
                "token_env": "LIMITED_TOKEN",
            }
        ]
        original_session = crawler.build_session
        crawler.build_session = lambda retry_server_errors=True: self.fail(
            "skipped source must not build an HTTP session"
        )
        try:
            records, reports = crawler.crawl(
                sources, self.window, self.groups, 10, {"limited"}
            )
        finally:
            crawler.build_session = original_session

        self.assertEqual(records, {})
        self.assertEqual(reports[0].status, "skipped")
        self.assertEqual(reports[0].http_request_count, 0)
        manifest = crawler.build_manifest(
            self.window, Path("sources.json"), Path("consensus.md"), records, reports
        )
        self.assertEqual(manifest["status"], "partial")
        self.assertEqual(manifest["skipped_source_count"], 1)

    def test_unknown_skipped_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown source.*typo"):
            crawler.crawl([], self.window, self.groups, 10, {"typo"})

    def test_token_is_read_from_environment_but_not_written_to_report(self):
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Robot learning</title><link>https://example.com/a</link>
        <pubDate>Thu, 30 Jul 2026 12:00:00 GMT</pubDate>
        <description>robot learning</description></item></channel></rss>"""

        class Response:
            content = feed
            status_code = 200

            def raise_for_status(self):
                return None

        class Session:
            params = None

            def get(self, url, params=None, timeout=None):
                self.params = params
                return Response()

        session = Session()
        original_session = crawler.build_session
        original_token = crawler.os.environ.get("TEST_RSS_TOKEN")
        session_retry_setting = None

        def build_session(retry_server_errors=True):
            nonlocal session_retry_setting
            session_retry_setting = retry_server_errors
            return session

        crawler.build_session = build_session
        crawler.os.environ["TEST_RSS_TOKEN"] = "secret-token"
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "official",
                    "publisher": "Official",
                    "url": "https://example.com/rss",
                    "homepage": "https://example.com",
                    "date_policy": "entry",
                    "token_env": "TEST_RSS_TOKEN",
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        finally:
            crawler.build_session = original_session
            if original_token is None:
                crawler.os.environ.pop("TEST_RSS_TOKEN", None)
            else:
                crawler.os.environ["TEST_RSS_TOKEN"] = original_token

        self.assertEqual(session.params, {"token": "secret-token"})
        self.assertFalse(session_retry_setting)
        self.assertEqual(len(records), 1)
        self.assertEqual(report.url, "https://example.com/rss")
        self.assertEqual(report.http_request_count, 1)
        self.assertNotIn("secret-token", json.dumps(crawler.asdict(report)))

    def test_missing_token_fails_only_that_source_without_exposing_a_value(self):
        original = crawler.os.environ.pop("MISSING_TEST_TOKEN", None)
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "official",
                    "publisher": "Official",
                    "url": "https://example.com/rss",
                    "homepage": "https://example.com",
                    "date_policy": "entry",
                    "token_env": "MISSING_TEST_TOKEN",
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        finally:
            if original is not None:
                crawler.os.environ["MISSING_TEST_TOKEN"] = original

        self.assertEqual(records, [])
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.http_request_count, 0)
        self.assertEqual(
            report.errors,
            ["required environment variable is not set: MISSING_TEST_TOKEN"],
        )

    def test_tokenized_http_error_is_redacted_and_not_retried_by_crawler(self):
        class Response:
            content = b""
            status_code = 429

        class Session:
            calls = 0

            def get(self, url, params=None, timeout=None):
                self.calls += 1
                return Response()

        session = Session()
        original_session = crawler.build_session
        original_token = crawler.os.environ.get("RATE_LIMIT_TOKEN")
        crawler.build_session = lambda retry_server_errors=True: session
        crawler.os.environ["RATE_LIMIT_TOKEN"] = "sensitive-token"
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "limited",
                    "publisher": "Limited",
                    "url": "https://example.com/rss",
                    "homepage": "https://example.com",
                    "date_policy": "entry",
                    "token_env": "RATE_LIMIT_TOKEN",
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        finally:
            crawler.build_session = original_session
            if original_token is None:
                crawler.os.environ.pop("RATE_LIMIT_TOKEN", None)
            else:
                crawler.os.environ["RATE_LIMIT_TOKEN"] = original_token

        self.assertEqual(records, [])
        self.assertEqual(session.calls, 1)
        self.assertEqual(report.http_request_count, 1)
        self.assertEqual(report.errors, ["feed request failed with HTTP 429"])
        self.assertNotIn("sensitive-token", json.dumps(crawler.asdict(report)))

    def test_zhihu_column_api_fetches_bounded_public_pages(self):
        class Response:
            status_code = 200

            def json(self):
                return {
                    "paging": {"is_end": True},
                    "data": [
                        {
                            "id": 123,
                            "title": "具身智能机器人学习新进展",
                            "url": "https://zhuanlan.zhihu.com/p/123",
                            "created": 1785412800,
                            "excerpt": "具身智能与机器人学习",
                            "content": "<p>具身智能与机器人学习的公开内容。</p>",
                            "author": {"name": "具身智能之心"},
                            "title_image": "https://img.example/zhihu.jpg",
                        }
                    ],
                }

        class Session:
            calls = []

            def get(self, url, params=None, timeout=None):
                self.calls.append((url, params, timeout))
                return Response()

        session = Session()
        original_session = crawler.build_session
        crawler.build_session = lambda retry_server_errors=True: session
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "embodied_ai_heart",
                    "publisher": "具身智能之心",
                    "url": "https://www.zhihu.com/api/v4/columns/c_1823331372888109056/articles",
                    "homepage": "https://www.zhihu.com/column/c_1823331372888109056",
                    "date_policy": "entry",
                    "access_mode": "zhihu_column_api",
                    "source_role": "specialized_community",
                    "category_hint": "News",
                    "scope": "broad_news",
                    "page_size": 1,
                    "max_pages": 1,
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                max_api_pages=1,
            )
        finally:
            crawler.build_session = original_session

        self.assertEqual(len(records), 1)
        self.assertEqual(report.status, "success")
        self.assertEqual(report.http_request_count, 1)
        self.assertEqual(report.api_request_count, 1)
        self.assertEqual(report.page_fetch_count, 1)
        self.assertEqual(
            session.calls[0],
            (
                "https://www.zhihu.com/api/v4/columns/c_1823331372888109056/articles",
                {"limit": 1, "offset": 0},
                crawler.REQUEST_TIMEOUT_SECONDS,
            ),
        )
        self.assertEqual(records[0].content_status, "full")
        self.assertEqual(records[0].source_role, "specialized_community")
        self.assertIn("https://img.example/zhihu.jpg", records[0].image_urls)

    def test_zhihu_column_api_reports_page_limit_without_unbounded_fetching(self):
        class Response:
            status_code = 200

            def json(self):
                return {
                    "paging": {"is_end": False},
                    "data": [
                        {
                            "title": "具身智能机器人学习新进展",
                            "url": "https://zhuanlan.example/p/123",
                            "created": 1785412800,
                            "excerpt": "具身智能与机器人学习",
                            "content": "具身智能与机器人学习的公开内容。",
                        }
                    ],
                }

        class Session:
            calls = 0

            def get(self, url, params=None, timeout=None):
                self.calls += 1
                return Response()

        session = Session()
        original_session = crawler.build_session
        crawler.build_session = lambda retry_server_errors=False: session
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "embodied_ai_heart",
                    "publisher": "具身智能之心",
                    "url": "https://www.zhihu.com/api/v4/columns/c_1823331372888109056/articles",
                    "homepage": "https://www.zhihu.com/column/c_1823331372888109056",
                    "date_policy": "entry",
                    "access_mode": "zhihu_column_api",
                    "category_hint": "News",
                    "scope": "broad_news",
                    "page_size": 1,
                    "max_pages": 3,
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                max_api_pages=1,
            )
        finally:
            crawler.build_session = original_session

        self.assertEqual(session.calls, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(report.status, "partial")
        self.assertIn("page limit", " ".join(report.errors))

    def test_wordpress_api_enriches_rss_without_unbounded_article_requests(self):
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>RSS title</title><link>https://qbit.example/2026/07/123.html</link>
        <pubDate>Thu, 30 Jul 2026 12:00:00 GMT</pubDate><description>具身智能摘要</description>
        </item></channel></rss>""".encode()

        class Response:
            def __init__(self, content=b"", status_code=200, payload=None):
                self.content = content
                self.status_code = status_code
                self.payload = payload

            def json(self):
                return self.payload

        class Session:
            calls = []

            def get(self, url, params=None, timeout=None):
                self.calls.append((url, params, timeout))
                if url.endswith("feed"):
                    return Response(feed)
                return Response(
                    status_code=200,
                    payload=[
                        {
                            "id": 123,
                            "link": "https://qbit.example/2026/07/123.html",
                            "title": {"rendered": "WordPress full title"},
                            "excerpt": {"rendered": "WordPress excerpt"},
                            "content": {"rendered": "<p>具身智能完整正文</p>"},
                            "date_gmt": "2026-07-30T12:00:00",
                        }
                    ],
                )

        session = Session()
        original_session = crawler.build_session
        crawler.build_session = lambda retry_server_errors=True: session
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "qbitai",
                    "publisher": "量子位",
                    "url": "https://qbit.example/feed",
                    "homepage": "https://qbit.example",
                    "date_policy": "entry",
                    "access_mode": "rss_plus_wordpress_api",
                    "api_url": "https://qbit.example/wp-json/wp/v2/posts",
                    "page_size": 20,
                    "max_pages": 1,
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                max_api_pages=1,
                max_article_fetches=1,
            )
        finally:
            crawler.build_session = original_session

        self.assertEqual(len(session.calls), 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].title, "WordPress full title")
        self.assertEqual(records[0].published_at, "2026-07-30T12:00:00+00:00")
        self.assertEqual(records[0].content_status, "full")
        self.assertIn("具身智能完整正文", records[0].content)
        self.assertEqual(report.http_request_count, 2)
        self.assertEqual(report.feed_request_count, 1)
        self.assertEqual(report.api_request_count, 1)
        self.assertEqual(report.article_fetch_count, 1)
        self.assertEqual(report.page_fetch_count, 1)

    def test_wordpress_api_401_is_partial_and_keeps_rss_record(self):
        feed = """<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>具身智能 RSS</title><link>https://qbit.example/2026/07/123.html</link>
        <pubDate>Thu, 30 Jul 2026 12:00:00 GMT</pubDate><description>具身智能摘要</description>
        </item></channel></rss>""".encode()

        class Response:
            def __init__(self, status_code, content=b""):
                self.status_code = status_code
                self.content = content

            def json(self):
                return {}

        class Session:
            def get(self, url, params=None, timeout=None):
                return Response(200, feed) if url.endswith("feed") else Response(401)

        original_session = crawler.build_session
        crawler.build_session = lambda retry_server_errors=True: Session()
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "qbitai",
                    "publisher": "量子位",
                    "url": "https://qbit.example/feed",
                    "homepage": "https://qbit.example",
                    "date_policy": "entry",
                    "access_mode": "rss_plus_wordpress_api",
                    "api_url": "https://qbit.example/wp-json/wp/v2/posts",
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                max_api_pages=1,
                max_article_fetches=1,
            )
        finally:
            crawler.build_session = original_session

        self.assertEqual(len(records), 1)
        self.assertEqual(report.status, "partial")
        self.assertIn("API request failed with HTTP 401", report.errors)
        self.assertEqual(records[0].content_status, "summary_only")
        self.assertEqual(report.http_request_count, 2)
        self.assertEqual(report.feed_request_count, 1)
        self.assertEqual(report.api_request_count, 1)
        self.assertEqual(report.article_fetch_count, 1)
        self.assertIn("WordPress API did not return full article content", records[0].errors)

    def test_tokenized_http_401_is_partial_and_makes_one_request(self):
        class Response:
            status_code = 401
            content = b""

        class Session:
            calls = 0

            def get(self, url, params=None, timeout=None):
                self.calls += 1
                return Response()

        session = Session()
        original_session = crawler.build_session
        original_token = crawler.os.environ.get("MACHINE_HEART_TEST_TOKEN")
        crawler.build_session = lambda retry_server_errors=True: session
        crawler.os.environ["MACHINE_HEART_TEST_TOKEN"] = "machine-heart-secret"
        try:
            records, report = crawler.crawl_source(
                {
                    "name": "jiqizhixin_official",
                    "publisher": "机器之心",
                    "url": "https://example.test/restricted-rss",
                    "homepage": "https://example.test",
                    "date_policy": "entry",
                    "token_env": "MACHINE_HEART_TEST_TOKEN",
                },
                self.window,
                self.groups,
                10,
                datetime(2026, 8, 1, tzinfo=timezone.utc),
            )
        finally:
            crawler.build_session = original_session
            if original_token is None:
                crawler.os.environ.pop("MACHINE_HEART_TEST_TOKEN", None)
            else:
                crawler.os.environ["MACHINE_HEART_TEST_TOKEN"] = original_token

        self.assertEqual(records, [])
        self.assertEqual(session.calls, 1)
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.http_request_count, 1)
        self.assertEqual(report.errors, ["feed request failed with HTTP 401"])
        self.assertNotIn("machine-heart-secret", json.dumps(crawler.asdict(report)))


if __name__ == "__main__":
    unittest.main()
