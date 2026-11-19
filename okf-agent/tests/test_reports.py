import hashlib
import unittest

from pydantic import ValidationError

from clew_okf.stages.step_06_publish.bundle import digest, evidence
from clew_okf.domain.model import Block, Equipment, Source
from clew_okf.domain.reports import ReportEvidence, ReportRecord, ReportRequest, validate_report_links


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.asset = Equipment("EQ1", {"plant_id": "P1"}, "equipment", "row:1")
        self.source = Source(
            key="manual:EQ1", title="정비 절차", kind="document", resource="manual.md",
            blocks=(Block("line:1", "전원을 차단한 뒤 베어링을 교체한다."),),
            equipment_ids=("P1/EQ1",), revision="R1",
        )
        identifier, block = evidence(self.source)[0]
        self.reference = {
            "evidence_id": identifier, "revision": "R1",
            "sha256": hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
            "quote": "전원을 차단한 뒤",
        }
        self.payload = {
            "report_id": "R-001", "version": 1, "plant_id": "P1", "equipment_id": "EQ1",
            "event_id": "EVENT-01", "title": "베어링 교체", "condition": "정상 운전 중",
            "observation": "진동 발생", "action": "전원 차단 후 베어링 교체", "result": "진동 해소",
            "status": "finalized", "reuse_approved": True, "evidence": [self.reference],
        }

    def record(self, **changes):
        return ReportRecord.model_validate({**self.payload, **changes})

    def codes(self, report=None, sources=None, equipment=None):
        return {
            item["code"] for item in validate_report_links(
                report if report is not None else self.record(),
                equipment if equipment is not None else [self.asset],
                sources if sources is not None else [self.source],
            )
        }

    def test_valid_report_and_canonical_hash(self):
        report = self.record()
        self.assertEqual(validate_report_links(report, [self.asset], [self.source]), [])
        self.assertEqual(report.equipment_key, "P1/EQ1")
        self.assertEqual(report.identity_key, "R-001@1")
        self.assertEqual(report.content_hash, digest(report.model_dump(mode="json")))
        with self.assertRaises(ValidationError):
            report.version = 2

    def test_strict_positive_request_and_record_versions(self):
        self.assertEqual(ReportRequest.model_validate({"version": 2}).version, 2)
        for value in (True, "1", 1.0, 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    ReportRequest.model_validate({"version": value})
                with self.assertRaises(ValidationError):
                    self.record(version=value)
        with self.assertRaises(ValidationError):
            ReportRequest.model_validate({"version": 1, "report_id": "R-001"})

    def test_invalid_record_input_is_rejected(self):
        changes = [
            {"report_id": "../report"}, {"report_id": "R" * 121}, {"plant_id": "P 1"},
            {"equipment_id": "P1/EQ1"}, {"event_id": "EVENT\n01"}, {"condition": "   "},
            {"action": ""}, {"title": "a" * 4001}, {"status": "draft"},
            {"reuse_approved": False}, {"reuse_approved": 1}, {"reuse_approved": "true"},
            {"supersedes_version": True}, {"supersedes_version": 0}, {"evidence": []},
            {"body": "unsupported field"},
        ]
        for change in changes:
            with self.subTest(change=change):
                with self.assertRaises(ValidationError):
                    self.record(**change)

    def test_invalid_or_duplicate_evidence_is_rejected(self):
        for change in (
            {"evidence_id": "src-" + "A" * 24}, {"sha256": "A" * 64},
            {"sha256": "a" * 63}, {"revision": " "}, {"quote": ""},
            {"quote": "a" * 601}, {"source_uri": "unsupported"},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValidationError):
                    ReportEvidence.model_validate({**self.reference, **change})
        with self.assertRaises(ValidationError):
            self.record(evidence=[self.reference, {**self.reference, "quote": "베어링"}])
        with self.assertRaises(ValidationError):
            self.record(evidence=[{**self.reference, "evidence_id": f"src-{i:024x}"} for i in range(17)])

    def test_all_required_report_text_and_evidence_reject_whitespace_only(self):
        for whitespace in (" ", "\t\n", "\u3000"):
            for field in ("title", "condition", "observation", "action", "result"):
                with self.subTest(field=field, whitespace=repr(whitespace)):
                    with self.assertRaises(ValidationError):
                        self.record(**{field: whitespace})
            for field in ("revision", "quote"):
                with self.subTest(field=field, whitespace=repr(whitespace)):
                    with self.assertRaises(ValidationError):
                        ReportEvidence.model_validate({**self.reference, field: whitespace})

    def test_hashes_preserve_events_and_ignore_report_identity_for_cases(self):
        original = self.record()
        renamed = self.record(report_id="R-002", version=2, title="다른 제목", supersedes_version=1)
        self.assertNotEqual(original.content_hash, renamed.content_hash)
        self.assertEqual(original.case_hash, renamed.case_hash)
        self.assertEqual(original.knowledge_hash, renamed.knowledge_hash)
        different_event = self.record(event_id="EVENT-02")
        self.assertNotEqual(original.case_hash, different_event.case_hash)
        self.assertEqual(original.knowledge_hash, different_event.knowledge_hash)
        self.assertNotEqual(original.knowledge_hash, self.record(result="미해소").knowledge_hash)

    def test_evidence_order_is_normalized_for_semantic_hashes(self):
        other = {**self.reference, "evidence_id": "src-" + "f" * 24, "quote": "베어링"}
        first = self.record(evidence=[self.reference, other])
        reversed_order = self.record(evidence=[other, self.reference])
        self.assertEqual(first.case_hash, reversed_order.case_hash)
        self.assertEqual(first.knowledge_hash, reversed_order.knowledge_hash)
        self.assertNotEqual(first.content_hash, reversed_order.content_hash)

    def test_unregistered_unknown_and_wrong_equipment_hold(self):
        self.assertEqual(self.codes(equipment=[]), {"asset_not_registered"})
        self.assertEqual(self.codes(sources=[]), {"evidence_unknown"})
        wrong = Source(
            self.source.key, self.source.title, self.source.kind, self.source.resource,
            self.source.blocks, ("P2/EQ1",), self.source.revision,
        )
        self.assertEqual(self.codes(sources=[wrong]), {"evidence_wrong_equipment"})

    def test_stale_revision_hash_and_inexact_quote_hold(self):
        stale = self.record(evidence=[{
            **self.reference, "revision": "R0", "sha256": "0" * 64, "quote": "차단 없이 교체",
        }])
        findings = validate_report_links(stale, [self.asset], [self.source])
        self.assertEqual(
            {item["code"] for item in findings},
            {"revision_mismatch", "evidence_hash_mismatch", "quote_not_found"},
        )
        for finding in findings:
            self.assertEqual(set(finding), {"code", "message", "evidence_ids"})
            self.assertEqual(finding["evidence_ids"], [self.reference["evidence_id"]])

    def test_missing_current_revision_holds(self):
        for revision in (None, "", "   "):
            with self.subTest(revision=revision):
                source = Source(
                    self.source.key, self.source.title, self.source.kind, self.source.resource,
                    self.source.blocks, self.source.equipment_ids, revision,
                )
                self.assertEqual(self.codes(sources=[source]), {"revision_missing"})

    def test_ambiguous_evidence_holds_and_out_of_scope_copy_does_not_hide_it(self):
        self.assertEqual(self.codes(sources=[self.source, self.source]), {"evidence_ambiguous"})
        wrong = Source(
            self.source.key, self.source.title, self.source.kind, self.source.resource,
            self.source.blocks, ("P2/EQ1",), self.source.revision,
        )
        self.assertEqual(self.codes(sources=[wrong, self.source]), set())


if __name__ == "__main__":
    unittest.main()
