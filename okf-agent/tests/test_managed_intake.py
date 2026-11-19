import hashlib
import io
import sqlite3
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from clew_okf.runtime.jobs import JobStore, enqueue_in_transaction
from clew_okf.stages.step_01_ingest.managed_intake import (
    InputConflictError, InputNotFoundError, InputValidationError, ManagedIntake, lookup_managed,
)


PDF = b"%PDF-1.7\n% Synthetic upload for intake tests only.\n"
HWP = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 24


def hwpx_bytes():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as package:
        package.writestr("mimetype", "application/hwp+zip")
        package.writestr("Contents/section0.xml", "<section/>")
    return stream.getvalue()


class ManagedIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "state.sqlite3"
        self.files = self.root / "uploads"
        self.intake = ManagedIntake(self.database, self.files)
        self.metadata = {"plant_id": "P1", "equipment_id": "EQ1", "event_id": "EVENT-1"}

    def text(self, report_id="R1", version=1, **changes):
        arguments = {"body": "관측만 기록했다.\r\n조건·조치·결과는 아직 없다.\n", "revision": "REV1", "metadata": self.metadata}
        arguments.update(changes)
        return self.intake.save_text(report_id, version, **arguments)

    def file(self, report_id="F1", version=1, **changes):
        arguments = {"stream": io.BytesIO(PDF), "filename": "정비보고서.pdf", "revision": "REV1", "metadata": self.metadata}
        arguments.update(changes)
        return self.intake.save_file(report_id, version, **arguments)

    def counts(self):
        with sqlite3.connect(self.database) as connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("managed_reports", "jobs", "job_events"))

    def test_text_and_job_are_durable_and_preserve_partial_draft_exactly(self):
        result = self.text(metadata={})
        report, job = result["report"], result["job"]
        self.assertEqual((report["status"], report["reuse_approved"]), ("draft", False))
        self.assertEqual(report["body"], "관측만 기록했다.\r\n조건·조치·결과는 아직 없다.\n")
        self.assertEqual(report["metadata"], {})
        self.assertEqual(report["content_sha"], hashlib.sha256(report["body"].encode("utf-8")).hexdigest())
        self.assertEqual(report["source"], "text")
        self.assertIsNone(report["path"])
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["kind"], "report")
        self.assertEqual(job["payload"], {"report_id": "R1", "version": 1, "source": "managed_text"})
        reopened = ManagedIntake(self.database, self.files)
        self.assertEqual(reopened.jobs.get(job["id"]), job)
        self.assertEqual(lookup_managed(self.database, "R1", 1), report)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_identical_text_is_idempotent_and_metadata_is_copied(self):
        first = self.text()
        second = self.text(metadata={"event_id": "EVENT-1", "equipment_id": "EQ1", "plant_id": "P1", "supersedes_version": None})
        self.assertEqual(first, second)
        self.metadata["equipment_id"] = "CHANGED"
        self.assertEqual(lookup_managed(self.database, "R1", 1)["metadata"]["equipment_id"], "EQ1")
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_approval_changes_enqueue_new_generations_without_mutating_content(self):
        initial = self.text()
        approved = self.intake.set_approval("R1", 1, status="finalized", reuse_approved=True)
        self.assertNotEqual(initial["job"]["id"], approved["job"]["id"])
        self.assertEqual(approved["report"]["approval_revision"], 2)
        self.assertEqual((approved["report"]["status"], approved["report"]["reuse_approved"]), ("finalized", True))
        self.assertEqual(approved["report"]["content_sha"], initial["report"]["content_sha"])
        self.assertEqual(approved["report"]["body"], initial["report"]["body"])
        self.assertEqual(self.intake.set_approval("R1", 1, "finalized", True), approved)
        # A regular save with default draft/false cannot revoke the approved report.
        self.assertEqual(self.text(), approved)
        revoked = self.intake.set_approval("R1", 1, "finalized", False)
        self.assertEqual(revoked["report"]["approval_revision"], 3)
        self.assertNotEqual(revoked["job"]["id"], approved["job"]["id"])
        self.assertFalse(revoked["report"]["reuse_approved"])
        self.assertEqual(self.counts(), (1, 3, 3))

    def test_explicit_trusted_approval_is_supported_but_never_implicit(self):
        approved = self.text(status="finalized", reuse_approved=True)
        self.assertTrue(approved["report"]["reuse_approved"])
        self.assertEqual(approved["job"]["status"], "queued")
        self.assertFalse(self.text("R2")["report"]["reuse_approved"])
        self.assertFalse(self.text("R3", status="finalized", reuse_approved=False)["report"]["reuse_approved"])

    def test_immutable_text_source_revision_and_identifiers_conflict(self):
        first = self.text()
        for change in ({"body": "다른 원문"}, {"revision": "REV2"}, {"metadata": {**self.metadata, "event_id": "OTHER"}}):
            with self.subTest(change=change):
                with self.assertRaises(InputConflictError):
                    self.text(**change)
        with self.assertRaises(InputConflictError):
            self.file(report_id="R1")
        self.assertEqual(self.counts(), (1, 1, 1))
        self.assertEqual(lookup_managed(self.database, "R1", 1), first["report"])
        self.assertEqual(list(self.files.iterdir()), [])
        self.assertEqual(self.text(version=2, metadata={**self.metadata, "supersedes_version": 1})["report"]["version"], 2)

    def test_strict_metadata_identity_and_approval_validation(self):
        for report_id, version in (("../R1", 1), ("R1", True), ("R1", "1"), ("R1", 0)):
            with self.subTest(report_id=report_id, version=version):
                with self.assertRaises(InputValidationError):
                    self.text(report_id=report_id, version=version)
        for metadata in ({"status": "finalized"}, {"reuse_approved": True}, {"event_id": "E 1"}, {"equipment_id": "P1/EQ1"}, {"supersedes_version": True}):
            with self.subTest(metadata=metadata):
                with self.assertRaises(InputValidationError):
                    self.text(metadata=metadata)
        for status, approval in (("draft", True), ("approved", True), ("finalized", 1), ("finalized", "true")):
            with self.subTest(status=status, approval=approval):
                with self.assertRaises(InputValidationError):
                    self.text(status=status, reuse_approved=approval)
        self.assertEqual(self.counts(), (0, 0, 0))
        self.text()
        for status, approval in (("draft", True), ("finalized", 1)):
            with self.assertRaises(InputValidationError):
                self.intake.set_approval("R1", 1, status, approval)
        with self.assertRaises(InputNotFoundError):
            self.intake.set_approval("MISSING", 1, "finalized", True)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_report_and_job_both_rollback_if_enqueue_fails_after_job_insert(self):
        def enqueue_then_fail(connection, *arguments, **keywords):
            self.assertTrue(connection.in_transaction)
            enqueue_in_transaction(connection, *arguments, **keywords)
            raise RuntimeError("controlled enqueue failure after insert")

        with patch("clew_okf.stages.step_01_ingest.managed_intake.enqueue_in_transaction", side_effect=enqueue_then_fail):
            with self.assertRaisesRegex(RuntimeError, "controlled enqueue failure"):
                self.text()
        self.assertIsNone(lookup_managed(self.database, "R1", 1))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_approval_and_new_job_both_rollback_if_enqueue_fails(self):
        first = self.text()
        def enqueue_then_fail(connection, *arguments, **keywords):
            enqueue_in_transaction(connection, *arguments, **keywords)
            raise RuntimeError("approval enqueue failure")
        with patch("clew_okf.stages.step_01_ingest.managed_intake.enqueue_in_transaction", side_effect=enqueue_then_fail):
            with self.assertRaisesRegex(RuntimeError, "approval enqueue failure"):
                self.intake.set_approval("R1", 1, "finalized", True)
        self.assertEqual(lookup_managed(self.database, "R1", 1), first["report"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_file_upload_keeps_bytes_and_uses_only_server_generated_path(self):
        for report_id, filename, data in (("PDF", "한글보고서.PDF", PDF), ("HWP", "정비.hwp", HWP), ("HWPX", "점검.hwpx", hwpx_bytes())):
            with self.subTest(filename=filename):
                result = self.file(report_id, filename=filename, stream=io.BytesIO(data))
                report = result["report"]
                self.assertEqual(report["source"], "file")
                self.assertIsNone(report["body"])
                self.assertEqual(report["filename"], filename)
                self.assertRegex(report["path"], r"^[0-9a-f]{32}\.(pdf|hwp|hwpx)$")
                self.assertEqual((self.files / report["path"]).read_bytes(), data)
                self.assertEqual(report["content_sha"], hashlib.sha256(data).hexdigest())
                self.assertEqual(result["job"]["payload"], {"report_id": report_id, "version": 1, "source": "file"})
        self.assertEqual(self.counts(), (3, 3, 3))
        self.assertEqual(len(list(self.files.iterdir())), 3)

    def test_same_file_bytes_are_idempotent_even_with_another_display_name(self):
        first = self.file()
        repeated = self.file(filename="다른이름.pdf")
        self.assertEqual(first, repeated)
        self.assertEqual(repeated["report"]["filename"], "정비보고서.pdf")
        self.assertEqual(len(list(self.files.iterdir())), 1)
        with self.assertRaises(InputConflictError):
            self.file(stream=io.BytesIO(PDF + b"changed"))
        self.assertEqual(len(list(self.files.iterdir())), 1)
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_filename_traversal_unsupported_extension_and_bad_magic_rejected(self):
        for filename in ("../x.pdf", "/x.pdf", "dir/x.pdf", "dir\\x.pdf", "C:x.pdf", "x.txt", "x", "x.pdf\0", " x.pdf", "x.pdf\n"):
            with self.subTest(filename=repr(filename)):
                with self.assertRaises(InputValidationError):
                    self.file(filename=filename)
        for filename, data in (("x.pdf", b"not a PDF"), ("x.hwp", b"%PDF-1.7"), ("x.hwpx", b"not zip"), ("x.pdf", b"")):
            with self.subTest(filename=filename, data=data):
                with self.assertRaises(InputValidationError):
                    self.file(filename=filename, stream=io.BytesIO(data))
        self.assertEqual(list(self.files.iterdir()), [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_upload_and_utf8_text_limits_are_bounded_and_no_partial_rows_survive(self):
        limited = ManagedIntake(self.database, self.files, max_upload_bytes=32)
        exact = b"%PDF-1.7\n" + b"x" * 23
        self.assertEqual(len(exact), 32)
        class RecordingStream(io.BytesIO):
            sizes = []
            def read(self, size=-1):
                self.sizes.append(size)
                return super().read(size)
        stream = RecordingStream(exact + b"x")
        with self.assertRaises(InputValidationError):
            limited.save_file("TOO-BIG", 1, stream=stream, filename="report.pdf", revision="R1", metadata={})
        self.assertTrue(all(0 < size <= 33 for size in stream.sizes))
        self.assertEqual(list(self.files.iterdir()), [])
        result = limited.save_file("EXACT", 1, stream=io.BytesIO(exact), filename="report.pdf", revision="R1", metadata={})
        self.assertEqual((self.files / result["report"]["path"]).stat().st_size, 32)
        small = ManagedIntake(self.database, self.files, max_upload_bytes=4)
        with self.assertRaises(InputValidationError):
            small.save_text("UTF8", 1, body="한글", revision="R1", metadata={})
        self.assertIsNone(lookup_managed(self.database, "UTF8", 1))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_file_transaction_rollback_removes_only_own_unreferenced_file(self):
        existing = self.file()
        existing_path = self.files / existing["report"]["path"]
        def enqueue_then_fail(connection, *arguments, **keywords):
            enqueue_in_transaction(connection, *arguments, **keywords)
            raise RuntimeError("file job insert failed")
        with patch("clew_okf.stages.step_01_ingest.managed_intake.enqueue_in_transaction", side_effect=enqueue_then_fail):
            with self.assertRaisesRegex(RuntimeError, "file job insert failed"):
                self.file(report_id="F2")
        self.assertEqual(existing_path.read_bytes(), PDF)
        self.assertEqual(list(self.files.iterdir()), [existing_path])
        self.assertIsNone(lookup_managed(self.database, "F2", 1))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_reserved_server_name_cannot_overwrite_existing_upload(self):
        first = self.file()
        existing_path = self.files / first["report"]["path"]
        with patch("clew_okf.stages.step_01_ingest.managed_intake.uuid4", side_effect=[SimpleNamespace(hex=existing_path.stem), SimpleNamespace(hex="f" * 32)]):
            second = self.file("F2", stream=io.BytesIO(PDF + b"second"))
        self.assertNotEqual(second["report"]["path"], first["report"]["path"])
        self.assertEqual(existing_path.read_bytes(), PDF)
        self.assertEqual((self.files / second["report"]["path"]).read_bytes(), PDF + b"second")

    def test_bad_stream_or_fsync_failure_leaves_no_files_or_jobs(self):
        with self.assertRaises(InputValidationError):
            self.file(stream=io.StringIO("not binary"))
        with patch("clew_okf.stages.step_01_ingest.managed_intake.os.fsync", side_effect=OSError("controlled fsync failure")):
            with self.assertRaisesRegex(OSError, "controlled fsync failure"):
                self.file()
        self.assertEqual(list(self.files.iterdir()), [])
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_simultaneous_identical_saves_share_one_persisted_report_and_job(self):
        other = ManagedIntake(self.database, self.files)
        def save(intake):
            return intake.save_text("SHARED", 1, body="원문", revision="R1", metadata={})
        with ThreadPoolExecutor(max_workers=2) as executor:
            first, second = list(executor.map(save, (self.intake, other)))
        self.assertEqual(first["job"]["id"], second["job"]["id"])
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_readonly_lookup_does_not_create_storage_or_managed_table(self):
        missing = self.root / "not-created" / "missing.sqlite3"
        self.assertIsNone(lookup_managed(missing, "R1", 1))
        self.assertFalse(missing.exists())
        jobs_only = self.root / "jobs-only.sqlite3"
        JobStore(jobs_only)
        self.assertIsNone(lookup_managed(jobs_only, "R1", 1))
        with sqlite3.connect(jobs_only) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'managed_reports'").fetchone())
        self.assertIsNone(lookup_managed(self.database, "MISSING", 1))


if __name__ == "__main__":
    unittest.main()
