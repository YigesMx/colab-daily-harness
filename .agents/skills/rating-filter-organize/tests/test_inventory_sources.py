import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "inventory_sources.py"
SPEC = importlib.util.spec_from_file_location("inventory_sources", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


class InventorySourcesTest(unittest.TestCase):
    cycle_id = "daily-2026-08-02"
    window_since = "2026-08-01T00:00:00+08:00"
    window_until = "2026-08-02T00:00:00+08:00"

    def ensure_source_contract(self, source_root):
        manifest_path = source_root / "crawl_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.setdefault("cycle_id", self.cycle_id)
        manifest.setdefault("window_since", self.window_since)
        manifest.setdefault("window_until", self.window_until)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        for directory in manifest.get("record_directories", []):
            record_root = source_root / directory
            candidates = [record_root / "record.md", record_root / "source_record.md"]
            for record_path in (path for path in candidates if path.is_file()):
                text = record_path.read_text(encoding="utf-8")
                fields = MODULE.front_matter(text)
                additions = []
                if "window_since" not in fields:
                    additions.append(f'window_since: "{self.window_since}"')
                if "window_until" not in fields:
                    additions.append(f'window_until: "{self.window_until}"')
                if "status" not in fields and "crawl_status" not in fields:
                    additions.append("status: complete")
                if additions:
                    additions_text = "\n".join(additions)
                    record_path.write_text(
                        text.replace("\n---\n", f"\n{additions_text}\n---\n", 1),
                        encoding="utf-8",
                    )
        return manifest

    def source_receipt(self, root, extra_sources=None):
        sources = []
        for source_root in sorted(root.iterdir()):
            manifest_path = source_root / "crawl_manifest.json"
            if not source_root.is_dir() or not manifest_path.is_file():
                continue
            manifest = self.ensure_source_contract(source_root)
            record_paths = []
            for directory in manifest.get("record_directories", []):
                record_root = source_root / directory
                candidates = [record_root / "record.md", record_root / "source_record.md"]
                record_paths.extend(path.as_posix() for path in candidates if path.is_file())
            sources.append(
                {
                    "skill": source_root.name,
                    "source_name": source_root.name,
                    "source_directory": source_root.name,
                    "status": manifest["status"],
                    "error": None,
                    "detail": "source completed",
                    "record_count": len(record_paths),
                    "error_count": 0,
                    "cycle_id": self.cycle_id,
                    "window_since": self.window_since,
                    "window_until": self.window_until,
                    "manifest_path": manifest_path.as_posix(),
                    "record_paths": record_paths,
                }
            )
        sources.extend(extra_sources or [])
        status_counts = {}
        for source in sources:
            status = source["status"]
            status_counts[status] = status_counts.get(status, 0) + 1
        return {
            "schema_version": 2,
            "cycle_id": self.cycle_id,
            "window_since": self.window_since,
            "window_until": self.window_until,
            "discovered_count": len(sources),
            "completed_count": len(sources),
            "record_count": sum(source["record_count"] for source in sources),
            "status_counts": status_counts,
            "sources": sources,
        }

    def build_inventory(self, root, receipt=None):
        return MODULE.build_inventory(
            root,
            receipt or self.source_receipt(root),
            cycle_id=self.cycle_id,
            run_id="rating-test-run",
            project_root=root.parent,
            expected_source_skills=[
                source["skill"]
                for source in (receipt or self.source_receipt(root))["sources"]
            ],
        )

    def test_groups_exact_arxiv_identity_across_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for source in ("arxiv", "huggingface"):
                source_root = root / source
                source_root.mkdir()
                directories = ["one"]
                (source_root / "crawl_manifest.json").write_text(
                    json.dumps({"status": "success", "record_directories": directories}),
                    encoding="utf-8",
                )
                if directories:
                    record_root = source_root / "one"
                    record_root.mkdir()
                    (record_root / "record.md").write_text(
                        "---\nsource: %s\nsource_id: \"2607.12345\"\n"
                        "title: \"A paper\"\nabstract_url: \"https://arxiv.org/abs/2607.12345v2\"\n"
                        "---\nBody\n" % source,
                        encoding="utf-8",
                    )
            (root / "new_source").mkdir()
            (root / "new_source" / "crawl_manifest.json").write_text(
                json.dumps({"status": "success", "record_directories": []}),
                encoding="utf-8",
            )

            result = self.build_inventory(root)

        self.assertEqual(result["source_record_count"], 2)
        self.assertEqual(result["canonical_identity_count"], 1)
        self.assertEqual(result["records"][0]["candidate_id"], "arxiv--2607.12345")
        self.assertEqual(len(result["exact_identity_groups"][0]["record_paths"]), 2)

    def test_non_arxiv_url_uses_explicit_canonical_url(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "new_official_source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps({"status": "success", "record_directories": ["one"]}),
                encoding="utf-8",
            )
            record_root = source_root / "one"
            record_root.mkdir()
            (record_root / "record.md").write_text(
                "---\nsource: new_official_source\nsource_id: \"official:1\"\n"
                "title: \"Release\"\nurl: \"https://example.com/release\"\n---\nBody\n",
                encoding="utf-8",
            )

            first = self.build_inventory(root)
            second = self.build_inventory(root)

        self.assertEqual(first, second)
        self.assertEqual(
            first["records"][0]["candidate_id"],
            "url--https%3A%2F%2Fexample.com%2Frelease",
        )
        self.assertNotIn("sha256", json.dumps(first))

    def test_ordinary_url_fragments_collapse_to_one_identity(self):
        self.assertEqual(MODULE.canonical_url("relative/path#citation"), "relative/path")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "ordinary_source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps(
                    {"status": "success", "record_directories": ["one", "two"]}
                ),
                encoding="utf-8",
            )
            for directory, fragment in (("one", "citation-1"), ("two", "citation-2")):
                record_root = source_root / directory
                record_root.mkdir()
                (record_root / "record.md").write_text(
                    "---\nsource: ordinary_source\n"
                    f"source_id: {directory}\n"
                    f'title: "Article {directory}"\n'
                    f'url: "https://example.com/article#{fragment}"\n'
                    "---\nBody\n",
                    encoding="utf-8",
                )

            result = self.build_inventory(root)

        self.assertEqual(result["canonical_identity_count"], 1)
        self.assertEqual(
            {record["url"] for record in result["records"]},
            {"https://example.com/article"},
        )
        self.assertTrue(
            all(not record["url_fragment_identity"] for record in result["records"])
        )

    def test_ai_policy_daily_explicit_story_fragments_remain_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "ai_official_policy_source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps(
                    {"status": "success", "record_directories": ["one", "two"]}
                ),
                encoding="utf-8",
            )
            for directory, fragment in (("one", "story-one"), ("two", "story-two")):
                record_root = source_root / directory
                record_root.mkdir()
                (record_root / "record.md").write_text(
                    "---\nsource: ai_official_policy_source\n"
                    "source_name: ai_policy_daily\n"
                    f"source_id: aipd:{directory}\n"
                    f'title: "Story {directory}"\n'
                    "url_fragment_identity: true\n"
                    "url: \"https://aipolicydaily.org/archive/daily/"
                    f"2026-08-01/#{fragment}\"\n"
                    "---\nBody\n",
                    encoding="utf-8",
                )

            result = self.build_inventory(root)

        self.assertEqual(result["canonical_identity_count"], 2)
        self.assertEqual(
            {record["url"] for record in result["records"]},
            {
                "https://aipolicydaily.org/archive/daily/2026-08-01/#story-one",
                "https://aipolicydaily.org/archive/daily/2026-08-01/#story-two",
            },
        )
        self.assertTrue(
            all(record["url_fragment_identity"] for record in result["records"])
        )

    def test_article_citation_does_not_change_article_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "new_info_source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps({"status": "success", "record_directories": ["one"]}),
                encoding="utf-8",
            )
            record_root = source_root / "one"
            record_root.mkdir()
            (record_root / "record.md").write_text(
                "---\nsource: new_info_source\nsource_id: \"media:1\"\n"
                "title: \"Article\"\nurl: \"https://example.com/article\"\n"
                "---\nThis cites https://arxiv.org/abs/2303.17564.\n",
                encoding="utf-8",
            )

            result = self.build_inventory(root)

        record = result["records"][0]
        self.assertEqual(record["candidate_id"], "url--https%3A%2F%2Fexample.com%2Farticle")
        self.assertIsNone(record["normalized_arxiv_id"])

    def test_arxiv_source_id_wins_over_body_citation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "new_arxiv_source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps({"status": "success", "record_directories": ["2607.28733"]}),
                encoding="utf-8",
            )
            record_root = source_root / "2607.28733"
            record_root.mkdir()
            (record_root / "record.md").write_text(
                "---\nsource: new_arxiv_source\nsource_id: \"2607.28733\"\n"
                "title: \"A paper\"\nabstract_url: \"https://arxiv.org/abs/2607.28733v1\"\n"
                "---\nThis cites arXiv:2605.26234v2.\n",
                encoding="utf-8",
            )

            result = self.build_inventory(root)

        record = result["records"][0]
        self.assertEqual(record["candidate_id"], "arxiv--2607.28733")
        self.assertEqual(record["normalized_arxiv_id"], "2607.28733")

    def test_preserves_scalar_metadata_from_record_and_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "papers_added_later"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps(
                    {
                        "status": "partial",
                        "category_hint": "Paper",
                        "source_category_hint": "News",
                        "content_status": "manifest-default",
                        "source_role": "primary",
                        "record_directories": ["one", "two"],
                    }
                ),
                encoding="utf-8",
            )
            for directory, extra in (("one", "content_status: partial"), ("two", "")):
                record_root = source_root / directory
                record_root.mkdir()
                (record_root / "record.md").write_text(
                    "---\nsource_id: \"item:%s\"\ntitle: \"Item\"\n"
                    "category_hint: Paper\n%s\n---\nBody\n" % (directory, extra),
                    encoding="utf-8",
                )

            result = self.build_inventory(root)

        records = {record["source_id"]: record for record in result["records"]}
        self.assertEqual(records["item:one"]["category_hint"], "Paper")
        self.assertEqual(records["item:one"]["content_status"], "partial")
        self.assertEqual(records["item:one"]["source_role"], "primary")
        self.assertEqual(records["item:two"]["content_status"], "manifest-default")
        self.assertEqual(result["manifests"][0]["category_hint"], "Paper")
        self.assertEqual(result["manifests"][0]["source_category_hint"], "News")

    def test_failed_discovered_source_without_manifest_remains_observable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed = {
                "skill": "failed_source_crawler",
                "source_name": "failed_source",
                "source_directory": "failed_source",
                "status": "failed",
                "error": "startup failed before a manifest was written",
                "detail": "source startup failed",
                "record_count": 0,
                "error_count": 1,
                "cycle_id": self.cycle_id,
                "window_since": self.window_since,
                "window_until": self.window_until,
                "manifest_path": None,
                "record_paths": [],
            }

            result = self.build_inventory(root, self.source_receipt(root, [failed]))

        self.assertEqual(result["discovered_sources"], ["failed_source"])
        self.assertEqual(result["sources"][0]["status"], "failed")
        self.assertEqual(result["sources"][0]["record_paths"], [])
        self.assertEqual(result["source_record_count"], 0)

    def test_receipt_must_cover_every_dynamically_discovered_skill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "unreceipted_source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps({"status": "success_empty", "record_directories": []}),
                encoding="utf-8",
            )

            receipt = {
                "schema_version": 2,
                "cycle_id": self.cycle_id,
                "window_since": self.window_since,
                "window_until": self.window_until,
                "discovered_count": 0,
                "completed_count": 0,
                "record_count": 0,
                "status_counts": {},
                "sources": [],
            }
            with self.assertRaisesRegex(ValueError, "dynamically discovered skills"):
                MODULE.build_inventory(
                    root,
                    receipt,
                    cycle_id=self.cycle_id,
                    run_id="rating-test-run",
                    project_root=root.parent,
                    expected_source_skills=["unreceipted_source"],
                )

    def test_receipt_manifest_and_record_sets_must_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "partial_source"
            source_root.mkdir()
            manifest_path = source_root / "crawl_manifest.json"
            manifest_path.write_text(
                json.dumps({"status": "partial", "record_directories": ["one"]}),
                encoding="utf-8",
            )
            record_root = source_root / "one"
            record_root.mkdir()
            record = record_root / "record.md"
            record.write_text(
                "---\nsource_id: one\ntitle: One\n---\nBody\n", encoding="utf-8"
            )
            receipt = self.source_receipt(root)

            receipt["sources"][0]["manifest_path"] = (
                root / "other" / "crawl_manifest.json"
            ).as_posix()
            with self.assertRaises((ValueError, FileNotFoundError)):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            receipt["sources"][0]["record_paths"] = []
            receipt["sources"][0]["record_count"] = 0
            receipt["record_count"] = 0
            with self.assertRaisesRegex(ValueError, "record paths do not match"):
                self.build_inventory(root, receipt)

    def test_partial_records_survive_and_cycle_window_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "partial_source"
            source_root.mkdir()
            manifest_path = source_root / "crawl_manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "status": "partial",
                        "cycle_id": self.cycle_id,
                        "window_since": self.window_since,
                        "window_until": self.window_until,
                        "record_directories": ["one"],
                    }
                ),
                encoding="utf-8",
            )
            record_root = source_root / "one"
            record_root.mkdir()
            (record_root / "record.md").write_text(
                "---\nsource_id: one\ntitle: One\ncontent_status: partial\n---\nBody\n",
                encoding="utf-8",
            )
            receipt = self.source_receipt(root)

            result = self.build_inventory(root, receipt)
            self.assertEqual(result["sources"][0]["status"], "partial")
            self.assertEqual(result["source_record_count"], 1)
            self.assertEqual(result["records"][0]["content_status"], "partial")

            receipt["sources"][0]["window_until"] = "2026-08-03T00:00:00+08:00"
            with self.assertRaisesRegex(ValueError, "wrong frozen window"):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            receipt["sources"][0]["status"] = "unknown"
            with self.assertRaisesRegex(ValueError, "invalid status"):
                self.build_inventory(root, receipt)

    def test_build_inventory_records_the_receipt_source_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "aggregate_source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps({"status": "success", "record_directories": ["one"]}),
                encoding="utf-8",
            )
            record_root = source_root / "one"
            record_root.mkdir()
            (record_root / "record.md").write_text(
                "---\nsource: subfeed\nsource_name: specific_feed\n"
                "source_id: item-1\ntitle: One\n---\nBody\n",
                encoding="utf-8",
            )

            result = self.build_inventory(root)

        self.assertEqual(result["records"][0]["inventory_source_name"], "aggregate_source")
        self.assertEqual(result["records"][0]["source_name"], "specific_feed")

    def test_receipt_requires_schema_v2_and_exact_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            (source_root / "crawl_manifest.json").write_text(
                json.dumps({"status": "success_empty", "record_directories": []}),
                encoding="utf-8",
            )
            receipt = self.source_receipt(root)

            receipt["schema_version"] = 1
            with self.assertRaisesRegex(ValueError, "schema_version must be 2"):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            del receipt["discovered_count"]
            with self.assertRaisesRegex(ValueError, "missing required fields"):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            receipt["record_count"] = 1
            with self.assertRaisesRegex(ValueError, "record_count does not match"):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            receipt["discovered_count"] = 2
            with self.assertRaisesRegex(ValueError, "discovered_count does not match"):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            receipt["completed_count"] = 0
            with self.assertRaisesRegex(ValueError, "must be terminal"):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            receipt["status_counts"] = {"success": 1}
            with self.assertRaisesRegex(ValueError, "status_counts does not match"):
                self.build_inventory(root, receipt)

            receipt = self.source_receipt(root)
            del receipt["sources"][0]["detail"]
            with self.assertRaisesRegex(ValueError, "missing required fields"):
                self.build_inventory(root, receipt)

    def test_manifest_fields_and_record_frozen_window_are_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_root = root / "source"
            source_root.mkdir()
            manifest_path = source_root / "crawl_manifest.json"
            manifest_path.write_text(
                json.dumps({"status": "success", "record_directories": ["one"]}),
                encoding="utf-8",
            )
            record_root = source_root / "one"
            record_root.mkdir()
            record_path = record_root / "record.md"
            record_path.write_text(
                "---\nsource_id: one\ntitle: One\n---\nBody\n", encoding="utf-8"
            )
            receipt = self.source_receipt(root)

            original_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for field in ("cycle_id", "window_since", "window_until", "status"):
                with self.subTest(missing_manifest_field=field):
                    manifest = dict(original_manifest)
                    del manifest[field]
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "missing required fields"):
                        self.build_inventory(root, receipt)

            manifest_path.write_text(json.dumps(original_manifest), encoding="utf-8")
            self.ensure_source_contract(source_root)
            stale = record_path.read_text(encoding="utf-8").replace(
                self.window_since, "2026-07-31T00:00:00+08:00"
            )
            record_path.write_text(stale, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match the frozen window"):
                self.build_inventory(root, receipt)

            record_path.write_text(
                stale.replace("status: complete\n", "").replace(
                    "2026-07-31T00:00:00+08:00", self.window_since
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "requires status or crawl_status"):
                self.build_inventory(root, receipt)

    def test_production_receipt_path_is_exact_and_not_a_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            project_root = Path(temporary)
            crawl_root = project_root / "working_tmp"
            phase_root = crawl_root / ".phase_prepare_candidates"
            phase_root.mkdir(parents=True)
            receipt_path = MODULE.production_source_receipt_path(
                self.cycle_id, project_root=project_root
            )
            receipt_path.write_text("{}", encoding="utf-8")
            self.assertEqual(
                MODULE.validate_production_source_receipt_path(
                    receipt_path,
                    cycle_id=self.cycle_id,
                    project_root=project_root,
                    crawl_root=crawl_root,
                ),
                receipt_path,
            )

            other = phase_root / "source-discovery-other.json"
            other.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source-discovery-<CYCLE_ID>"):
                MODULE.validate_production_source_receipt_path(
                    other,
                    cycle_id=self.cycle_id,
                    project_root=project_root,
                    crawl_root=crawl_root,
                )

            receipt_path.unlink()
            target = phase_root / "receipt-target.json"
            target.write_text("{}", encoding="utf-8")
            receipt_path.symlink_to(target)
            with self.assertRaisesRegex(ValueError, "must not use symlinks"):
                MODULE.validate_production_source_receipt_path(
                    receipt_path,
                    cycle_id=self.cycle_id,
                    project_root=project_root,
                    crawl_root=crawl_root,
                )

    def test_immutable_output_create_exact_reuse_and_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "shared" / "source_inventory.json"
            self.assertTrue(MODULE.write_json_immutable(output, {"value": 1}))
            self.assertFalse(MODULE.write_json_immutable(output, {"value": 1}))
            with self.assertRaisesRegex(ValueError, "different content"):
                MODULE.write_json_immutable(output, {"value": 2})
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), {"value": 1})

    def test_fallback_identity_percent_encoding_prevents_punctuation_collision(self):
        first, basis = MODULE.make_candidate_id(
            source_directory="aggregate",
            source="feed/name",
            source_id="item:one",
            url=None,
            normalized_arxiv_id=None,
        )
        second, _ = MODULE.make_candidate_id(
            source_directory="aggregate",
            source="feed:name",
            source_id="item/one",
            url=None,
            normalized_arxiv_id=None,
        )
        self.assertEqual(basis, "source_identity")
        self.assertNotEqual(first, second)
        self.assertEqual(
            first, "source--source=feed%2Fname&source_id=item%3Aone"
        )


if __name__ == "__main__":
    unittest.main()
