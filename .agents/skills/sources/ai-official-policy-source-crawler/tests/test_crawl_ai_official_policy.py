import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "crawl_ai_official_policy.py"
SPEC = importlib.util.spec_from_file_location("crawl_ai_official_policy", SCRIPT)
assert SPEC and SPEC.loader
crawler = importlib.util.module_from_spec(SPEC)
sys.modules["crawl_ai_official_policy"] = crawler
SPEC.loader.exec_module(crawler)


class FakeResponse:
    def __init__(self, content=b"", status_code=200, payload=None, url=""):
        self.content = content if isinstance(content, bytes) else content.encode()
        self.status_code = status_code
        self.text = self.content.decode("utf-8", errors="replace")
        self.payload = payload
        self.url = url

    def json(self):
        if self.payload is not None:
            return self.payload
        return json.loads(self.text)


class RouteSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def _response(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        route = self.routes.get((method, url)) or self.routes.get(url)
        if callable(route):
            return route(method, url, kwargs)
        if route is None:
            return FakeResponse(b"not found", 404)
        return route

    def get(self, url, **kwargs):
        return self._response("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._response("post", url, **kwargs)


class AIOfficialPolicyCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.window = crawler.Window(
            datetime(2026, 7, 31, 16, tzinfo=timezone.utc),
            datetime(2026, 8, 1, 16, tzinfo=timezone.utc),
        )
        self.retrieved_at = datetime(2026, 8, 1, 17, tzinfo=timezone.utc)

    def source(self, name, source_type, **extra):
        base = {
            "name": name,
            "publisher": name,
            "homepage": "https://www.example.gov.cn/",
            "role": "primary",
            "type": source_type,
        }
        base.update(extra)
        return base

    def test_default_window_is_previous_shanghai_day(self):
        args = crawler.parse_args([])
        window = crawler.resolve_window(args, datetime(2026, 8, 1, 17, 23, tzinfo=timezone.utc))
        self.assertEqual(window.since, datetime(2026, 7, 31, 16, tzinfo=timezone.utc))
        self.assertEqual(window.until, datetime(2026, 8, 1, 16, tzinfo=timezone.utc))

    def test_cycle_id_is_parsed_and_written_to_manifest(self):
        args = crawler.parse_args(["--cycle-id", "daily-2026-08-01"])
        result = crawler.manifest(self.window, Path("sources.json"), {}, [], args.cycle_id)
        self.assertEqual(args.cycle_id, "daily-2026-08-01")
        self.assertEqual(result["cycle_id"], "daily-2026-08-01")

    def test_explicit_window_requires_both_bounds(self):
        args = crawler.parse_args(["--since", "2026-08-01T00:00:00Z"])
        with self.assertRaisesRegex(ValueError, "supplied together"):
            crawler.resolve_window(args)

    def test_config_uses_requested_public_endpoints_and_no_restricted_feed(self):
        sources = crawler.load_sources(SKILL_DIR / "sources.json")
        by_type = {source["type"]: source for source in sources}
        self.assertEqual(
            by_type["state_council_search"]["search_url"],
            "https://sousuo.www.gov.cn/search-gov/data",
        )
        self.assertEqual(
            by_type["miit_dynamic"]["category_url"],
            "https://www.miit.gov.cn/search-front-server/api/structure/list-category",
        )
        self.assertEqual(
            by_type["miit_dynamic"]["search_url"],
            "https://www.miit.gov.cn/search-front-server/api/search/info",
        )
        self.assertEqual(by_type["miit_dynamic"]["search_id"], 51)
        self.assertEqual(
            by_type["ai_policy_daily"]["feed_url"],
            "https://aipolicydaily.org/archive/daily/feed.xml",
        )
        raw = (SKILL_DIR / "sources.json").read_text(encoding="utf-8").lower()
        self.assertNotIn("jiqizhixin", raw)
        self.assertNotIn("machine-heart", raw)
        self.assertNotIn("token=", raw)

    def test_state_council_filters_window_and_fetches_gov_detail(self):
        source = self.source(
            "state_council_ai_policy",
            "state_council_search",
            search_url="https://sousuo.www.gov.cn/search-gov/data",
            search_code="17da70961a7",
            data_type_id="107",
            keywords=["人工智能"],
            max_details=5,
        )
        search = {
            "searchVO": {
                "catMap": {
                    "gongwen": {
                        "listVO": [
                        {
                            "title": "人工智能治理政策",
                            "url": "https://www.gov.cn/zhengce/content_1.htm",
                            "summary": "人工智能政策和监管要求",
                            "pubtimeStr": "2026.08.01",
                            "pubtime": int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000),
                            "ptime": int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000),
                        },
                        {
                            "title": "旧人工智能政策",
                            "url": "https://www.gov.cn/zhengce/content_old.htm",
                            "summary": "人工智能政策和监管要求",
                            "pubtimeStr": "2026.07.30",
                        },
                        ]
                    }
                }
            }
        }
        detail = """
        <html><head><title>人工智能治理政策</title>
        <meta name='article:published_time' content='2026-08-01T08:00:00+08:00'>
        <meta name='description' content='政府发布人工智能治理政策和监管要求'></head>
        <body><div id='pages_content'>政府发布人工智能治理政策和监管要求，明确人工智能标准、监管和安全责任。</div></body></html>
        """.encode()
        session = RouteSession(
            {
                ("get", source["search_url"]): FakeResponse(payload=search),
                "https://www.gov.cn/zhengce/content_1.htm": FakeResponse(detail),
                "https://www.gov.cn/zhengce/content_old.htm": FakeResponse(detail.replace(b"2026-08-01", b"2026-07-30")),
            }
        )
        records, report = crawler.crawl_state_council(source, self.window, self.retrieved_at, session)
        self.assertEqual(len(records), 1)
        self.assertEqual(report.window_item_count, 1)
        self.assertEqual(report.status, "success")
        self.assertEqual(session.calls[0][0], "get")
        self.assertEqual(session.calls[0][1], "https://sousuo.www.gov.cn/search-gov/data")
        self.assertEqual(session.calls[0][2]["params"]["q"], "人工智能")
        self.assertEqual(session.calls[0][2]["params"]["timetype"], "2")
        self.assertEqual(
            set(session.calls[0][2]["params"]),
            {"t", "q", "searchfield", "timetype", "mintime", "maxtime", "sort", "sortType", "p", "n"},
        )
        self.assertTrue(all("www.gov.cn" in call[1] for call in session.calls))
        self.assertEqual(records[0].source_role, "primary")
        self.assertEqual(records[0].source_category_hint, "Policy")
        self.assertEqual(records[0].content_scope, "summary_only")

    def test_miit_dynamic_success_with_stale_rss_is_observable(self):
        source = self.source(
            "miit_ai_policy",
            "miit_dynamic",
            homepage="https://www.miit.gov.cn/",
            category_url="https://www.miit.gov.cn/search-front-server/api/structure/list-category",
            search_url="https://www.miit.gov.cn/search-front-server/api/search/info",
            category_page="https://www.miit.gov.cn/RRSdy/index.html",
            website_id="110000000000000",
            search_id=51,
            rss_candidates=["https://www.miit.gov.cn/rss.xml"],
            keywords=["人工智能"],
            max_details=2,
        )
        stale_feed = b"""<?xml version='1.0'?><rss><channel>
        <lastBuildDate>Thu, 30 Jul 2026 00:00:00 GMT</lastBuildDate>
        </channel></rss>"""
        session = RouteSession(
            {
                ("get", source["category_url"]): FakeResponse(
                    payload={"success": True, "data": {"categories": [{"iid": 57, "name": "全 部"}]}}
                ),
                ("get", source["search_url"]): FakeResponse(
                    payload={"success": True, "data": {"searchResult": {"dataResults": []}}}
                ),
                "https://www.miit.gov.cn/rss.xml": FakeResponse(stale_feed),
            }
        )
        records, report = crawler.crawl_miit(source, self.window, self.retrieved_at, session)
        self.assertEqual(records, [])
        self.assertEqual(report.status, "success_stale")
        self.assertEqual(report.discovery_status, "success_stale")
        self.assertEqual(report.request_count, 3)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(session.calls[0][2]["params"], {"websiteid": "110000000000000", "searchid": 51})
        self.assertEqual(session.calls[1][2]["params"]["cateid"], 57)

    def test_miit_dynamic_success_with_rss_failure_is_partial(self):
        source = self.source(
            "miit_ai_policy",
            "miit_dynamic",
            homepage="https://www.miit.gov.cn/",
            category_url="https://www.miit.gov.cn/search-front-server/api/structure/list-category",
            search_url="https://www.miit.gov.cn/search-front-server/api/search/info",
            category_page="https://www.miit.gov.cn/xwfb/gxdt/index.html",
            website_id="110000000000000",
            search_id=51,
            rss_candidates=["https://www.miit.gov.cn/rss.xml"],
            keywords=["人工智能"],
            max_details=1,
        )
        session = RouteSession(
            {
                ("get", source["category_url"]): FakeResponse(
                    payload={"success": True, "data": {"categories": [{"iid": 58, "name": "文件发布"}]}}
                ),
                ("get", source["search_url"]): FakeResponse(
                    payload={"success": True, "data": {"searchResult": {"dataResults": []}}}
                ),
                "https://www.miit.gov.cn/rss.xml": FakeResponse(b"blocked", 500),
            }
        )
        records, report = crawler.crawl_miit(source, self.window, self.retrieved_at, session)
        self.assertEqual(records, [])
        self.assertEqual(report.status, "partial")
        self.assertEqual(report.discovery_status, "failed")

    def test_miit_dynamic_result_uses_returned_category_and_detail(self):
        source = self.source(
            "miit_ai_policy",
            "miit_dynamic",
            homepage="https://www.miit.gov.cn/",
            category_url="https://www.miit.gov.cn/search-front-server/api/structure/list-category",
            search_url="https://www.miit.gov.cn/search-front-server/api/search/info",
            category_page="https://www.miit.gov.cn/RRSdy/index.html",
            website_id="110000000000000",
            search_id=51,
            rss_candidates=[],
            keywords=["人工智能"],
            max_details=1,
        )
        search = {
            "success": True,
            "data": {
                "searchResult": {
                    "dataResults": [
                        {
                            "groupData": [
                                {
                                    "data": {
                                        "title_text": "人工智能监管政策",
                                        "url": "/zwgk/zcjd/art/2026/art_policy.html",
                                        "contentdescribe": "人工智能监管政策解读",
                                        "deploytime": str(int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp() * 1000)),
                                    }
                                }
                            ]
                        }
                    ]
                }
            },
        }
        detail = """
        <html><head><title>人工智能监管政策</title></head>
        <body><div class='UCAP-CONTENT'>政府发布人工智能监管政策，明确人工智能安全、治理和标准要求。<p>政策内容。</p></div></body></html>
        """
        session = RouteSession(
            {
                ("get", source["category_url"]): FakeResponse(
                    payload={"success": True, "data": {"categories": [{"iid": 61, "name": "政策解读"}]}}
                ),
                ("get", source["search_url"]): FakeResponse(payload=search),
                "https://www.miit.gov.cn/zwgk/zcjd/art/2026/art_policy.html": FakeResponse(detail),
            }
        )
        records, report = crawler.crawl_miit(source, self.window, self.retrieved_at, session)
        self.assertEqual(report.status, "success")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].content, "政府发布人工智能监管政策，明确人工智能安全、治理和标准要求。 政策内容。")
        self.assertEqual(session.calls[1][2]["params"]["cateid"], 61)

    def test_miit_rss_404_is_explicitly_stale(self):
        source = self.source(
            "miit_ai_policy",
            "miit_dynamic",
            category_url="https://www.miit.gov.cn/search-front-server/api/structure/list-category",
            search_url="https://www.miit.gov.cn/search-front-server/api/search/info",
            category_page="https://www.miit.gov.cn/RRSdy/index.html",
            website_id="110000000000000",
            search_id=51,
            rss_candidates=[{"name": "RRSdy / old", "url": "https://www.miit.gov.cn/old-feed.xml"}],
            keywords=["人工智能"],
        )
        session = RouteSession(
            {
                ("get", source["category_url"]): FakeResponse(
                    payload={"success": True, "data": {"categories": [{"iid": 57}]}}
                ),
                ("get", source["search_url"]): FakeResponse(
                    payload={"success": True, "data": {"searchResult": {"dataResults": []}}}
                ),
                "https://www.miit.gov.cn/old-feed.xml": FakeResponse(b"missing", 404),
            }
        )
        records, report = crawler.crawl_miit(source, self.window, self.retrieved_at, session)
        self.assertEqual(records, [])
        self.assertEqual(report.status, "success_stale")
        self.assertEqual(report.discovery_status, "stale_404")
        self.assertIn("RRSdy / old", report.errors[0])

    def test_official_detail_prefers_pages_content(self):
        metadata = crawler.metadata_from_html(
            """
            <html><head><title>页面标题</title></head>
            <body><div>导航和无关内容</div><div id='pages_content'>正文人工智能政策和监管要求。</div></body></html>
            """,
            "https://www.gov.cn/policy.html",
        )
        self.assertEqual(metadata["content"], "正文人工智能政策和监管要求。")

    def test_authoritative_long_detail_is_full(self):
        source = self.source("state_council_ai_policy", "state_council_search")
        record = crawler.record_from_candidate(
            source,
            {"url": "https://www.gov.cn/zhengce/ai.htm", "title": "AI policy"},
            {
                "title": "AI policy",
                "published": "2026-08-01T08:00:00+08:00",
                "content": "The government published an artificial intelligence policy and safety regulation. " * 30,
            },
            self.window,
            self.retrieved_at,
            discovery_url="https://sousuo.www.gov.cn/search-gov/data",
            date_basis="detail_published",
            authoritative=True,
        )
        self.assertIsNotNone(record)
        assert record
        self.assertEqual(record.content_scope, "full")
        self.assertFalse(record.url_fragment_identity)
        self.assertIn("url_fragment_identity: false", crawler.record_markdown(record, self.window))

    def test_failed_discovery_is_reported_as_failed(self):
        source = self.source(
            "state_council_ai_policy",
            "state_council_search",
            search_url="https://sousuo.www.gov.cn/search-gov/data",
            keywords=["人工智能"],
        )
        session = RouteSession({source["search_url"]: FakeResponse(b"server error", 500)})
        records, report = crawler.crawl_state_council(source, self.window, self.retrieved_at, session)
        self.assertEqual(records, [])
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.request_count, 1)

    def test_aipd_splits_policy_stories_and_marks_missing_index_not_yet_published(self):
        source = self.source(
            "ai_policy_daily",
            "ai_policy_daily",
            role="secondary",
            feed_url="https://aipolicydaily.org/archive/daily/feed.xml",
            archive_url="https://aipolicydaily.org/archive/daily/{date}/",
            markdown_url="https://aipolicydaily.org/archive/daily/{date}/index.md",
            text_url="https://aipolicydaily.org/archive/daily/{date}/index.txt",
            max_feed_items=10,
            max_details=5,
        )
        feed = b"""<?xml version='1.0'?><rss><channel><item>
        <title>Policy update</title><link>https://aipolicydaily.org/archive/daily/2026-08-01/</link>
        <pubDate>Sat, 01 Aug 2026 12:00:00 GMT</pubDate></item></channel></rss>"""
        session = RouteSession(
            {
                source["feed_url"]: FakeResponse(feed),
                "https://aipolicydaily.org/archive/daily/2026-08-01/index.md": FakeResponse(b"missing", 404),
                "https://aipolicydaily.org/archive/daily/2026-08-01/index.txt": FakeResponse(b"missing", 404),
            }
        )
        records, report = crawler.crawl_ai_policy_daily(source, self.window, self.retrieved_at, session)
        self.assertEqual(records, [])
        self.assertEqual(report.status, "not_yet_published")
        self.assertIn("issue not published", report.errors[0])

        raw = """---\ndate: 2026-08-01\n---\n# Policy Tracker\n- **AI bill advances** - The government introduced a bill for AI safety and regulation.\n# Capability & Research Watch\n- **A model benchmark** - A new research result.\n"""
        stories = crawler.split_daily_stories(raw)
        self.assertEqual(stories[0][0], "Policy Tracker")
        self.assertEqual(stories[0][1], "AI bill advances")
        self.assertEqual(len(stories), 2)

        text_stories = crawler.split_daily_stories(
            "I.Top Stories\n\nAI model policy\nThe government proposed an AI safety policy.\nRead at Official.\n\n"
            "III.Policy Tracker\n\nAI bill advances\nThe legislature advanced an AI regulation bill.\n"
        )
        self.assertEqual(text_stories[0][0], "Top Stories")
        self.assertEqual(text_stories[0][1], "AI model policy")
        self.assertEqual(text_stories[1][0], "Policy Tracker")

    def test_aipd_policy_record_has_secondary_role_and_substantial_content(self):
        source = self.source(
            "ai_policy_daily",
            "ai_policy_daily",
            role="secondary",
            feed_url="https://aipolicydaily.org/archive/daily/feed.xml",
            homepage="https://aipolicydaily.org/",
        )
        content = "The government introduced a new AI regulation and safety bill. " * 30
        record = crawler.daily_record(
            source,
            "https://aipolicydaily.org/archive/daily/2026-08-01/",
            datetime(2026, 8, 1, tzinfo=timezone.utc).date(),
            1,
            "Policy Tracker",
            "AI regulation bill advances",
            content,
            datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            self.retrieved_at,
        )
        self.assertIsNotNone(record)
        assert record
        self.assertEqual(record.content_scope, "substantial")
        self.assertEqual(record.source_role, "secondary")
        self.assertEqual(record.source_category_hint, "Policy")
        self.assertTrue(record.url_fragment_identity)
        self.assertIn("#story-0001", record.url)

    def test_aipd_full_crawl_preserves_colliding_titles_and_retry_identity(self):
        source = self.source(
            "ai_policy_daily",
            "ai_policy_daily",
            role="secondary",
            feed_url="https://aipolicydaily.org/archive/daily/feed.xml",
            archive_url="https://aipolicydaily.org/archive/daily/{date}/",
            markdown_url="https://aipolicydaily.org/archive/daily/{date}/index.md",
            text_url="https://aipolicydaily.org/archive/daily/{date}/index.txt",
            max_feed_items=10,
            max_details=5,
        )
        issue_url = "https://aipolicydaily.org/archive/daily/2026-08-01/"
        markdown_url = f"{issue_url}index.md"
        feed = f"""<?xml version='1.0'?><rss><channel><item>
        <title>Policy update</title><link>{issue_url}</link>
        <pubDate>Sat, 01 Aug 2026 12:00:00 GMT</pubDate></item></channel></rss>""".encode()
        shared_prefix = "a" * 80
        issue = (
            "---\ndate: 2026-08-01\n---\n# Policy Tracker\n"
            "- **Repeated AI policy title** - The government introduced an AI safety bill.\n"
            "- **Repeated AI policy title** - The agency published a second AI regulation.\n"
            f"- **{shared_prefix} first** - The government adopted an AI governance policy.\n"
            f"- **{shared_prefix} second** - Congress proposed another AI safety law.\n"
        ).encode()

        def session():
            return RouteSession(
                {
                    source["feed_url"]: FakeResponse(feed),
                    markdown_url: FakeResponse(issue),
                }
            )

        with patch.object(crawler, "build_session", side_effect=[session(), session()]):
            first_records, first_reports = crawler.crawl([source], self.window)
            retry_records, retry_reports = crawler.crawl([source], self.window)

        self.assertEqual(set(first_records), set(retry_records))
        self.assertEqual(
            {record.url for record in first_records.values()},
            {record.url for record in retry_records.values()},
        )
        self.assertEqual(len(first_records), 4)
        self.assertEqual(first_reports[0].window_item_count, 4)
        self.assertEqual(first_reports[0].record_count, 4)
        self.assertEqual(retry_reports[0].window_item_count, 4)
        self.assertEqual(retry_reports[0].record_count, 4)
        self.assertEqual(len({record.source_id for record in first_records.values()}), 4)
        repeated = [
            record
            for record in first_records.values()
            if record.title == "Repeated AI policy title"
        ]
        shared_slug_prefix = [
            record
            for record in first_records.values()
            if record.title.startswith(shared_prefix)
        ]
        self.assertEqual(len({record.source_id for record in repeated}), 2)
        self.assertEqual(len({record.url for record in repeated}), 2)
        self.assertEqual(len({record.source_id for record in shared_slug_prefix}), 2)
        self.assertEqual(len({record.url for record in shared_slug_prefix}), 2)
        self.assertEqual(
            {record.url.rsplit("#", 1)[1] for record in first_records.values()},
            {"story-0001", "story-0002", "story-0003", "story-0004"},
        )

        with tempfile.TemporaryDirectory() as temporary:
            output_dir = Path(temporary)
            crawler.write_outputs(
                output_dir,
                self.window,
                Path("sources.json"),
                first_records,
                first_reports,
                False,
            )
            source_dir = output_dir / "ai_official_policy_source"
            written_manifest = json.loads(
                (source_dir / "crawl_manifest.json").read_text(encoding="utf-8")
            )
            written_records = list(source_dir.glob("*/record.md"))

        self.assertEqual(written_manifest["record_count"], 4)
        self.assertEqual(len(written_manifest["record_directories"]), 4)
        self.assertEqual(len(set(written_manifest["record_directories"])), 4)
        self.assertEqual(written_manifest["sources"][0]["window_item_count"], 4)
        self.assertEqual(written_manifest["sources"][0]["record_count"], 4)
        self.assertEqual(len(written_records), 4)

    def test_new_official_sources_are_configured(self):
        sources = crawler.load_sources(SKILL_DIR / "sources.json")
        by_type = {source["type"]: source for source in sources}
        self.assertEqual(
            by_type["federal_register_api"]["api_url"],
            "https://www.federalregister.gov/api/v1/documents.json",
        )
        self.assertEqual(
            by_type["ec_digital_strategy_rss"]["feed_url"],
            "https://digital-strategy.ec.europa.eu/en/rss.xml",
        )
        self.assertTrue(
            by_type["govuk_policy_atom"]["feed_url"].startswith(
                "https://www.gov.uk/search/policy-papers-and-consultations.atom?"
            )
        )
        self.assertEqual(
            by_type["samr_national_standard_api"]["api_url"],
            "https://std.samr.gov.cn/noc/search/nocGBPage",
        )

    def test_federal_register_filters_incidental_ai_mentions(self):
        source = self.source(
            "us_federal_register_ai",
            "federal_register_api",
            api_url="https://www.federalregister.gov/api/v1/documents.json",
            homepage="https://www.federalregister.gov/",
            term="artificial intelligence",
            max_records=5,
        )
        payload = {
            "results": [
                {
                    "title": "Request for Information on Artificial Intelligence Security",
                    "abstract": "The agency requests information on artificial intelligence security standards.",
                    "html_url": "https://www.federalregister.gov/documents/2026/08/01/ai-security",
                    "publication_date": "2026-08-01",
                    "type": "Notice",
                    "agencies": [{"name": "NIST"}],
                },
                {
                    "title": "Energy Advisory Board",
                    "abstract": "A routine advisory body notice.",
                    "html_url": "https://www.federalregister.gov/documents/2026/08/01/energy",
                    "publication_date": "2026-08-01",
                    "type": "Notice",
                    "agencies": [{"name": "Energy Department"}],
                },
            ]
        }
        session = RouteSession(
            {"https://www.federalregister.gov/api/v1/documents.json": FakeResponse(payload=payload)}
        )
        records, report = crawler.crawl_federal_register(source, self.window, self.retrieved_at, session)
        self.assertEqual(report.status, "success")
        self.assertEqual([record.title for record in records], ["Request for Information on Artificial Intelligence Security"])
        self.assertEqual(session.calls[0][2]["params"]["conditions[publication_date][gte]"], "2026-08-01")
        self.assertEqual(session.calls[0][2]["params"]["conditions[publication_date][lte]"], "2026-08-01")

    def test_federal_register_empty_count_without_results_is_success_empty(self):
        source = self.source(
            "us_federal_register_ai",
            "federal_register_api",
            api_url="https://www.federalregister.gov/api/v1/documents.json",
            homepage="https://www.federalregister.gov/",
        )
        session = RouteSession(
            {
                "https://www.federalregister.gov/api/v1/documents.json": FakeResponse(
                    payload={"description": "No documents", "count": 0}
                )
            }
        )
        records, report = crawler.crawl_federal_register(source, self.window, self.retrieved_at, session)
        self.assertEqual((records, report.status, report.record_count), ([], "success_empty", 0))

    def test_official_feed_filters_ai_and_marks_stale(self):
        source = self.source(
            "eu_digital_strategy_ai",
            "ec_digital_strategy_rss",
            feed_url="https://digital-strategy.ec.europa.eu/en/rss.xml",
            homepage="https://digital-strategy.ec.europa.eu/",
            max_feed_items=5,
        )
        feed = """<?xml version="1.0"?>
        <rss><channel><title>EU</title>
        <item><title>AI Act transparency guidance</title><link>https://digital-strategy.ec.europa.eu/en/news/ai-act</link><pubDate>Sat, 1 Aug 2026 08:00:00 +0000</pubDate><description>The Commission published AI Act guidance.</description></item>
        <item><title>Online marketplace study</title><link>https://digital-strategy.ec.europa.eu/en/tenders/marketplace</link><pubDate>Sat, 1 Aug 2026 09:00:00 +0000</pubDate><description>A digital services study.</description></item>
        </channel></rss>"""
        session = RouteSession(
            {"https://digital-strategy.ec.europa.eu/en/rss.xml": FakeResponse(feed.encode())}
        )
        records, report = crawler.crawl_official_feed(source, self.window, self.retrieved_at, session)
        self.assertEqual(report.status, "success")
        self.assertEqual([record.title for record in records], ["AI Act transparency guidance"])
        self.assertEqual(records[0].date_basis, "feed_published")

        stale_feed = feed.replace("1 Aug 2026", "20 Jul 2026")
        session = RouteSession(
            {"https://digital-strategy.ec.europa.eu/en/rss.xml": FakeResponse(stale_feed.encode())}
        )
        records, report = crawler.crawl_official_feed(source, self.window, self.retrieved_at, session)
        self.assertEqual((records, report.status), ([], "success_stale"))

    def test_samr_standard_notice_uses_official_listing_identity(self):
        source = self.source(
            "china_national_standard_ai",
            "samr_national_standard_api",
            api_url="https://std.samr.gov.cn/noc/search/nocGBPage",
            listing_url="https://std.samr.gov.cn/noc/nocGB",
            search_text="人工智能",
        )
        payload = {
            "total": 1,
            "rows": [
                {
                    "CODE": "2026年第32号",
                    "NOTICE_DATE": "2026-08-01",
                    "PID": "PID",
                    "TITLE": "关于发布人工智能标准指导性技术文件的公告",
                    "id": "ROWID",
                }
            ],
        }
        session = RouteSession(
            {"https://std.samr.gov.cn/noc/search/nocGBPage": FakeResponse(payload=payload)}
        )
        records, report = crawler.crawl_samr_standards(source, self.window, self.retrieved_at, session)
        self.assertEqual(report.status, "success")
        self.assertEqual(len(records), 1)
        self.assertIn("noticeCode=2026年第32号", records[0].url)
        self.assertEqual(records[0].date_basis, "official_notice_date")

    def test_blocked_response_is_not_success_empty(self):
        report = crawler.SourceReport("test", "Test", "test")
        session = RouteSession({"https://example.com": FakeResponse(b"captcha", 403)})
        with self.assertRaises(crawler.HTTPSourceError) as context:
            crawler.request(session, report, "get", "https://example.com")
        self.assertEqual(context.exception.status, "blocked")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.request_count, 1)

    def test_record_markdown_and_manifest_contain_contract_metadata(self):
        source = self.source(
            "ai_policy_daily",
            "ai_policy_daily",
            role="secondary",
            feed_url="https://aipolicydaily.org/archive/daily/feed.xml",
        )
        record = crawler.daily_record(
            source,
            "https://aipolicydaily.org/archive/daily/2026-08-01/",
            date.fromisoformat("2026-08-01"),
            1,
            "Policy Tracker",
            "AI governance bill",
            "The government introduced an AI governance bill for safety regulation.",
            datetime(2026, 8, 1, 12, tzinfo=timezone.utc),
            self.retrieved_at,
        )
        assert record
        output = crawler.record_markdown(record, self.window)
        self.assertIn("source_category_hint: \"Policy\"", output)
        self.assertIn("source_role: \"secondary\"", output)
        self.assertIn("content_scope:", output)
        self.assertIn("url_fragment_identity: true", output)
        report = crawler.SourceReport("ai_policy_daily", "AI Policy Daily", "ai_policy_daily", "success", 1)
        manifest = crawler.manifest(self.window, Path("sources.json"), {record.source_id: record}, [report])
        self.assertEqual(manifest["request_count"], 1)
        self.assertEqual(manifest["record_count"], 1)


if __name__ == "__main__":
    unittest.main()
