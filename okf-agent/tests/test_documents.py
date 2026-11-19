import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clew_okf.cli import _build
from clew_okf.stages.step_01_ingest.documents import load_documents


class DocumentManifestTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.base = Path(folder.name)
        self.root = self.base / "originals"
        self.root.mkdir()
        self.output = self.base / "output"

    def load(self, **settings):
        return load_documents({"root": self.root.name, **settings}, self.base, output=self.output)

    def test_manifest_skips_unlisted_files_and_preserves_explicit_scope(self):
        manuals = self.root / "P2" / "EQ-1"
        manuals.mkdir(parents=True)
        (manuals / "current.md").write_text("Approved instructions", encoding="utf-8")
        (manuals / "legacy.md").write_bytes(b"\xff")
        (manuals / "legacy.pdf").write_bytes(b"invalid PDF")
        (self.base / "equipment.csv").write_text(
            "equipment_id,name,plant_id\nEQ-1,First,P1\nEQ-1,Second,P2\n", encoding="utf-8"
        )
        config = self.base / "wiki.yml"
        config.write_text(yaml.safe_dump({
            "equipment_csv": "equipment.csv",
            "output": "output",
            "documents": {
                "root": "originals",
                "scope_from_path": True,
                "kordoc": "/nonexistent/kordoc",
                "files": {"P2/EQ-1/current.md": {"equipment_ids": ["P1/EQ-1"]}},
            },
        }), encoding="utf-8")

        report = _build(config)

        self.assertEqual(report["sources"], 1)
        self.assertEqual(report["unmapped_documents"], 0)
        pages = {
            yaml.safe_load(text.split("---", 2)[1])["equipment_key"]: text
            for page in (self.output / "current" / "equipment").glob("*.md")
            if (text := page.read_text(encoding="utf-8"))
        }
        self.assertIn("current", pages["P1/EQ-1"])
        self.assertNotIn("current", pages["P2/EQ-1"])
        self.assertFalse((self.output / "extraction-cache").exists())

    def test_discovery_can_be_enabled_with_a_nonempty_manifest(self):
        (self.root / "current.md").write_text("Approved instructions", encoding="utf-8")
        (self.root / "other.txt").write_text("Extra text", encoding="utf-8")
        (self.root / "drawing.dwg").write_bytes(b"drawing")

        sources, warnings = self.load(files={"current.md": {}}, include_only_listed=False)

        self.assertEqual([source.key for source in sources], ["docs:current.md", "docs:other.txt"])
        self.assertEqual(warnings, ["Unsupported document format: drawing.dwg"])

    def test_explicit_empty_allowlist_admits_no_documents(self):
        (self.root / "unspecified.md").write_text("Example", encoding="utf-8")

        discovered, _ = self.load(files={})
        admitted, _ = self.load(files={}, include_only_listed=True)

        self.assertEqual([source.key for source in discovered], ["docs:unspecified.md"])
        self.assertEqual(admitted, [])

    def test_missing_listed_file_still_fails(self):
        with self.assertRaisesRegex(ValueError, "Mapped document files are missing.*missing.md"):
            self.load(files={"missing.md": {}})

    def test_manifest_flag_must_be_boolean(self):
        with self.assertRaisesRegex(ValueError, "documents.include_only_listed must be a boolean"):
            self.load(include_only_listed="false")


if __name__ == "__main__":
    unittest.main()
