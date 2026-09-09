import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPT = SKILL_DIR / "scripts" / "crawl_huggingface.py"
SPEC = importlib.util.spec_from_file_location("crawl_huggingface", SCRIPT)
assert SPEC and SPEC.loader
crawler = importlib.util.module_from_spec(SPEC)
sys.modules["crawl_huggingface"] = crawler
SPEC.loader.exec_module(crawler)


def sample_item(
    paper_id="2607.27205",
    title="TurboVLA: A Vision-Language-Action Model",
    summary="A robot manipulation policy using vision-language-action training.",
    listed_at="2026-07-30T00:00:00.000Z",
    upvotes=122,
):
    return {
        "paper": {
            "id": paper_id,
            "title": title,
            "summary": summary,
            "authors": [{"name": "A. Author"}],
            "submittedOnDailyAt": listed_at,
            "upvotes": upvotes,
            "projectPage": "https://example.com/project",
        },
        "publishedAt": "2026-07-28T20:00:00.000Z",
        "numComments": 2,
        "organization": {"fullname": "Example Robotics Lab"},
    }


class HuggingFaceCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.window = crawler.Window(
            datetime(2026, 7, 29, tzinfo=timezone.utc),
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        self.groups = {
            "arm_vla": ["vision-language-action", "VLA", "robot manipulation"],
            "world_model": ["world model"],
            "llm_vlm_methods": [
                "large language model",
                "LLM",
                "reinforcement learning",
                "agent",
            ],
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
        result = crawler.manifest(self.window, Path("consensus.md"), [], 0, 0, {}, args.cycle_id)
        self.assertEqual(args.cycle_id, "daily-2026-08-01")
        self.assertEqual(result["cycle_id"], "daily-2026-08-01")

    def test_extract_weekly_items_from_server_props(self):
        payload = {"dailyPapers": [sample_item()]}
        escaped = (
            json.dumps(payload)
            .replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        page = f'<div data-target="DailyPapers" data-props="{escaped}"></div>'
        items = crawler.extract_weekly_items(page)
        self.assertEqual(items[0]["paper"]["id"], "2607.27205")

    def test_window_uses_huggingface_listing_time(self):
        self.assertTrue(crawler.item_in_window(sample_item(), self.window))
        older = sample_item(listed_at="2026-07-28T00:00:00.000Z")
        self.assertFalse(crawler.item_in_window(older, self.window))

    def test_merge_preserves_daily_and_weekly_positions(self):
        records = {}
        retrieved_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
        item = sample_item()
        crawler.merge_item(records, item, "daily", "2026-07-30", 3, retrieved_at)
        crawler.merge_item(records, item, "weekly", "2026-W31", 8, retrieved_at)
        record = records["2607.27205"]
        self.assertEqual(record.daily_positions, {"2026-07-30": 3})
        self.assertEqual(record.weekly_positions, {"2026-W31": 8})

    def test_direct_and_secondary_relevance_rules(self):
        direct = crawler.paper_from_item(
            sample_item(title="TurboVLA", summary="A fast robot policy."),
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        direct.daily_positions["2026-07-30"] = 20
        groups, keywords, _ = crawler.assess_relevance(direct, self.groups)
        self.assertIn("arm_vla", groups)
        self.assertIn("VLA", keywords)

        generic = crawler.paper_from_item(
            sample_item(title="Optimization Method", summary="We train an agent."),
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        generic.daily_positions["2026-07-30"] = 30
        groups, _, _ = crawler.assess_relevance(generic, self.groups)
        self.assertNotIn("llm_vlm_methods", groups)
        generic.daily_positions["2026-07-30"] = 5
        groups, _, _ = crawler.assess_relevance(generic, self.groups)
        self.assertIn("llm_vlm_methods", groups)

    def test_record_markdown_contains_source_contract(self):
        record = crawler.paper_from_item(
            sample_item(), datetime(2026, 8, 1, tzinfo=timezone.utc)
        )
        record.daily_positions["2026-07-30"] = 1
        record.weekly_positions["2026-W31"] = 8
        record.matched_query_groups = ["arm_vla"]
        record.matched_keywords = ["VLA", "vision-language-action"]
        record.relevance_reasons = ["direct keyword in title: VLA"]
        output = crawler.record_markdown(record, self.window)
        self.assertIn("source: huggingface", output)
        self.assertIn("category_hint: Paper", output)
        self.assertIn("content_status: summary_only", output)
        self.assertIn("source_role: secondary", output)
        self.assertIn('source_id: "2607.27205"', output)
        self.assertIn("upvotes: 122", output)
        self.assertIn('weekly_periods: ["2026-W31"]', output)
        self.assertIn("A robot manipulation policy", output)


if __name__ == "__main__":
    unittest.main()
