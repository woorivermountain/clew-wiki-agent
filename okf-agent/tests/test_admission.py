"""Exercise the public admission workflow with native sources and controlled models."""

import copy
import hashlib
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from clew_okf import WikiAgent
from clew_okf.stages.step_06_publish.bundle import evidence, file_key, validate_bundle
from clew_okf.stages.step_01_ingest.documents import load_documents
from clew_okf.domain.reports import ReportRecord

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from clew_okf.api import create_router
except ImportError:
    HAS_API = False
else:
    HAS_API = True


class AdmissionProvider:
    """Use actual request roles and source substrings, with no network or DB."""

    def __init__(self):
        self.calls = []
        self.decision = "create"
        self.reply = None
        self.on_admission = None
        self.on_author = None
        self.fail_author = False

    def complete(self, system, payload, *, model=None):
        role = payload.get("role", "author")
        self.calls.append({"role": role, "payload": copy.deepcopy(payload), "model": model})
        if role == "report_admission_reviewer":
            if self.on_admission is not None:
                callback, self.on_admission = self.on_admission, None
                callback()
            if self.reply is not None:
                return copy.deepcopy(self.reply)
            candidates = payload["candidates"]
            related = []
            if self.decision in {"attach", "new_case", "duplicate"} and candidates:
                candidate = candidates[0]
                related = [f"{candidate['report_id']}@{candidate['version']}"]
            return {
                "decision": self.decision,
                "reason": "원문 근거와 사건 식별자를 대조한 테스트 판정입니다.",
                "candidate_ids": related,
                "evidence_ids": [item["evidence_id"] for item in payload["report"]["evidence"]],
            }
        if role == "independent_reviewer":
            return {"issues": []}
        if self.on_author is not None:
            callback, self.on_author = self.on_author, None
            callback()
        if self.fail_author:
            raise RuntimeError("controlled author failure")
        if "Allowed sections: " not in system:
            raise AssertionError("Unexpected model request")
        sections = system.split("Allowed sections: ", 1)[1].split(". ", 1)[0].split(", ")
        section = "점검 절차" if "점검 절차" in sections else sections[0]
        return {"claims": [
            {"id": item["id"], "section": section, "quote": item["text"][:600]}
            for item in payload["evidence"][:4]
        ]}


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.originals = self.root / "originals"
        self.originals.mkdir()
        self.manual = self.originals / "manual.md"
        self.manual.write_text(
            "설비를 정지하고 전원을 차단한다. 베어링을 교체한 뒤 진동을 측정한다. "
            "정비 기록: 진동 0.4 mm/s, 교체 후 0.1 mm/s.\n", encoding="utf-8",
        )
        (self.root / "equipment.csv").write_text(
            "equipment_id,name,plant_id\nEQ1,공기압축기,P1\nEQ2,보조 압축기,P1\n", encoding="utf-8",
        )
        self.output = self.root / "published"
        self.current = self.output / "current"
        self.config_path = self.root / "clew.yaml"
        self.config = {
            "equipment_csv": "equipment.csv", "output": "published",
            "documents": {
                "root": "originals",
                "files": {"manual.md": {"equipment_ids": ["P1/EQ1"], "revision": "R1"}},
            },
            "synthesis": {"model": "controlled-test-model", "review": True},
            "admission": {"reports": {"table": "public.approved_reports"}},
        }
        self.write_config()
        sources, _ = load_documents(self.config["documents"], self.root, output=self.output)
        identifier, block = evidence(sources[0])[0]
        self.reference = {
            "evidence_id": identifier, "revision": "R1",
            "sha256": hashlib.sha256(block.text.encode("utf-8")).hexdigest(),
            "quote": "설비를 정지하고 전원을 차단한다.",
        }
        self.payload = {
            "report_id": "R-001", "version": 1, "plant_id": "P1", "equipment_id": "EQ1",
            "event_id": "EVENT-01", "title": "베어링 정비 결과", "condition": "설비 정지 및 전원 차단 후",
            "observation": "진동 0.4 mm/s", "action": "전원을 차단하고 베어링을 교체했다.",
            "result": "교체 후 진동 0.1 mm/s", "status": "finalized", "reuse_approved": True,
            "evidence": [self.reference],
        }
        self.records = {}
        self.store(self.record())
        self.provider = AdmissionProvider()
        provider_patch = patch("clew_okf.workflows.compiler.configure_synthesis", return_value=self.provider)
        provider_patch.start()
        self.addCleanup(provider_patch.stop)
        loader_patch = patch("clew_okf.stages.step_01_ingest.report_store.load_report", side_effect=self.load_report)
        self.loader = loader_patch.start()
        self.addCleanup(loader_patch.stop)
        self.agent = WikiAgent(self.config_path)

    def write_config(self):
        self.config_path.write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")

    def record(self, **changes):
        return ReportRecord.model_validate({**self.payload, **changes})

    def store(self, report):
        self.records[(report.report_id, report.version)] = report
        return report

    def load_report(self, admission, postgres, report_id, version):
        value = self.records[(report_id, version)]
        return value if isinstance(value, ReportRecord) else ReportRecord.model_validate(value)

    def ingest(self, report=None):
        report = report or self.records[("R-001", 1)]
        self.store(report)
        return self.agent.ingest_report(report.report_id, report.version)

    def ledger(self):
        return json.loads((self.current.resolve().parent / "admitted_reports.json").read_text(encoding="utf-8"))

    def inventory(self):
        return json.loads((self.current.resolve().parent / "sources.json").read_text(encoding="utf-8"))

    def report_page(self, report_id="R-001"):
        return self.current / "references" / f"{file_key('report:' + report_id)}.md"

    def baseline(self):
        self.agent.run()
        self.provider.calls.clear()
        return self.current.resolve()

    def test_publish_case_source_and_regular_run_preserve_ledger(self):
        result = self.ingest()
        self.assertEqual((result["status"], result["action"]), ("published", "create"))
        self.assertEqual(result["wiki_path"], self.report_page().relative_to(self.current).as_posix())
        page = self.report_page().read_text(encoding="utf-8")
        self.assertIn("type: Maintenance Case", page)
        self.assertIn("source_key: report:R-001", page)
        self.assertIn("사건 EVENT-01", page)
        self.assertIn("적용 설비", page)
        self.assertIn(self.reference["sha256"], page)
        self.assertEqual({row["key"] for row in self.inventory()}, {"docs:manual.md", "report:R-001"})
        self.assertGreater(validate_bundle(self.current), 0)
        original_ledger = self.ledger()
        self.assertEqual(original_ledger[0]["report"], self.record().model_dump(mode="json"))
        roles = [call["role"] for call in self.provider.calls]
        self.assertIn("report_admission_reviewer", roles)
        self.assertIn("author", roles)
        self.assertIn("independent_reviewer", roles)

        self.provider.calls.clear()
        self.assertEqual(self.agent.run()["state"], "unchanged")
        self.assertEqual(self.provider.calls, [])
        old_pointer = self.current.resolve()
        self.config["title"] = "운영용 제조 위키"
        self.write_config()
        self.assertEqual(self.agent.run()["state"], "published")
        self.assertNotEqual(self.current.resolve(), old_pointer)
        self.assertEqual(self.ledger(), original_ledger)
        self.assertTrue(self.report_page().is_file())
        self.assertIn("report:R-001", {row["key"] for row in self.inventory()})

    def test_retry_is_idempotent_without_new_model_or_pointer(self):
        first = self.ingest()
        pointer = self.current.resolve()
        ledger = self.ledger()
        self.provider.calls.clear()
        again = self.ingest()
        self.assertEqual((again["status"], again["action"]), ("unchanged", "no_change"))
        self.assertEqual(first["build"]["snapshot"], again["build"]["snapshot"])
        self.assertEqual(self.current.resolve(), pointer)
        self.assertEqual(self.ledger(), ledger)
        self.assertEqual(self.provider.calls, [])

    def test_same_identity_with_changed_content_conflicts_before_model(self):
        self.ingest()
        pointer = self.current.resolve()
        self.provider.calls.clear()
        result = self.ingest(self.record(result="다른 결과"))
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["findings"][0]["code"], "identity_content_conflict")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.current.resolve(), pointer)
        self.assertEqual(len(self.ledger()), 1)

    def test_same_event_exact_case_is_duplicate_under_different_report_id(self):
        self.ingest()
        pointer = self.current.resolve()
        self.provider.calls.clear()
        result = self.ingest(self.record(report_id="R-002", title="동일 사건 보고서"))
        self.assertEqual((result["status"], result["action"]), ("duplicate", "no_change"))
        self.assertEqual(result["review"]["candidate_ids"], ["R-001@1"])
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.current.resolve(), pointer)
        self.assertFalse(self.report_page("R-002").exists())

    def test_different_event_same_facts_is_preserved_as_new_case(self):
        self.ingest()
        second = self.record(report_id="R-002", event_id="EVENT-02")
        self.assertEqual(second.knowledge_hash, self.record().knowledge_hash)
        self.assertNotEqual(second.case_hash, self.record().case_hash)
        self.provider.decision = "new_case"
        result = self.ingest(second)
        self.assertEqual((result["status"], result["action"]), ("published", "new_case"))
        self.assertEqual({row["report"]["event_id"] for row in self.ledger()}, {"EVENT-01", "EVENT-02"})
        self.assertTrue(self.report_page("R-001").is_file())
        self.assertTrue(self.report_page("R-002").is_file())
        self.assertIn("사건 EVENT-02", self.report_page("R-002").read_text(encoding="utf-8"))

    def test_attach_preserves_new_report_and_links_existing_case(self):
        self.ingest()
        second = self.record(report_id="R-002", result="교체 후 진동은 0.1 mm/s였다.")
        self.provider.decision = "attach"
        result = self.ingest(second)
        self.assertEqual((result["status"], result["action"]), ("published", "attach"))
        page = self.report_page("R-002").read_text(encoding="utf-8")
        self.assertIn("관련 사례", page)
        self.assertIn(self.report_page("R-001").name, page)
        self.assertEqual(len(self.ledger()), 2)
        self.assertGreater(validate_bundle(self.current), 0)

    def test_explicit_revision_updates_same_page_and_retains_version_history(self):
        self.ingest()
        original_pointer = self.current.resolve()
        relative = self.report_page().relative_to(self.current)
        second = self.record(version=2, supersedes_version=1, result="교체 후 진동은 0.1 mm/s였다.")
        self.provider.decision = "attach"
        result = self.ingest(second)
        self.assertEqual((result["status"], result["action"]), ("published", "update"))
        self.assertEqual(result["wiki_path"], relative.as_posix())
        self.assertIn("버전 2", self.report_page().read_text(encoding="utf-8"))
        self.assertIn("버전 1", (original_pointer / relative).read_text(encoding="utf-8"))
        self.assertEqual([row["report"]["version"] for row in self.ledger()], [1, 2])
        self.assertEqual([row["key"] for row in self.inventory()].count("report:R-001"), 1)
        pointer = self.current.resolve()
        self.provider.calls.clear()
        missing_lineage = self.ingest(self.record(version=3, result="재확인 완료"))
        self.assertEqual(missing_lineage["status"], "conflict")
        self.assertEqual(missing_lineage["findings"][0]["code"], "revision_conflict")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.current.resolve(), pointer)

    def test_strict_link_failures_reject_before_model_and_preserve_pointer(self):
        pointer = self.baseline()
        variants = [
            (self.record(equipment_id="EQ2"), "evidence_wrong_equipment"),
            (self.record(evidence=[{**self.reference, "sha256": "0" * 64}]), "evidence_hash_mismatch"),
            (self.record(evidence=[{**self.reference, "revision": "R0"}]), "revision_mismatch"),
        ]
        for report, code in variants:
            with self.subTest(code=code):
                result = self.ingest(report)
                self.assertEqual(result["status"], "rejected")
                self.assertIn(code, {finding["code"] for finding in result["findings"]})
                self.assertEqual(self.provider.calls, [])
                self.assertEqual(self.current.resolve(), pointer)
                self.assertEqual(self.ledger(), [])
                self.assertFalse(self.report_page().exists())

    def test_semantic_hold_and_malformed_review_never_admit_a_source(self):
        pointer = self.baseline()
        replies = [
            {"decision": "hold", "reason": "필수 조건을 확인할 수 없어 보류합니다.", "candidate_ids": [], "evidence_ids": []},
            {"decision": "create"},
            {"decision": "create", "reason": "연결이 잘못된 응답입니다.", "candidate_ids": [], "evidence_ids": ["src-" + "f" * 24]},
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                self.provider.calls.clear()
                self.provider.reply = reply
                result = self.ingest()
                self.assertEqual(result["status"], "hold")
                self.assertEqual(result["review"]["decision"], "hold")
                self.assertEqual([call["role"] for call in self.provider.calls], ["report_admission_reviewer"])
                self.assertEqual(self.current.resolve(), pointer)
                self.assertEqual(self.ledger(), [])
                self.assertFalse(self.report_page().exists())

    def test_source_change_during_review_prevents_publication(self):
        pointer = self.baseline()
        self.provider.on_admission = lambda: self.manual.write_text(
            self.manual.read_text(encoding="utf-8") + "추가 안전 조건을 확인한다.\n", encoding="utf-8",
        )
        result = self.ingest()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["findings"][0]["code"], "source_changed")
        self.assertEqual(sum(call["role"] == "report_admission_reviewer" for call in self.provider.calls), 1)
        self.assertEqual(self.current.resolve(), pointer)
        self.assertEqual(self.ledger(), [])

    def test_report_change_during_review_prevents_publication(self):
        pointer = self.baseline()
        self.provider.on_admission = lambda: self.store(self.record(result="검토 중 원본이 수정됨"))
        result = self.ingest()
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["findings"][0]["code"], "source_changed")
        self.assertEqual(sum(call["role"] == "report_admission_reviewer" for call in self.provider.calls), 1)
        self.assertEqual(self.current.resolve(), pointer)
        self.assertEqual(self.ledger(), [])

    def test_changes_during_final_author_build_prevent_publication(self):
        pointer = self.baseline()
        original_text = self.manual.read_text(encoding="utf-8")
        callbacks = {
            "source": lambda: self.manual.write_text(original_text + "최종 작성 중 변경됨.\n", encoding="utf-8"),
            "report": lambda: self.store(self.record(result="최종 작성 중 보고서 변경됨")),
        }
        for kind, callback in callbacks.items():
            with self.subTest(kind=kind):
                self.provider.calls.clear()
                self.provider.on_author = callback
                result = self.ingest(self.record())
                self.assertEqual(result["status"], "conflict")
                self.assertEqual(result["findings"][0]["code"], "source_changed")
                self.assertIn("author", [call["role"] for call in self.provider.calls])
                self.assertEqual(self.current.resolve(), pointer)
                self.assertEqual(self.ledger(), [])
                self.assertFalse(self.report_page().exists())
                self.manual.write_text(original_text, encoding="utf-8")
                self.store(self.record())

    def test_changed_admitted_evidence_blocks_retry_and_full_build(self):
        self.ingest()
        pointer = self.current.resolve()
        ledger = self.ledger()
        self.manual.write_text(self.manual.read_text(encoding="utf-8").replace("0.4 mm/s", "0.5 mm/s"), encoding="utf-8")
        self.provider.calls.clear()
        retry = self.ingest()
        self.assertEqual(retry["status"], "rejected")
        self.assertIn("evidence_hash_mismatch", {finding["code"] for finding in retry["findings"]})
        with self.assertRaises(ValueError):
            self.agent.run()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.current.resolve(), pointer)
        self.assertEqual(self.ledger(), ledger)

    def test_changed_or_unapproved_canonical_report_blocks_full_build(self):
        self.ingest()
        pointer = self.current.resolve()
        ledger = self.ledger()
        original = self.record()
        changed = self.record(result="같은 버전의 정본 내용이 변경됨")
        unapproved = {**self.payload, "reuse_approved": False}
        for kind, value in (("content_changed", changed), ("approval_revoked", unapproved)):
            with self.subTest(kind=kind):
                self.records[("R-001", 1)] = value
                self.provider.calls.clear()
                with self.assertRaises((ValueError, RuntimeError)):
                    self.agent.run()
                self.assertEqual(self.provider.calls, [])
                self.assertEqual(self.current.resolve(), pointer)
                self.assertEqual(self.ledger(), ledger)
                self.assertTrue(self.report_page().is_file())
                self.records[("R-001", 1)] = original

    def test_canonical_change_during_regular_refresh_preserves_previous(self):
        self.ingest()
        pointer = self.current.resolve()
        ledger = self.ledger()
        self.config["title"] = "재작성 검증용 제목"
        self.write_config()
        self.provider.calls.clear()
        self.provider.on_author = lambda: self.store(self.record(result="재작성 중 정본 변경됨"))
        with self.assertRaises((ValueError, RuntimeError)):
            self.agent.run()
        self.assertIn("author", [call["role"] for call in self.provider.calls])
        self.assertEqual(self.current.resolve(), pointer)
        self.assertEqual(self.ledger(), ledger)

    def test_failed_staged_build_preserves_current_and_ledger(self):
        pointer = self.baseline()
        snapshots = set((self.output / "snapshots").iterdir())
        self.provider.fail_author = True
        with self.assertRaisesRegex(RuntimeError, "controlled author failure"):
            self.ingest()
        self.assertEqual(self.current.resolve(), pointer)
        self.assertEqual(self.ledger(), [])
        self.assertFalse(self.report_page().exists())
        self.assertEqual(set((self.output / "snapshots").iterdir()), snapshots)

    @unittest.skipUnless(HAS_API, "Install the optional API and test dependencies")
    def test_api_accepts_only_version_and_loads_server_owned_report(self):
        pointer = self.baseline()
        app = FastAPI()
        app.include_router(create_router(self.agent))
        with TestClient(app) as client:
            for body in (
                {"version": 1, "report": self.payload},
                {"version": 1, "condition": "client supplied"},
                {"version": True}, {"version": "1"}, {"version": 1.0},
                {"version": 0}, {"version": -1}, {},
            ):
                with self.subTest(body=body):
                    response = client.post("/wiki/reports/R-001/ingest", json=body)
                    self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.provider.calls, [])
            self.loader.assert_not_called()
            self.assertEqual(self.current.resolve(), pointer)
            accepted = client.post("/wiki/reports/R-001/ingest", json={"version": 1})
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(accepted.json()["status"], "published")
            self.assertEqual(accepted.json()["version"], 1)
        self.assertIn("보고서 R-001 / 버전 1", self.report_page().read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
