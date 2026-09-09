import importlib.util
import json
import os
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import requests


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "crawl_ai_companies.py"
SPEC = importlib.util.spec_from_file_location("crawl_ai_companies", SCRIPT)
assert SPEC and SPEC.loader
crawler = importlib.util.module_from_spec(SPEC)
sys.modules["crawl_ai_companies"] = crawler
SPEC.loader.exec_module(crawler)


class CompanyCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.window = crawler.Window(
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

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
        result = crawler.manifest(self.window, Path("sources.json"), {}, [], args.cycle_id)
        self.assertEqual(args.cycle_id, "daily-2026-08-01")
        self.assertEqual(result["cycle_id"], "daily-2026-08-01")

    def test_page_metadata_extracts_schema_article(self):
        page = """
        <html><head>
          <meta property="og:title" content="Meta AI Release">
          <meta property="og:description" content="A new model">
          <meta property="og:image" content="https://example.com/image.jpg">
          <script type="application/ld+json">
          {"@type":"NewsArticle","datePublished":"2026-07-30T12:00:00Z",
           "dateModified":"2026-07-30T14:00:00Z","articleBody":"Full article body",
           "keywords":["AI","model"],
           "author":[{"@type":"Person","name":"Author One"},{"name":"Author Two"}]}
          </script>
        </head><body><main>Visible body</main></body></html>
        """
        data = crawler.page_metadata(page, "https://example.com/article")
        self.assertEqual(data["published"], "2026-07-30T12:00:00Z")
        self.assertEqual(data["content"], "Full article body")
        self.assertEqual(data["categories"], ["AI", "model"])
        self.assertEqual(data["authors"], ["Author One", "Author Two"])

    def test_anthropic_embedded_published_on_is_supported(self):
        page = r'''<html><head><meta property="og:title" content="Research"></head>
        <body><main>Article text</main><script>\"publishedOn\":\"2026-07-30T15:00:00.000Z\"</script></body></html>'''
        data = crawler.page_metadata(page, "https://www.anthropic.com/research/test")
        self.assertEqual(data["published"], "2026-07-30T15:00:00.000Z")

    @unittest.skipUnless(hasattr(time, "tzset"), "requires time.tzset")
    def test_feed_struct_time_is_always_interpreted_as_utc(self):
        previous_timezone = os.environ.get("TZ")
        try:
            os.environ["TZ"] = "America/Los_Angeles"
            time.tzset()
            parsed = crawler.struct_time_to_datetime(
                time.strptime("Wed, 30 Jul 2026 15:00:00 GMT", "%a, %d %b %Y %H:%M:%S %Z")
            )
        finally:
            if previous_timezone is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous_timezone
            time.tzset()

        self.assertEqual(parsed, datetime(2026, 7, 30, 15, tzinfo=timezone.utc))

    def test_sitemap_lastmod_only_selects_candidates(self):
        xml = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url><loc>https://example.com/news/new</loc><lastmod>2026-07-30T00:00:00Z</lastmod></url>
          <url><loc>https://example.com/news/old</loc><lastmod>2026-07-20T00:00:00Z</lastmod></url>
          <url><loc>https://example.com/other/new</loc><lastmod>2026-07-30T00:00:00Z</lastmod></url>
        </urlset>"""
        source = {"include_url_prefixes": ["https://example.com/news/"]}
        rows = crawler.sitemap_candidates(xml, source, self.window, 10)
        self.assertEqual(rows[0][0], "https://example.com/news/new")
        self.assertEqual(len(rows), 1)

    def test_meta_topic_filter_is_configurable(self):
        source = {"topic_keywords": ["AI", "Llama"]}
        relevant = {"title": "New Llama model", "description": "", "content": "", "categories": []}
        unrelated = {"title": "Community event", "description": "", "content": "", "categories": []}
        self.assertTrue(crawler.topic_matches(relevant, source))
        self.assertFalse(crawler.topic_matches(unrelated, source))

    def test_record_directory_is_stable_and_source_scoped(self):
        source_id, directory = crawler.stable_identity(
            "openai_news", "https://openai.com/index/example"
        )
        self.assertTrue(source_id.startswith("openai_news:"))
        self.assertTrue(directory.startswith("openai_news--"))
        self.assertEqual(len(directory.rsplit("--", 1)[1]), 16)

    def test_record_markdown_contains_original_content(self):
        source_id, _ = crawler.stable_identity("anthropic_official", "https://example.com/a")
        record = crawler.ArticleRecord(
            source_name="anthropic_official",
            publisher="Anthropic",
            source_id=source_id,
            title="Official Research",
            authors=[],
            published_at="2026-07-30T15:00:00+00:00",
            updated_at=None,
            retrieved_at="2026-08-01T00:00:00+00:00",
            url="https://example.com/a",
            feed_url="https://example.com/sitemap.xml",
            homepage_url="https://example.com/news",
            categories=["Research"],
            image_urls=[],
            summary="Original description",
            content="Original full article text",
            content_type="sitemap_article",
        )
        output = crawler.record_markdown(record, self.window)
        self.assertIn("source: ai_company_official", output)
        self.assertIn('category_hint: "News"', output)
        self.assertIn('content_status: "full"', output)
        self.assertIn('source_role: "primary"', output)
        self.assertIn("Original description", output)
        self.assertIn("Original full article text", output)

    def test_manifest_preserves_content_status_metadata(self):
        source_id, _ = crawler.stable_identity("example_news", "https://example.com/a")
        record = crawler.ArticleRecord(
            source_name="example_news",
            publisher="Example",
            source_id=source_id,
            title="Official Research",
            authors=[],
            published_at="2026-07-30T15:00:00+00:00",
            updated_at=None,
            retrieved_at="2026-08-01T00:00:00+00:00",
            url="https://example.com/a",
            feed_url="https://example.com/feed.xml",
            homepage_url="https://example.com/news",
            categories=[],
            image_urls=[],
            summary="Summary",
            content="Full article text",
            content_type="feed_entry",
        )

        manifest = crawler.manifest(
            self.window,
            Path("sources.json"),
            {record.source_id: record},
            [],
        )

        self.assertEqual(manifest["category_hint"], "News")
        self.assertEqual(manifest["content_status"], "full")
        self.assertEqual(manifest["source_role"], "primary")

    def test_failed_feed_enrichment_marks_record_and_source_partial(self):
        class FailingSession:
            def get(self, url, timeout):
                raise requests.RequestException("blocked")

        entry = {
            "title": "Official update",
            "link": "https://example.com/update",
            "published": "Thu, 30 Jul 2026 15:00:00 GMT",
            "summary": "Short official summary",
        }
        source = {
            "name": "example_news",
            "publisher": "Example",
            "type": "rss",
            "url": "https://example.com/feed.xml",
            "homepage": "https://example.com/news",
        }
        record = crawler.record_from_feed_entry(
            source,
            entry,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            FailingSession(),
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.status, "partial")
        self.assertIn("article enrichment failed: blocked", record.errors)

        reports = [
            crawler.SourceReport("complete", "Complete", "rss", "https://a", "success"),
            crawler.SourceReport("degraded", "Degraded", "rss", "https://b", "partial"),
        ]
        result = crawler.manifest(self.window, Path("sources.json"), {}, reports)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["partial_source_count"], 1)

    def test_disabled_page_enrichment_avoids_request_and_marks_summary_partial(self):
        class NoRequestSession:
            def get(self, url, timeout):
                self.fail("page request must not run")

        source = {
            "name": "summary_only",
            "publisher": "Summary Only",
            "type": "rss",
            "url": "https://example.com/feed.xml",
            "homepage": "https://example.com/news",
            "page_enrichment": "never",
        }
        entry = {
            "title": "Official update",
            "link": "https://example.com/update",
            "published": "Thu, 30 Jul 2026 15:00:00 GMT",
            "summary": "Short official summary",
        }
        record = crawler.record_from_feed_entry(
            source,
            entry,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
            NoRequestSession(),
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.status, "partial")
        self.assertEqual(record.content, "Short official summary")
        self.assertIn("article enrichment disabled", record.errors[0])

    def test_page_date_overrides_month_granularity_feed_date_before_filtering(self):
        feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
        <item><title>Recent official article</title>
        <link>https://deepmind.google/blog/recent/</link>
        <pubDate>Wed, 01 Jul 2026 00:00:00 GMT</pubDate></item>
        </channel></rss>"""
        page = """<html><head>
        <meta property="article:published_time" content="2026-07-30T16:00:00Z">
        <meta property="article:modified_time" content="2026-07-31T14:00:00Z">
        <meta property="og:title" content="Recent official article">
        <meta property="og:description" content="Official description">
        <meta name="author" content="DeepMind Author">
        <link rel="canonical" href="https://deepmind.google/blog/recent/">
        </head><body><main>Official article body</main></body></html>"""

        class Response:
            def __init__(self, content, url):
                self.content = content
                self.text = content.decode() if isinstance(content, bytes) else content
                self.url = url

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, timeout):
                if url.endswith("feed.xml"):
                    return Response(feed, url)
                return Response(page, url)

        source = {
            "name": "google_deepmind_blog",
            "publisher": "Google DeepMind",
            "type": "rss",
            "url": "https://example.com/feed.xml",
            "homepage": "https://deepmind.google/discover/blog/",
            "date_source": "page",
            "feed_date_granularity": "month",
            "allowed_article_hosts": ["deepmind.google", "blog.google"],
        }
        records, report = crawler.crawl_rss(
            source,
            Session(),
            self.window,
            10,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].published_at, "2026-07-30T16:00:00+00:00")
        self.assertEqual(records[0].updated_at, "2026-07-31T14:00:00+00:00")
        self.assertEqual(records[0].authors, ["DeepMind Author"])
        self.assertEqual(report.window_item_count, 1)
        self.assertEqual(report.record_count, 1)
        self.assertEqual(report.page_fetch_count, 1)

    def test_month_granularity_skips_pages_outside_window_months(self):
        source = {"feed_date_granularity": "month"}
        self.assertTrue(
            crawler.entry_may_overlap_window(
                datetime(2026, 7, 1, tzinfo=timezone.utc), source, self.window
            )
        )
        self.assertFalse(
            crawler.entry_may_overlap_window(
                datetime(2026, 6, 1, tzinfo=timezone.utc), source, self.window
            )
        )

    def test_allowed_article_hosts_are_source_configuration(self):
        source = {"allowed_article_hosts": ["deepmind.google", "blog.google"]}
        self.assertTrue(
            crawler.article_host_allowed(source, "https://deepmind.google/blog/example/")
        )
        self.assertFalse(
            crawler.article_host_allowed(source, "https://cloud.google.com/blog/example")
        )

    def test_page_metadata_falls_back_to_body_without_semantic_wrappers(self):
        page = """<html><head><title>New model</title></head>
        <body><div>New model article body without semantic wrappers.</div></body></html>"""
        data = crawler.page_metadata(page, "https://example.com/blog/new-model")
        self.assertEqual(data["content"], "New model article body without semantic wrappers.")

    def test_listing_candidates_extract_same_host_links_and_visible_dates(self):
        listing = """<html><body>
        <nav><a href="./blog">Blog</a><a href="./blog/research">Research</a></nav>
        <a href="./blog/introducing-isaac-0-5">Introducing Isaac 0.5</a>
        <span>Aug 26th, 2026</span>
        <a href="https://external.example.com/blog/other">External</a><span>Aug 27th, 2026</span>
        <a href="./blog/egocentric-api">Egocentric API</a>
        <span>July 9, 2026</span>
        <a href="./about">About</a><span>August 19, 2026</span>
        </body></html>"""
        source = {"url": "https://www.example.com/blog", "article_url_prefixes": ["/blog/"]}
        rows = crawler.listing_candidates(listing, source, 10)
        self.assertEqual(
            rows,
            [
                ("https://www.example.com/blog/introducing-isaac-0-5", datetime(2026, 8, 26, tzinfo=timezone.utc)),
                ("https://www.example.com/blog/egocentric-api", datetime(2026, 7, 9, tzinfo=timezone.utc)),
            ],
        )

    def test_crawl_blog_listing_uses_listing_date_and_page_content(self):
        listing = """<html><body>
        <a href="./blog/new-model">New model</a><span>July 30, 2026</span>
        </body></html>"""
        article = """<html><head>
        <meta property="og:title" content="New model">
        <meta property="og:description" content="Official description">
        <link rel="canonical" href="https://www.example.com/">
        </head><body><div>Official full article body.</div></body></html>"""

        class Response:
            def __init__(self, text, url):
                self.text = text
                self.url = url

            def raise_for_status(self):
                return None

        class Session:
            def get(self, url, timeout):
                return Response(listing if url.endswith("/blog") else article, url)

        source = {
            "name": "example_blog",
            "publisher": "Example",
            "type": "blog_listing",
            "url": "https://www.example.com/blog",
            "homepage": "https://www.example.com/blog",
        }
        records, report = crawler.crawl_blog_listing(
            source,
            Session(),
            self.window,
            10,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(report.status, "success")
        self.assertEqual(report.fetched_item_count, 1)
        self.assertEqual(report.window_item_count, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].published_at, "2026-07-30T00:00:00+00:00")
        self.assertEqual(records[0].url, "https://www.example.com/blog/new-model")
        self.assertEqual(records[0].content, "Official full article body.")
        self.assertEqual(records[0].content_type, "blog_listing_article")


if __name__ == "__main__":
    unittest.main()
