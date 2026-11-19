import hashlib
import unittest
from unittest.mock import Mock

from clew_okf.stages.step_03_admission.admission_review import evaluate_report
from clew_okf.llm import JsonModel
from clew_okf.domain.model import Block, Source
from clew_okf.domain.reports import ReportRecord


class AdmissionReviewTests(unittest.TestCase):
    def setUp(self):
        self.report_id = "src-" + "a" * 24
        self.candidate_id = "src-" + "b" * 24
        self.rule_id = "src-" + "c" * 24
        self.report_text = "가동 전에 누설을 확인했고 정상 토출압력 6.5–7.5 bar를 확인했다."
        self.candidate_text = "이전 정비에서도 누설을 확인했다."
        self.lookup = {
            self.report_id: self.source("report.md", self.report_text),
            self.candidate_id: self.source("previous.md", self.candidate_text),
            self.rule_id: self.source("rule.md", "가동 전에 누설을 확인한다."),
        }
        self.report = self.report_record()
        self.candidate = self.report_record(
            report_id="CASE-1", evidence_id=self.candidate_id, quote=self.candidate_text
        )
        self.provider = Mock(spec=JsonModel)

    @staticmethod
    def source(key, text):
        block = Block("section 1", text)
        return Source(
            f"docs:{key}", key, "document", f"file:///{key}", (block,),
            equipment_ids=("P1/EQ1",), revision="R1",
        ), block

    def report_record(
        self, *, report_id="REPORT-1", event_id="EVENT-1", version=1, equipment_id="EQ1",
        evidence_id=None, quote=None,
    ):
        quote = quote or self.report_text
        return ReportRecord.model_validate({
            "report_id": report_id,
            "version": version,
            "plant_id": "P1",
            "equipment_id": equipment_id,
            "event_id": event_id,
            "title": "정비 결과",
            "condition": "가동 전",
            "observation": "공기 누설 점검",
            "action": "누설과 압력 확인",
            "result": "정상 범위 확인",
            "status": "finalized",
            "reuse_approved": True,
            "evidence": [{
                "evidence_id": evidence_id or self.report_id,
                "revision": "R1",
                "sha256": hashlib.sha256(quote.encode()).hexdigest(),
                "quote": quote,
            }],
        })

    def result(self, decision="create", *, candidates=None, evidence=None, reason="근거가 확인됩니다."):
        return {
            "decision": decision,
            "reason": reason,
            "candidate_ids": candidates or [],
            "evidence_ids": [self.report_id] if evidence is None else evidence,
        }

    def evaluate(self, candidates=None, report=None, lookup=None):
        return evaluate_report(
            report or self.report, [] if candidates is None else candidates,
            self.lookup if lookup is None else lookup, self.provider, model="review-only",
        )

    def assert_held_without_links(self, result):
        self.assertEqual(result["decision"], "hold")
        self.assertTrue(result["reason"])
        self.assertEqual(result["candidate_ids"], [])
        self.assertEqual(result["evidence_ids"], [])

    def test_novel_report_can_create_and_passes_complete_original_evidence(self):
        expected = self.result()
        self.provider.complete.return_value = expected
        self.assertEqual(self.evaluate(), expected)
        (system, payload), keywords = self.provider.complete.call_args
        self.assertEqual(keywords, {"model": "review-only"})
        self.assertEqual(payload["role"], "report_admission_reviewer")
        self.assertEqual(payload["report"], self.report.model_dump(mode="json"))
        self.assertEqual(payload["candidates"], [])
        self.assertEqual({item["id"] for item in payload["original"]}, set(self.lookup))
        original = next(item for item in payload["original"] if item["id"] == self.report_id)
        self.assertEqual(original, {
            "id": self.report_id,
            "text": self.report_text,
            "revision": "R1",
            "source_key": "docs:report.md",
            "locator": "section 1",
        })
        self.assertIn("never merely", system)
        self.assertIn("observation", system)

    def test_supported_decisions_preserve_candidate_and_source_references(self):
        for decision in ("new_case", "attach", "duplicate", "hold"):
            with self.subTest(decision=decision):
                candidate = (self.candidate.model_copy(update={"event_id": "EVENT-2"})
                             if decision == "new_case" else self.candidate)
                expected = self.result(
                    decision, candidates=[candidate.identity_key],
                    evidence=([self.report_id] if decision in {"new_case", "attach"}
                              else [self.report_id, self.candidate_id]),
                )
                self.provider.complete.return_value = expected
                self.assertEqual(self.evaluate([candidate]), expected)
        self.provider.complete.return_value = self.result("hold", evidence=[])
        self.assertEqual(self.evaluate(), self.result("hold", evidence=[]))

    def test_admissible_decision_cannot_add_unpinned_shown_evidence(self):
        for decision in ("create", "new_case", "attach"):
            with self.subTest(decision=decision):
                self.provider.complete.return_value = self.result(
                    decision,
                    candidates=[self.candidate.identity_key] if decision == "attach" else [],
                    evidence=[self.report_id, self.rule_id],
                )
                self.assert_held_without_links(self.evaluate([self.candidate]))

    def test_malformed_or_unlinked_model_decisions_hold(self):
        variants = [
            None,
            {"decision": "create"},
            {**self.result(), "extra": True},
            self.result("update"),
            self.result(reason=" "),
            self.result(reason="English only"),
            self.result(candidates=["unknown@1"]),
            self.result(evidence=["src-" + "d" * 24]),
            self.result(evidence=[]),
            self.result(evidence=[self.rule_id]),
            self.result(evidence=[self.report_id, self.report_id]),
            self.result("attach"),
            self.result("duplicate"),
            self.result("duplicate", candidates=[self.candidate.identity_key], evidence=[]),
            {**self.result(), "evidence_ids": self.report_id},
        ]
        for reply in variants:
            with self.subTest(reply=reply):
                self.provider.complete.return_value = reply
                self.assert_held_without_links(self.evaluate([self.candidate]))

    def test_provider_failure_holds_and_does_not_expose_details(self):
        for exception in (RuntimeError("private provider key"), TimeoutError("private host")):
            with self.subTest(exception=type(exception).__name__):
                self.provider.complete.side_effect = exception
                result = self.evaluate()
                self.assert_held_without_links(result)
                self.assertNotIn("private", result["reason"])
        self.assertEqual(self.provider.complete.call_count, 2)

    def test_different_asset_or_missing_original_does_not_call_provider(self):
        other_equipment = self.report_record(report_id="OTHER", equipment_id="EQ2")
        self.assert_held_without_links(self.evaluate([other_equipment]))
        self.assert_held_without_links(self.evaluate(lookup={}))
        self.assert_held_without_links(self.evaluate([self.candidate, self.candidate]))
        self.provider.complete.assert_not_called()

    def test_large_original_is_held_instead_of_truncated(self):
        original = self.report_text + " 검토해야 할 원문." * 8000
        lookup = {self.report_id: self.source("long-report.md", original)}
        self.assert_held_without_links(self.evaluate(lookup=lookup))
        self.provider.complete.assert_not_called()

    def test_same_procedure_in_other_event_cannot_be_a_duplicate(self):
        other_event = self.report_record(report_id="CASE-2", event_id="EVENT-2")
        self.provider.complete.return_value = self.result(
            "duplicate", candidates=[other_event.identity_key]
        )
        self.assert_held_without_links(self.evaluate([other_event]))
        self.provider.complete.return_value = self.result(
            "new_case", candidates=[other_event.identity_key]
        )
        self.assertEqual(self.evaluate([other_event])["decision"], "new_case")

    def test_known_same_event_cannot_be_classified_as_novel(self):
        other_event = self.candidate.model_copy(update={"report_id": "CASE-2", "event_id": "EVENT-2"})
        for decision in ("create", "new_case"):
            with self.subTest(decision=decision):
                self.provider.complete.return_value = self.result(decision)
                self.assert_held_without_links(self.evaluate([other_event, self.candidate]))
                self.assertEqual(self.evaluate([other_event])["decision"], decision)

    def test_new_revision_cannot_be_a_duplicate_of_its_earlier_version(self):
        previous = self.report_record()
        newer = self.report_record(version=2).model_copy(update={"supersedes_version": 1})
        self.provider.complete.return_value = self.result(
            "duplicate", candidates=[previous.identity_key]
        )
        self.assert_held_without_links(self.evaluate([previous], report=newer))
        self.provider.complete.return_value = self.result(
            "attach", candidates=[previous.identity_key]
        )
        self.assertEqual(self.evaluate([previous], report=newer)["decision"], "attach")
        system, payload = self.provider.complete.call_args.args
        self.assertEqual(payload["report"]["supersedes_version"], payload["candidates"][0]["version"])
        self.assertIn("authoritative revision replacement", system)
        self.assertIn("substantive knowledge is unchanged", system)

    def test_same_event_paraphrase_can_be_a_semantic_duplicate(self):
        candidate = self.report_record(
            report_id="CASE-paraphrase",
            evidence_id=self.candidate_id,
            quote=self.candidate_text,
        ).model_copy(update={
            "observation": "같은 점검 사건에서 공기 누출 여부를 검사했다.",
            "action": "누출 점검과 토출 압력 대조를 실시했다.",
            "result": "허용 압력 범위를 충족했다.",
        })
        self.assertEqual(candidate.event_id, self.report.event_id)
        self.assertNotEqual(candidate.case_hash, self.report.case_hash)
        expected = self.result(
            "duplicate",
            candidates=[candidate.identity_key],
            evidence=[self.report_id, self.candidate_id],
            reason="같은 사건에서 이미 확인한 점검 내용을 다르게 표현한 보고서입니다.",
        )
        self.provider.complete.return_value = expected
        self.assertEqual(self.evaluate([candidate]), expected)
        _, payload = self.provider.complete.call_args.args
        self.assertEqual(payload["candidates"][0]["event_id"], payload["report"]["event_id"])
        self.assertNotEqual(payload["candidates"][0]["action"], payload["report"]["action"])
        self.provider.complete.assert_called_once()
