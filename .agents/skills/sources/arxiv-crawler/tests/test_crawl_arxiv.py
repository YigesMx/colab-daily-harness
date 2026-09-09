import importlib.util
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[3] / "arxiv-announcement-state" / "scripts" / "sync_arxiv_announcements.py"
SPEC = importlib.util.spec_from_file_location("sync_arxiv_announcements", SCRIPT)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


class ArxivCrawlerContractTests(unittest.TestCase):
    def test_normalize_id_uses_unversioned_identity(self):
        self.assertEqual(module.normalize_id("oai:arXiv.org:2607.29169v2"), ("2607.29169", "v2"))

    def test_new_and_cross_share_state_identity(self):
        self.assertEqual(
            module.state_key({"source_id": "2607.29169", "announce_type": "new"}),
            module.state_key({"source_id": "2607.29169", "announce_type": "cross"}),
        )

    def test_record_declares_paper_source_metadata(self):
        args = type(
            "Args",
            (),
            {
                "since": module.parse_datetime("2026-08-01T00:00:00+00:00"),
                "until": module.parse_datetime("2026-08-02T00:00:00+00:00"),
            },
        )()
        row = {
            "source_id": "2607.29169",
            "version": "v1",
            "announce_type": "new",
            "announce_types": ["new"],
            "announcement_at": "2026-08-01T01:00:00+00:00",
            "title": "Example",
            "summary": "Summary",
            "source_categories": ["cs.RO"],
            "matched_query_groups": {},
            "abstract_url": "https://arxiv.org/abs/2607.29169",
        }
        metadata = {
            "title": "Example",
            "summary": "Summary",
            "authors": ["Author"],
            "categories": ["cs.RO"],
            "primary_category": "cs.RO",
            "pdf_url": "https://arxiv.org/pdf/2607.29169",
        }

        output = module.build_record(row, metadata, args)

        self.assertIn("category_hint: Paper", output)
        self.assertIn("content_status: substantial", output)
        self.assertIn("source_role: primary", output)


if __name__ == "__main__":
    unittest.main()
