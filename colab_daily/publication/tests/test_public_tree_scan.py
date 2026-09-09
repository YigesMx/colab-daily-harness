import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).parents[3] / "scripts/public_tree_scan.py"
SPEC = importlib.util.spec_from_file_location("public_tree_scan", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PublicTreeScanTests(unittest.TestCase):
    def test_detects_private_value_without_scanning_ignored_runtime(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("PRIVATE_TOKEN=synthetic-private-value\n")
            (root / "public.py").write_text("VALUE = 'synthetic-private-value'\n")
            (root / ".local").mkdir()
            (root / ".local/private.txt").write_text("synthetic-private-value")
            findings = MODULE.scan(root)
            self.assertEqual([(path.name, kind) for path, kind in findings], [("public.py", "private configuration value")])

    def test_exact_generated_write_set_is_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("PRIVATE_DOMAIN=private.example.invalid\n")
            findings = MODULE.scan_file_map(root, {"docs/daily/page.md": b"private.example.invalid"})
            self.assertEqual([(path.as_posix(), kind) for path, kind in findings],
                             [("docs/daily/page.md", "private configuration value")])

    def test_environment_only_private_value_is_scanned(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("")
            findings = MODULE.scan_file_map(
                root,
                {"docs/daily/page.md": b"environment-private.example.invalid"},
                {"PRIVATE_DOMAIN": "environment-private.example.invalid"},
            )
            self.assertEqual([(path.as_posix(), kind) for path, kind in findings],
                             [("docs/daily/page.md", "private configuration value")])

    def test_environment_private_value_overrides_dotenv_value(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("PRIVATE_DOMAIN=dotenv-private.example.invalid\n")
            environment = {"PRIVATE_DOMAIN": "environment-private.example.invalid"}
            self.assertEqual(MODULE.private_values(root, environment), ["environment-private.example.invalid"])
            self.assertEqual(MODULE.scan_file_map(
                root, {"docs/daily/page.md": b"dotenv-private.example.invalid"}, environment), [])
            findings = MODULE.scan_file_map(
                root, {"docs/daily/page.md": b"environment-private.example.invalid"}, environment)
            self.assertEqual(findings[0][1], "private configuration value")

    def test_private_key_material_is_detected_without_echoing_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("")
            marker = b"-----BEGIN " + b"PRIVATE KEY-----"
            findings = MODULE.scan_file_map(root, {"docs/page.md": marker})
            self.assertEqual(findings[0][1], "private key material")

    def test_clean_generic_tree_passes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".env").write_text("PRIVATE_TOKEN=synthetic-private-value\n")
            (root / "public.md").write_text("Configure your own HTTPS endpoint.\n")
            self.assertEqual(MODULE.scan(root), [])


if __name__ == "__main__":
    unittest.main()
