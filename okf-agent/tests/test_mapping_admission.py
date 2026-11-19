"""End-to-end original mapping, shared admission and atomic publication guards."""

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from clew_okf import WikiAgent
from clew_okf.stages.step_06_publish.bundle import file_key, validate_bundle
from clew_okf.stages.step_01_ingest.raw_report_store import RawReport
from clew_okf.domain.model import Block, Source
from test_admission import AdmissionProvider, HAS_API


FIELDS = {"title": "베어링 정비 결과", "condition": "설비 정지 및 전원 차단 후",
          "observation": "진동 0.4 mm/s", "action": "베어링을 교체했다.",
          "result": "교체 후 진동 0.1 mm/s", "plant_id": "P1", "equipment_id": "EQ1", "event_id": "EVENT-01"}


class MappingProvider(AdmissionProvider):
    def __init__(self):
        super().__init__()
        self.on_mapping = None
        self.mapping_reply = None

    def complete(self, system, payload, *, model=None):
        if payload.get("role") != "report_field_mapper":
            return super().complete(system, payload, model=model)
        self.calls.append({"role": "report_field_mapper", "payload": copy.deepcopy(payload), "model": model})
        if self.on_mapping:
            callback, self.on_mapping = self.on_mapping, None
            callback()
        if self.mapping_reply is not None:
            return copy.deepcopy(self.mapping_reply)
        original = payload["original"]
        fields, supports = {}, {}
        for key in FIELDS:
            if key in payload["trusted_metadata"]:
                fields[key] = None
                continue
            found = None
            for block in original:
                for line in block["text"].splitlines():
                    if line.startswith(key + ": "):
                        found = (line[len(key) + 2:], block["id"], line)
            fields[key] = found[0] if found else None
            if found:
                supports[key] = [{"id": found[1], "quote": found[2]}]
        return {"fields": fields, "evidence": supports}


class MappingAdmissionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "manuals").mkdir()
        (self.root / "incoming").mkdir()
        (self.root / "equipment.csv").write_text("equipment_id,name,plant_id\nEQ1,공기압축기,P1\n")
        self.manual = self.root / "manuals/manual.md"
        self.manual.write_text("설비를 정지하고 전원을 차단한 후 베어링을 교체한다.\n")
        self.original = self.root / "incoming/report.md"
        self.write_original()
        self.config = {"equipment_csv": "equipment.csv", "output": "wiki",
            "documents": {"root": "manuals", "files": {"manual.md": {"equipment_ids": ["P1/EQ1"], "revision": "R1"}}},
            "synthesis": {"model": "controlled-model", "review": True},
            "admission": {"mapping": {"model": "mapping-test-model", "files": {
                "root": "incoming", "entries": [{"report_id": "RAW-1", "version": 1, "path": "report.md",
                "revision": "REPORT-R1", "status": "finalized", "reuse_approved": True}]}}}}
        self.path = self.root / "clew.yaml"
        self.write_config()
        self.provider = MappingProvider()
        provider_patch = patch("clew_okf.workflows.compiler.configure_synthesis", return_value=self.provider)
        provider_patch.start()
        self.addCleanup(provider_patch.stop)
        self.agent = WikiAgent(self.path)
        self.current = self.root / "wiki/current"
        self.agent.run()
        self.provider.calls.clear()

    def write_config(self):
        self.path.write_text(yaml.safe_dump(self.config, allow_unicode=True))

    def write_original(self, **changes):
        values = {**FIELDS, **changes}
        self.original.write_text("\n".join(f"{k}: {v}" for k, v in values.items() if v is not None) + "\n")

    def ingest(self, version=1):
        return self.agent.map_and_ingest_report("RAW-1", version, "file")

    def ledger(self):
        return json.loads((self.current.resolve().parent / "admitted_reports.json").read_text())

    def page(self):
        return self.current / "references" / f"{file_key('report:RAW-1')}.md"

    def test_file_mapping_publishes_original_provenance_and_shared_gate(self):
        result = self.ingest()
        self.assertEqual(result["status"], "published")
        self.assertEqual(result["mapping"]["report"]["result"], FIELDS["result"])
        roles = [row["role"] for row in self.provider.calls]
        self.assertIn("report_field_mapper", roles)
        self.assertIn("report_admission_reviewer", roles)
        admission_call = next(row for row in self.provider.calls if row["role"] == "report_admission_reviewer")
        self.assertEqual(admission_call["payload"]["field_evidence"], result["mapping"]["field_evidence"])
        self.assertEqual(self.provider.calls[0]["model"], "mapping-test-model")
        original_key = "raw-report:file:RAW-1@1"
        rawpage = self.current / "references" / f"{file_key(original_key)}.md"
        self.assertTrue(rawpage.is_file())
        self.assertIn(FIELDS["action"], rawpage.read_text())
        page = self.page().read_text()
        self.assertIn("공통 필드의 원문 연결", page)
        self.assertIn(file_key(original_key), page)
        self.assertIn("field_evidence", self.ledger()[0]["mapping"])
        self.assertEqual(len(self.ledger()), 1)
        self.assertGreater(validate_bundle(self.current), 0)
        # Neither a raw report nor its generated case is a normative author input.
        for row in self.provider.calls:
            if row["role"] == "author":
                self.assertFalse(any("진동 0.4" in item["text"] for item in row["payload"]["evidence"]))

    def test_field_spans_are_rechecked_before_semantic_admission(self):
        from clew_okf.stages.step_03_admission.admission_review import evaluate_report
        from clew_okf.stages.step_06_publish.bundle import evidence
        from clew_okf.stages.step_02_mapping.mapping import map_report
        from clew_okf.stages.step_01_ingest.raw_report_store import load_raw_report
        from clew_okf.domain.reports import ReportRecord
        raw = load_raw_report(self.config["admission"]["mapping"], None,
                              self.root, self.root / "wiki", "RAW-1", 1, "file")
        from clew_okf.domain.model import Equipment
        equipment = [Equipment("EQ1", {"plant_id": "P1"}, "file:///equipment.csv", "row 1")]
        mapped = map_report(raw, equipment, self.provider)
        report = ReportRecord.model_validate(mapped["report"])
        lookup = {identifier: (raw.source, block) for identifier, block in evidence(raw.source)}
        for mutation in ("forged_quote", "unquoted_value", "unknown_id", "missing_field"):
            spans = copy.deepcopy(mapped["field_evidence"])
            if mutation == "forged_quote":
                spans["result"][0]["quote"] = "원문에 없는 결과"
            elif mutation == "unquoted_value":
                spans["result"][0]["quote"] = spans["title"][0]["quote"]
            elif mutation == "unknown_id":
                spans["result"][0]["evidence_id"] = "src-" + "0" * 24
            else:
                del spans["result"]
            self.provider.calls.clear()
            result = evaluate_report(report, [], lookup, self.provider, field_evidence=spans)
            self.assertEqual(result["decision"], "hold")
            self.assertEqual(self.provider.calls, [])

    def test_missing_and_placeholder_fields_hold_without_admission_or_wiki_change(self):
        for missing in (None, "미확인", "NULL"):
            with self.subTest(missing=missing):
                self.write_original(result=missing)
                before = self.current.resolve()
                self.provider.calls.clear()
                result = self.ingest()
                self.assertEqual(result["status"], "hold")
                self.assertEqual(self.current.resolve(), before)
                self.assertEqual([x["role"] for x in self.provider.calls], ["report_field_mapper"])
                self.assertFalse(self.page().exists())

    def test_missing_event_is_not_inferred_from_other_identifiers(self):
        self.write_original(event_id=None)
        result = self.ingest()
        self.assertEqual(result["status"], "hold")
        self.assertTrue(any(x.get("field") == "event_id" for x in result["findings"]))

    def test_wrong_asset_mapping_holds_before_admission(self):
        self.write_original(equipment_id="EQ-NOT-REGISTERED")
        result = self.ingest()
        self.assertEqual(result["status"], "hold")
        self.assertEqual([x["role"] for x in self.provider.calls], ["report_field_mapper"])

    def test_shared_admission_hold_never_publishes_raw_or_case(self):
        self.provider.decision = "hold"
        before = self.current.resolve()
        result = self.ingest()
        self.assertEqual(result["mapping"]["status"], "mapped")
        self.assertEqual(result["status"], "hold")
        self.assertEqual(self.current.resolve(), before)
        self.assertFalse(self.page().exists())
        self.assertFalse(any("raw-report" in p.name for p in self.current.rglob("*.md")))

    def test_accepted_retry_and_regular_build_do_not_remap(self):
        self.ingest()
        before = self.current.resolve()
        ledger = self.ledger()
        self.provider.calls.clear()
        self.assertEqual(self.ingest()["status"], "unchanged")
        self.assertEqual(self.agent.run()["state"], "unchanged")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.current.resolve(), before)
        self.assertEqual(self.ledger(), ledger)

    def test_modified_same_version_original_conflicts_and_full_build_fails(self):
        self.ingest()
        before = self.current.resolve()
        self.write_original(result="기존 버전의 결과가 변경됨.")
        self.provider.calls.clear()
        self.assertEqual(self.ingest()["status"], "conflict")
        with self.assertRaises(ValueError):
            self.agent.run()
        self.assertEqual(self.current.resolve(), before)
        self.assertEqual(self.provider.calls, [])

    def test_approval_revocation_holds_and_blocks_refresh(self):
        self.ingest()
        before = self.current.resolve()
        self.config["admission"]["mapping"]["files"]["entries"][0]["reuse_approved"] = False
        self.write_config()
        self.provider.calls.clear()
        self.assertEqual(self.ingest()["status"], "hold")
        from clew_okf.domain.errors import RevalidationRequiredError
        with self.assertRaises(RevalidationRequiredError):
            self.agent.run()
        self.assertEqual(self.current.resolve(), before)
        self.assertEqual(self.provider.calls, [])

    def test_manual_change_blocks_refresh_until_explicit_report_revision(self):
        self.ingest()
        before = self.current.resolve()
        self.manual.write_text("개정: 설비 정지·전원 차단 후 베어링 교체, 재가동 전에 진동 확인.\n")
        self.provider.calls.clear()
        with self.assertRaises(ValueError):
            self.agent.run()
        self.assertEqual(self.current.resolve(), before)
        entry = self.config["admission"]["mapping"]["files"]["entries"][0]
        entry2 = {**entry, "version": 2, "revision": "REPORT-R2", "metadata": {"supersedes_version": 1}}
        self.config["admission"]["mapping"]["files"]["entries"].append(entry2)
        self.write_config()
        self.provider.decision = "attach"
        result = self.ingest(2)
        self.assertEqual((result["status"], result["action"]), ("published", "update"))
        self.assertEqual(len(self.ledger()), 2)
        self.assertIn("revision: v2", self.page().read_text())
        self.assertEqual(self.agent.run()["state"], "unchanged")

    def test_changes_during_mapping_or_final_author_cannot_publish(self):
        for stage in ("on_mapping", "on_author", "on_admission"):
            with self.subTest(stage=stage):
                self.write_original()
                self.provider.calls.clear()
                before = self.current.resolve()
                setattr(self.provider, stage, lambda: self.write_original(result="검토 중 결과 변경"))
                result = self.ingest()
                self.assertEqual(result["status"], "conflict")
                self.assertEqual(self.current.resolve(), before)
                self.assertFalse(self.page().exists())

    def test_unapproved_source_never_calls_mapping(self):
        self.config["admission"]["mapping"]["files"]["entries"][0]["reuse_approved"] = False
        self.write_config()
        before = self.current.resolve()
        self.assertEqual(self.ingest()["status"], "hold")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.current.resolve(), before)

    def test_raw_files_and_records_cannot_bypass_gate_through_direct_collection(self):
        self.config["documents"]["root"] = "incoming"
        self.config["documents"]["files"] = {"report.md": {"equipment_ids": ["P1/EQ1"], "revision": "R1"}}
        self.write_config()
        before = self.current.resolve()
        with self.assertRaises(ValueError):
            self.agent.run()
        self.assertEqual(self.current.resolve(), before)

    def test_raw_database_cannot_be_directly_collected_without_admission(self):
        self.config["admission"]["mapping"]["database"] = {"table": "public.raw_reports"}
        self.config["postgres"] = {"records": [{"table": "public.raw_reports"}]}
        self.write_config()
        before = self.current.resolve()
        with self.assertRaisesRegex(ValueError, "DB 보고서"):
            self.agent.run()
        self.assertEqual(self.current.resolve(), before)
        self.assertEqual(self.provider.calls, [])

    @unittest.skipUnless(HAS_API, "FastAPI optional dependency is not installed")
    def test_fastapi_maps_registered_input_and_rejects_client_facts_paths_and_approvals(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from clew_okf.api import create_router
        app = FastAPI()
        app.include_router(create_router(self.agent))
        with TestClient(app) as client:
            endpoint = "/wiki/reports/RAW-1/map-and-ingest"
            for payload in ({"version": "1", "source": "file"}, {"version": True, "source": "file"},
                            {"version": 1, "source": "upload"}, {"version": 1, "source": "file", "path": "/etc/passwd"},
                            {"version": 1, "source": "file", "reuse_approved": True}):
                self.assertEqual(client.post(endpoint, json=payload).status_code, 422)
            self.assertEqual(self.provider.calls, [])
            response = client.post(endpoint, json={"version": 1, "source": "file"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "published")

    def test_database_original_uses_same_mapping_gate_and_refresh(self):
        # Actual SQL projection/read-only behavior is covered in loader tests.
        # This integration test isolates routing/provenance/publication from DB availability.
        source = Source("raw-report:database:DB-1@1", "DB-1", "report_input", "postgresql://fixture/reports",
                        (Block("column body", self.original.read_text()),), revision="DB-R1", synthesize=False)
        raw = RawReport("DB-1", 1, source, {"report_id": "DB-1", "version": 1,
                        "status": "finalized", "reuse_approved": True}, "a" * 64,
                        {"kind": "database", "input_digest": "a" * 64})
        with patch("clew_okf.stages.step_01_ingest.raw_report_store.load_raw_report", return_value=raw):
            result = self.agent.map_and_ingest_report("DB-1", 1, "database")
            self.assertEqual(result["status"], "published")
            self.assertEqual(self.ledger()[0]["mapping"]["kind"], "database")
            self.provider.calls.clear()
            self.assertEqual(self.agent.run()["state"], "unchanged")
            self.assertEqual(self.provider.calls, [])


if __name__ == "__main__":
    unittest.main()
