import copy
import importlib.util
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from clew_okf.stages.step_01_ingest.documents import load_documents
from clew_okf.domain.model import Block, Source
from clew_okf.stages.step_01_ingest.raw_report_store import load_raw_report


class RawFileReportTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.root = self.base / "reports"
        self.root.mkdir()
        self.path = self.root / "report.md"
        self.path.write_text("공기 누설 점검 결과 정상.\n", encoding="utf-8")
        self.output = self.base / "wiki-output"
        self.entry = {
            "report_id": "REPORT-1", "version": 1, "path": "report.md", "revision": "LOG1",
            "status": "finalized", "reuse_approved": True,
            "metadata": {"plant_id": "P1", "equipment_id": "EQ1", "event_id": "EVENT-1"},
        }
        self.mapping = {"files": {"root": "reports", "entries": [self.entry]}}

    def load(self):
        return load_raw_report(self.mapping, None, self.base, self.output, "REPORT-1", 1, "file")

    def test_selected_original_is_unscoped_and_excludes_unlisted_files(self):
        (self.root / "unlisted.pdf").write_bytes(b"invalid binary")
        raw = self.load()
        self.assertEqual(raw.source.key, "raw-report:file:REPORT-1@1")
        self.assertEqual(raw.source.kind, "report_input")
        self.assertEqual(raw.source.concept_type, "Report Original")
        self.assertEqual(raw.source.equipment_ids, ())
        self.assertFalse(raw.source.synthesize)
        self.assertEqual(raw.source.revision, "LOG1")
        self.assertEqual(raw.source.blocks[0].text, "공기 누설 점검 결과 정상.\n")
        self.assertEqual(raw.metadata["equipment_id"], "EQ1")
        self.assertEqual(raw.origin, {"kind": "file", "input_digest": raw.fingerprint})

    def test_fingerprint_covers_bytes_metadata_and_extracted_blocks(self):
        initial = self.load()
        # The decoder removes a BOM, while the bytes digest still detects its addition.
        self.path.write_bytes(b"\xef\xbb\xbf" + self.path.read_bytes())
        with_bom = self.load()
        self.assertEqual(initial.source.blocks, with_bom.source.blocks)
        self.assertNotEqual(initial.fingerprint, with_bom.fingerprint)
        self.entry["metadata"]["event_id"] = "EVENT-2"
        changed_metadata = self.load()
        self.assertNotEqual(with_bom.fingerprint, changed_metadata.fingerprint)
        self.path.write_text("공기 누설 발견.\n", encoding="utf-8")
        self.assertNotEqual(changed_metadata.fingerprint, self.load().fingerprint)

    def test_original_change_during_extraction_is_rejected(self):
        def changing_extractor(*args, **kwargs):
            result = load_documents(*args, **kwargs)
            self.path.write_text("changed while extracting", encoding="utf-8")
            return result

        with (
            patch("clew_okf.stages.step_01_ingest.raw_report_store.load_documents", side_effect=changing_extractor),
            self.assertRaisesRegex(RuntimeError, "^Raw report original is unavailable or invalid$"),
        ):
            self.load()

    def test_manifest_requires_one_approved_typed_record_and_revision(self):
        original = copy.deepcopy(self.entry)
        for mutation in (
            {"status": "draft"}, {"reuse_approved": "true"}, {"reuse_approved": 1},
            {"reuse_approved": False}, {"revision": None}, {"metadata": {"equipment_id": "EQ 1"}},
            {"metadata": {"status": "finalized"}},
        ):
            with self.subTest(mutation=mutation):
                self.mapping["files"]["entries"] = [{**original, **mutation}]
                with self.assertRaisesRegex(RuntimeError, "^Raw report metadata is invalid or not approved$"):
                    self.load()
        for entries in ([], [original, original], [{**original, "version": True}]):
            with self.subTest(entry_count=len(entries)):
                self.mapping["files"]["entries"] = entries
                with self.assertRaisesRegex(RuntimeError, "^Raw report metadata is invalid or not approved$"):
                    self.load()

    def test_paths_reject_escape_links_and_output_overlap(self):
        for path in ("../outside.md", str(self.path), "sub/../report.md"):
            with self.subTest(path=path):
                self.entry["path"] = path
                with self.assertRaises(ValueError):
                    self.load()
        self.entry["path"] = "link.md"
        (self.root / "link.md").symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.load()
        self.entry["path"] = "linked/report.md"
        (self.root / "linked").symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            self.load()
        self.entry["path"] = "report.md"
        root_link = self.base / "root-link"
        root_link.symlink_to(self.root, target_is_directory=True)
        self.mapping["files"]["root"] = "root-link"
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            self.load()
        self.mapping["files"]["root"] = "reports"
        self.output = self.root / "output"
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            self.load()

    def test_binary_formats_delegate_to_existing_allowlisted_document_reader(self):
        for extension in (".hwp", ".hwpx", ".pdf"):
            with self.subTest(extension=extension):
                path = self.root / f"report{extension}"
                path.write_bytes(b"synthetic parser input")
                self.entry["path"] = path.name
                parsed = Source("docs:input", "Original report", "document", path.as_uri(),
                                (Block("page 1", "공기 누설 없음"),), revision="LOG1")
                with patch("clew_okf.stages.step_01_ingest.raw_report_store.load_documents", return_value=([parsed], [])) as parser:
                    raw = self.load()
                settings = parser.call_args.args[0]
                self.assertTrue(settings["include_only_listed"])
                self.assertEqual(settings["files"], {path.name: {"revision": "LOG1"}})
                self.assertEqual(raw.source.blocks, parsed.blocks)


@unittest.skipUnless(importlib.util.find_spec("psycopg"), "psycopg is an optional database dependency")
class RawDatabaseReportTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "table": "reports.Raw Reports",
            "id_column": "report_id",
            "version_column": "version",
            "revision_column": "revision",
            "status_column": "status",
            "approval_column": "approved",
            "content_columns": ['Body "Text"'],
            "metadata_columns": {"equipment_id": "asset", "event_id": "event"},
            "dsn_env": "RAW_REPORT_DSN",
        }
        self.row = ("REPORT-1", 1, "LOG1", "finalized", True, "조치 결과\n정상", "EQ1", "EVENT-1")
        self.cursor = MagicMock()
        self.cursor.fetchall.return_value = [self.row]
        self.connection = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value = self.cursor
        connect = MagicMock()
        connect.return_value.__enter__.return_value = self.connection
        connection_patch = patch("psycopg.connect", connect)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        env = patch.dict(os.environ, {"RAW_REPORT_DSN": "synthetic-private-dsn"})
        env.start()
        self.addCleanup(env.stop)
        self.connect = connect

    def load(self):
        return load_raw_report({"database": self.settings}, {"name": "factory"},
                               Path("/configured"), Path("/output"), "REPORT-1", 1, "database")

    def test_safe_snapshot_query_and_metadata_content_fingerprint(self):
        from psycopg import IsolationLevel

        first = self.load()
        query, params = self.cursor.execute.call_args.args
        self.assertEqual(query.as_string(),
                         'SELECT "report_id", "version", "revision", "status", "approved", "Body ""Text""", '
                         '"asset", "event" FROM "reports"."Raw Reports" '
                         'WHERE "report_id" = %s AND "version" = %s LIMIT 2')
        self.assertEqual(params, ("REPORT-1", 1))
        self.assertTrue(self.connection.read_only)
        self.assertEqual(self.connection.isolation_level, IsolationLevel.REPEATABLE_READ)
        self.assertEqual(self.cursor.execute.call_args_list[0].args,
                         ("SET LOCAL statement_timeout = '15s'",))
        self.assertEqual(first.source.key, "raw-report:database:REPORT-1@1")
        self.assertEqual(first.source.blocks, (Block('column Body "Text"', "조치 결과\n정상"),))
        self.assertFalse(first.source.synthesize)
        self.assertEqual(first.source.equipment_ids, ())
        self.assertEqual(first.metadata["event_id"], "EVENT-1")
        self.assertEqual(first.origin, {"kind": "database", "input_digest": first.fingerprint})
        updated = (*self.row[:5], "조치 결과 재확인", *self.row[6:])
        self.cursor.fetchall.return_value = [updated]
        self.assertNotEqual(first.fingerprint, self.load().fingerprint)
        self.assertNotIn("synthetic-private-dsn", first.source.resource)

    def test_missing_duplicate_invalid_identity_approval_and_metadata_fail_closed(self):
        values = [
            [], [self.row, self.row],
            [("OTHER", *self.row[1:])],
            [(self.row[0], "1", *self.row[2:])],
            [(self.row[0], 1, None, *self.row[3:])],
            [(*self.row[:3], "draft", *self.row[4:])],
            [(*self.row[:4], "true", *self.row[5:])],
            [(*self.row[:4], False, *self.row[5:])],
            [(*self.row[:6], 123, self.row[-1])],
        ]
        for records in values:
            with self.subTest(records=records):
                self.cursor.fetchall.return_value = records
                with self.assertRaisesRegex(RuntimeError, "^Raw report metadata is invalid or not approved$"):
                    self.load()

    def test_configuration_errors_and_database_failures_are_separate(self):
        original = copy.deepcopy(self.settings)
        for changes in (
            {"table": "unqualified"}, {"content_columns": []},
            {"metadata_columns": {"unknown": "field"}}, {"dsn_env": "not an env"},
        ):
            with self.subTest(changes=changes):
                self.settings = {**original, **changes}
                with self.assertRaises(ValueError):
                    self.load()
        self.connect.assert_not_called()
        self.settings = original
        import psycopg
        self.cursor.execute.side_effect = psycopg.OperationalError("secret-password private-server")
        with self.assertRaisesRegex(RuntimeError, "^Raw report database retrieval failed$"):
            self.load()


if __name__ == "__main__":
    unittest.main()
