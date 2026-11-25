"""Service contracts: immediate enqueue, real engine integration and durable jobs."""

import copy
import io
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from clew_okf import WikiAgent
from clew_okf.async_api import create_service_router, install_wiki_service
from clew_okf.stages.step_06_publish.bundle import file_key, validate_bundle
from clew_okf.runtime.jobs import JobStore
from clew_okf.domain.model import Block, Source
from clew_okf.service import WikiService
from clew_okf.workflows.release import approve_release, inspect_release_candidate
from clew_okf.runtime.worker import WikiWorker
from test_mapping_admission import FIELDS, MappingProvider


class AsyncServiceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "manuals").mkdir()
        (self.root / "equipment.csv").write_text("equipment_id,name,plant_id\nEQ1,공기압축기,P1\n")
        self.manual = self.root / "manuals/manual.md"
        self.manual.write_text("전원을 차단하고 베어링을 교체한다.\n")
        self.config = {"equipment_csv": "equipment.csv", "output": "wiki",
            "documents": {"root": "manuals", "files": {"manual.md": {"equipment_ids": ["P1/EQ1"], "revision": "R1"}}},
            "synthesis": {"model": "controlled-model", "review": True},
            "service": {"state_dir": "service-state", "bootstrap_on_startup": False, "lease_seconds": 5}}
        self.path = self.root / "clew.yaml"
        self.write_config()
        self.provider = MappingProvider()
        provider_patch = patch("clew_okf.workflows.compiler.configure_synthesis", return_value=self.provider)
        provider_patch.start();self.addCleanup(provider_patch.stop)
        self.service = WikiService(self.path)
        self.worker = WikiWorker(self.service)
        self.current = self.root / "wiki/current"
        self.service.agent().run()
        self.provider.calls.clear()
        self.client = self.make_client()

    def write_config(self):
        self.path.write_text(yaml.safe_dump(self.config, allow_unicode=True))

    def make_client(self, resolver=None):
        app = FastAPI()
        install_wiki_service(app, self.service, approval_resolver=resolver)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def body(self, **changes):
        return "\n".join(f"{key}: {value}" for key,value in {**FIELDS,**changes}.items() if value is not None) + "\n"

    def request(self, **changes):
        return {"version": 1, "revision": "R1", "body": self.body(), **changes}

    def page(self, rid="S-1"):
        return self.current / "references" / f"{file_key('report:' + rid)}.md"

    def test_release_inherits_router_dependencies(self):
        def authorize(x_access: str = Header(default="")):
            if x_access != "trusted":
                raise HTTPException(403)

        app = FastAPI()
        app.include_router(create_service_router(self.service, dependencies=[Depends(authorize)]))
        headers = {"x-access": "trusted"}
        with TestClient(app) as client:
            draft_only = client.get("/wiki/release", headers=headers)
            self.assertEqual(draft_only.status_code, 404, draft_only.text)
            candidate = inspect_release_candidate(self.path)
            release = approve_release(self.path, snapshot=candidate["snapshot"],
                bundle_sha256=candidate["bundle_sha256"], reviewer="supervisor")
            self.assertEqual(client.get("/wiki/release").status_code, 403)
            approved = client.get("/wiki/release", headers=headers)
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.headers["cache-control"], "no-store")
        self.assertEqual(approved.json(), release)

    def test_save_returns_queued_before_model_and_default_approval_holds(self):
        old = self.current.resolve()
        response = self.client.post("/wiki/reports/S-1", json=self.request())
        self.assertEqual(response.status_code, 202)
        job = response.json()
        self.assertEqual(job["status"], "queued")
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.current.resolve(), old)
        self.assertFalse(self.page().exists())
        result = self.worker.run_once()
        self.assertEqual(result["status"], "held")
        status = self.client.get(job["status_url"]).json()
        self.assertEqual(status["hold_reasons"][0]["code"], "awaiting_approval")
        self.assertEqual(self.provider.calls, [])

    def test_trusted_approval_event_queues_and_real_engine_publishes(self):
        first = self.client.post("/wiki/reports/S-1", json=self.request()).json()
        self.worker.run_once()
        approved = self.service.set_report_approval("S-1", 1, status="finalized", reuse_approved=True)
        self.assertNotEqual(approved["job"]["id"], first["job_id"])
        latest = self.client.get("/wiki/reports/S-1/status", params={"version":1})
        self.assertEqual(latest.status_code, 200)
        self.assertEqual(latest.json()["id"], approved["job"]["id"])
        result = self.worker.run_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["status"], "published")
        self.assertTrue(self.page().exists())
        self.assertEqual(result["progress"]["report_id"], "S-1")
        self.assertEqual(result["progress"]["source"], "managed_text")
        self.assertGreater(validate_bundle(self.current), 0)
        roles = [item["role"] for item in self.provider.calls]
        self.assertIn("report_field_mapper", roles)
        self.assertIn("report_admission_reviewer", roles)
        events = self.client.get(f"/wiki/jobs/{result['id']}/events").json()["events"]
        self.assertIn("mapping_fields", [item["stage"] for item in events])
        self.assertIn("publishing_wiki", [item["stage"] for item in events])
        self.assertNotIn("lease_token", self.client.get(f"/wiki/jobs/{result['id']}").json())
        self.provider.calls.clear()
        same = self.client.post("/wiki/reports/S-1", json=self.request()).json()
        self.assertEqual(same["job_id"], result["id"])
        self.assertEqual(self.provider.calls, [])

    def test_missing_result_is_held_with_field_reason_and_no_publication(self):
        client = self.make_client(lambda _id,_version,submission: {"status": "finalized", "reuse_approved": True, "approved_fingerprint": submission["fingerprint"]})
        old = self.current.resolve()
        job = client.post("/wiki/reports/S-1", json=self.request(body=self.body(result=None))).json()
        result = self.worker.run_once()
        self.assertEqual(result["status"], "held")
        status = client.get(job["status_url"]).json()
        self.assertTrue(any(item.get("field") == "result" for item in status["hold_reasons"]))
        self.assertFalse(self.page().exists())
        self.assertEqual(old, self.current.resolve())

    def test_required_evidence_omission_holds_build_and_mapped_report(self):
        old = self.current.resolve()
        quote = self.manual.read_text(encoding="utf-8").strip()
        self.config["synthesis"]["structured_evidence"] = [{
            "source_key": "docs:manual.md",
            "protected_locators": ["line 1"],
            "groups": [{
                "id": "maintenance", "label": "베어링 교체 조건", "required": True,
                "equipment_ids": ["P1/EQ1"],
                "spans": [{"locator": "line 1", "quote": quote}],
            }],
        }]
        self.write_config()

        complete = self.provider.complete

        def omit_required_group(system, payload, *, model=None):
            if "evidence" in payload:
                return {"claims": []}
            return complete(system, payload, model=model)

        client = self.make_client(lambda _id, _version, submission: {
            "status": "finalized", "reuse_approved": True, "approved_fingerprint": submission["fingerprint"],
        })
        with patch.object(self.provider, "complete", side_effect=omit_required_group):
            for endpoint, body in (("/wiki/build", {}), ("/wiki/reports/S-1", self.request())):
                with self.subTest(endpoint=endpoint):
                    enqueued = client.post(endpoint, json=body)
                    self.assertEqual(enqueued.status_code, 202, enqueued.text)
                    self.worker.run_once()
                    status = client.get(enqueued.json()["status_url"]).json()
                    self.assertEqual(status["status"], "held", status)
                    self.assertEqual(status["result"]["status"], "hold")
                    finding, = status["hold_reasons"]
                    self.assertEqual(finding["code"], "required_evidence_missing")
                    self.assertEqual(finding["label"], "베어링 교체 조건")
                    evidence, = finding["evidence"]
                    self.assertEqual((evidence["source_key"], evidence["locator"], evidence["quote"]),
                                     ("docs:manual.md", "line 1", quote))
                    self.assertEqual(self.current.resolve(), old)
                    self.assertFalse(self.page().exists())

    def test_inflight_model_does_not_block_status_or_another_save(self):
        entered, released = threading.Event(), threading.Event()
        self.addCleanup(released.set)
        self.provider.on_mapping = lambda: (entered.set(), released.wait(3))
        client = self.make_client(lambda _id,_version,submission: {"status": "finalized", "reuse_approved": True, "approved_fingerprint": submission["fingerprint"]})
        accepted = client.post("/wiki/reports/S-1", json=self.request()).json()
        thread = threading.Thread(target=self.worker.run_once)
        thread.start();self.addCleanup(lambda: thread.join(timeout=3))
        try:
            self.assertTrue(entered.wait(2))
            status = client.get(accepted["status_url"]).json()
            self.assertEqual(status["status"], "running")
            self.assertEqual(status["stage"], "mapping_fields")
            second = client.post("/wiki/reports/S-2", json=self.request(body=self.body(event_id="EVENT-02")))
            self.assertEqual(second.status_code, 202)
            self.assertEqual(second.json()["status"], "queued")
        finally:
            released.set();thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(client.get(accepted["status_url"]).json()["status"], "succeeded")

    def test_restart_process_state_keeps_queued_report_and_can_finish(self):
        accepted = self.service.save_report("S-1", 1, body=self.body(), revision="R1", status="finalized", reuse_approved=True)
        restarted = WikiService(self.path)
        self.assertEqual(restarted.jobs.get(accepted["job"]["id"])["status"], "queued")
        result = WikiWorker(restarted).run_once()
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(self.page().exists())

    def test_build_active_dedupes_but_completed_build_can_run_after_data_change(self):
        one = self.service.enqueue_build()
        self.assertEqual(one["id"], self.service.enqueue_build()["id"])
        self.assertEqual(self.worker.run_once()["status"], "succeeded")
        before = self.current.resolve()
        self.manual.write_text("전원을 차단하고 베어링을 교체한다. 교체 후 진동을 확인한다.\n")
        two = self.service.enqueue_build()
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(self.worker.run_once()["status"], "succeeded")
        self.assertNotEqual(before, self.current.resolve())

    def test_queued_configuration_change_holds_and_explicit_retry_uses_new_config(self):
        one = self.service.enqueue_build()
        old = self.current.resolve()
        self.config["title"] = "운영 설정 변경"
        self.write_config()
        result = self.worker.run_once()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["result"]["findings"][0]["code"], "configuration_changed")
        self.assertEqual(old, self.current.resolve())
        two = self.client.post(f"/wiki/jobs/{one['id']}/retry")
        self.assertEqual(two.status_code, 202)
        self.assertEqual(self.worker.run_once()["status"], "succeeded")

    def test_configuration_change_during_generation_never_publishes_mixed_plan(self):
        before = self.current.resolve()
        self.manual.write_text("전원을 차단하고 베어링을 교체한다. 이번 개정에서 진동을 확인한다.\n")
        def change():
            self.config["title"] = "모델 실행 중 변경"
            self.write_config()
        self.provider.on_author = change
        self.service.enqueue_build()
        result = self.worker.run_once()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["result"]["findings"][0]["code"], "configuration_changed")
        self.assertEqual(before, self.current.resolve())

    def test_admitted_dependency_change_becomes_actionable_hold(self):
        self.service.save_report("S-1", 1, body=self.body(), revision="R1", status="finalized", reuse_approved=True)
        self.assertEqual(self.worker.run_once()["status"], "succeeded")
        before = self.current.resolve()
        self.manual.write_text("원본 매뉴얼 개정으로 기준이 변경됨.\n")
        job = self.service.enqueue_build()
        result = self.worker.run_once()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["result"]["findings"][0]["code"], "dependency_changed")
        self.assertEqual(before, self.current.resolve())
        self.assertEqual(self.service.job_status(job["id"])["hold_reasons"][0]["code"], "dependency_changed")

    def test_revoked_approval_holds_new_work_and_explains_refresh_block(self):
        self.service.save_report("S-1", 1, body=self.body(), revision="R1", status="finalized", reuse_approved=True)
        self.assertEqual(self.worker.run_once()["status"], "succeeded")
        before = self.current.resolve()
        self.service.set_report_approval("S-1", 1, status="finalized", reuse_approved=False)
        self.assertEqual(self.worker.run_once()["result"]["findings"][0]["code"], "awaiting_approval")
        self.service.enqueue_build()
        result = self.worker.run_once()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["result"]["findings"][0]["code"], "admitted_source_unavailable")
        self.assertEqual(before, self.current.resolve())

    def test_report_dedupe_does_not_treat_underscores_as_sql_wildcards(self):
        one = self.service.report_saved("A-B", 1, source="database")
        two = self.service.report_saved("A_B", 1, source="database")
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(one["id"], self.service.report_saved("A-B", 1, source="database")["id"])

    def test_posted_report_before_setup_uses_activated_initial_mapping(self):
        paths = []
        class FakeAgent:
            def __init__(self,path):self.path=path
            def run(self):return {"state":"published"}
            def ingest_report(self,rid,version):
                paths.append(yaml.safe_load(self.path.read_text()))
                return {"status":"published","findings":[]}
        self.service.agent_factory=FakeAgent
        self.service.enqueue_bootstrap()
        external=self.service.report_saved("EXTERNAL",1,source="canonical")
        plan={"status":"ready","findings":[],"counts":{},"tables":[],"report_jobs":[],
              "proposed_postgres":{"tables":["factory.assets"],"equipment":{"table":"factory.assets","columns":{"equipment_id":"asset_no"}}}}
        with patch("clew_okf.runtime.worker.scan_database",return_value=plan):
            self.assertEqual(self.worker.run_once()["status"],"succeeded")
        self.assertEqual(self.worker.run_once()["id"],external["id"])
        self.assertEqual(paths[0]["postgres"]["equipment"]["table"],"factory.assets")

    def test_server_approval_resolver_is_required_to_promote_upload_or_save(self):
        bad = self.client.post("/wiki/reports/S-1", json=self.request(reuse_approved=True, status="finalized"))
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.service.jobs.list_jobs(), [])
        broken = self.make_client(lambda _id,_version,submission: {"status": "draft", "reuse_approved": True})
        self.assertEqual(broken.post("/wiki/reports/S-1", json=self.request()).status_code, 503)
        self.assertEqual(self.service.jobs.list_jobs(), [])

    def test_primary_approval_cannot_be_reused_for_a_different_body_or_metadata(self):
        from clew_okf.stages.step_01_ingest.managed_intake import submission_descriptor
        import hashlib
        approved = submission_descriptor("S-1", 1, source="text", content_sha=hashlib.sha256(self.body().encode()).hexdigest(),
                                         revision="R1", metadata={})
        client = self.make_client(lambda _id,_version,_submitted: {
            "status":"finalized", "reuse_approved":True, "approved_fingerprint":approved["fingerprint"]})
        for request in (self.request(body=self.body(result="다른 원본 결과")),
                        self.request(metadata={"plant_id":"P2"}), self.request(revision="R2")):
            self.assertEqual(client.post("/wiki/reports/S-1",json=request).status_code,409)
        self.assertEqual(self.service.jobs.list_jobs(),[])
        self.assertEqual(client.post("/wiki/reports/S-1",json=self.request()).status_code,202)

    def test_upload_approval_is_bound_to_file_bytes_and_not_report_id_alone(self):
        from clew_okf.stages.step_01_ingest.managed_intake import submission_descriptor
        import hashlib
        content=b"%PDF-1.7\napproved original"
        approved=submission_descriptor("S-1",1,source="file",content_sha=hashlib.sha256(content).hexdigest(),revision="R1",metadata={})
        client=self.make_client(lambda _id,_version,_submitted:{
            "status":"finalized","reuse_approved":True,"approved_fingerprint":approved["fingerprint"]})
        response=client.post("/wiki/reports/S-1/upload",data={"details":json.dumps({"version":1,"revision":"R1"})},
                             files={"file":("report.pdf",b"%PDF-1.7\nforged other file")})
        self.assertEqual(response.status_code,409)
        self.assertEqual(self.service.jobs.list_jobs(),[])

    def test_managed_retry_updates_report_latest_job_pointer(self):
        initial=self.client.post("/wiki/reports/S-1",json=self.request()).json()
        self.worker.run_once()
        retry=self.client.post(initial["status_url"]+"/retry").json()
        current=self.client.get("/wiki/reports/S-1/status",params={"version":1}).json()
        self.assertEqual(current["id"],retry["job_id"])
        self.assertEqual(current["status"],"queued")
        replay=self.client.post("/wiki/reports/S-1",json=self.request()).json()
        self.assertEqual(replay["job_id"],retry["job_id"])
        self.assertEqual(self.service.report_status("S-1",1)["id"],retry["job_id"])
        same_approval=self.service.set_report_approval("S-1",1,status="draft",reuse_approved=False)
        self.assertEqual(same_approval["job"]["id"],retry["job_id"])

    def test_file_upload_is_persisted_before_queue_and_uses_actual_gate(self):
        client = self.make_client(lambda _id,_version,submission: {"status": "finalized", "reuse_approved": True, "approved_fingerprint": submission["fingerprint"]})
        details = {"version": 1, "revision": "UPLOAD-R1"}
        response = client.post("/wiki/reports/S-1/upload", data={"details": json.dumps(details)},
                               files={"file": ("report.pdf", b"%PDF-1.7\nfixture", "application/pdf")})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.provider.calls, [])
        def parsed(config, base, **kwargs):
            relative,options = next(iter(config["files"].items()))
            path = Path(config["root"]) / relative
            return [Source(f"docs:{relative}", options.get("title", "report"), "document", path.as_uri(),
                           (Block("page 1", self.body()),), revision=options["revision"])], []
        with patch("clew_okf.stages.step_01_ingest.raw_report_store.load_documents", side_effect=parsed):
            result = self.worker.run_once()
            self.assertEqual(result["status"], "succeeded")
            self.assertTrue(self.page().exists())
            self.assertEqual(self.service.agent().run()["state"], "unchanged")
        conflict = client.post("/wiki/reports/S-1/upload", data={"details": json.dumps(details)},
                               files={"file": ("report.pdf", b"%PDF-1.7\nchanged", "application/pdf")})
        self.assertEqual(conflict.status_code, 409)

    def test_runtime_configuration_preserves_native_yaml_date_filters(self):
        from datetime import date
        self.config["postgres"]={"records":[{"table":"factory.current","id_column":"id","columns":["body"],
                                             "filters":{"effective_on":date(2026,11,19)}}]}
        self.write_config()
        runtime=self.service.write_runtime_config()
        loaded=yaml.safe_load(runtime.read_text())
        self.assertEqual(loaded["postgres"]["records"][0]["filters"]["effective_on"],date(2026,11,19))
        reopened=WikiService(self.path)
        self.assertEqual(self.service._signature(),reopened._signature())
        self.assertEqual(self.service.enqueue_build()["id"],reopened.enqueue_build()["id"])

    def test_service_uploads_inherit_the_configured_document_extractor(self):
        self.config["documents"]["kordoc"] = "./parsers/kordoc"
        self.write_config()
        runtime = yaml.safe_load(self.service.write_runtime_config().read_text())
        self.assertEqual(runtime["admission"]["mapping"]["files"]["kordoc"], str((self.root / "parsers/kordoc").resolve()))
        self.assertEqual(runtime["admission"]["mapping"]["files"]["max_bytes"], self.service.intake.max_upload_bytes)

    def test_upload_rejects_bad_paths_types_and_client_approval_flags(self):
        details = json.dumps({"version": 1, "revision": "R1"})
        for filename,content in [("../../report.pdf", b"%PDF-1.7"), ("report.exe", b"MZfoo"), ("report.pdf", b"notpdf")]:
            result = self.client.post("/wiki/reports/S-1/upload", data={"details": details}, files={"file": (filename,content)})
            self.assertEqual(result.status_code, 422)
        result = self.client.post("/wiki/reports/S-1/upload", data={"details": details,"reuse_approved":"true"},
                                  files={"file": ("report.pdf", b"%PDF-1.7")})
        self.assertEqual(result.status_code, 422)
        result = self.client.post("/wiki/reports/S-1/upload", data={"details": json.dumps({"version":1,"revision":"R1","reuse_approved":True})},
                                  files={"file": ("report.pdf", b"%PDF-1.7")})
        self.assertEqual(result.status_code, 422)
        self.assertEqual(self.service.jobs.list_jobs(), [])

    def test_request_body_cap_applies_to_chunked_and_content_length(self):
        cap = self.service.intake.max_upload_bytes + 64 * 1024
        # Invalid oversized declared lengths are rejected without allocating that body.
        response = self.client.post("/wiki/reports/S-1", content=b"{}", headers={"content-length": str(cap+1),"content-type":"application/json"})
        self.assertEqual(response.status_code, 413)

    def test_chunked_upload_cannot_bypass_request_byte_limit(self):
        from clew_okf.async_api import create_service_router, WikiBodyLimitMiddleware
        app=FastAPI()
        app.add_middleware(WikiBodyLimitMiddleware,max_bytes=32,prefix="/wiki")
        app.include_router(create_service_router(self.service))
        with TestClient(app) as client:
            def chunks():
                yield b'{"version":1,"revision":"R1","body":"'
                yield b'x'*100
                yield b'"}'
            response=client.post("/wiki/reports/S-1",content=chunks(),headers={"content-type":"application/json"})
            self.assertEqual(response.status_code,413)
        self.assertEqual(self.service.jobs.list_jobs(),[])

    def test_worker_does_not_exhaust_retries_while_output_is_locked(self):
        from clew_okf.workflows.compiler import BuildInProgressError
        class LockedAgent:
            def __init__(self,path):pass
            def run(self):raise BuildInProgressError("locked")
        self.service.agent_factory=LockedAgent
        now=[time.time()+1]
        self.service.jobs=JobStore(self.service.state_db,now=lambda:now[0])
        self.service.enqueue_build()
        for _ in range(5):
            job=self.worker.run_once()
            self.assertEqual(job["status"],"queued")
            self.assertEqual(job["attempts"],0)
            now[0]+=6
        self.service.agent_factory=WikiAgent
        self.assertEqual(self.worker.run_once()["status"],"succeeded")

    def test_external_commit_event_is_idempotent_while_active(self):
        one = self.client.post("/wiki/reports/EXTERNAL/saved", json={"version":1,"source":"database"})
        two = self.client.post("/wiki/reports/EXTERNAL/saved", json={"version":1,"source":"database"})
        self.assertEqual(one.status_code, 202)
        self.assertEqual(one.json()["job_id"], two.json()["job_id"])
        self.assertEqual(self.provider.calls, [])

    def test_external_unapproved_canonical_report_is_held_with_reason_not_failed(self):
        from clew_okf.stages.step_01_ingest.report_store import CanonicalReportHoldError
        before=self.current.resolve()
        self.service.report_saved("EXTERNAL",1,source="canonical")
        with patch("clew_okf.stages.step_01_ingest.report_store.load_report",side_effect=CanonicalReportHoldError(
            "Canonical report payload is invalid",code="awaiting_approval",public_message="정본 승인이 필요합니다.")):
            result=self.worker.run_once()
        self.assertEqual(result["status"],"held")
        self.assertEqual(self.service.job_status(result["id"])["hold_reasons"][0]["code"],"awaiting_approval")
        self.assertEqual(before,self.current.resolve())
        self.assertEqual(self.provider.calls,[])

    def test_bootstrap_parent_waits_until_all_approved_report_jobs_finish(self):
        calls = []
        class FakeAgent:
            def __init__(self, path):self.path=path
            def run(self):calls.append("build");return {"state":"published","snapshot":"fixture"}
            def ingest_report(self, rid, version):
                calls.append(rid)
                if rid=="OLD-2":return {"status":"hold","findings":[{"code":"fixture_missing","message":"결과 누락"}]}
                return {"status":"published","findings":[]}
        self.service.agent_factory = FakeAgent
        plan = {"status":"ready","findings":[],"counts":{"tables":2,"report_jobs":2},
                "tables":[],"proposed_postgres":{},"report_jobs":[
                    {"report_id":"OLD-1","version":1,"source":"canonical"},
                    {"report_id":"OLD-2","version":1,"source":"canonical"}]}
        accepted = self.client.post("/wiki/setup").json()
        with patch("clew_okf.runtime.worker.scan_database", return_value=plan):
            first = self.worker.run_once()
        self.assertEqual(first["status"], "waiting")
        self.assertEqual(calls, ["build"])
        self.assertEqual(self.worker.run_once()["status"], "succeeded")
        self.assertEqual(self.service.job_status(accepted["job_id"])["status"], "waiting")
        self.assertEqual(self.worker.run_once()["status"], "held")
        parent = self.client.get(accepted["status_url"]).json()
        self.assertEqual(parent["status"], "held")
        self.assertEqual(parent["result"]["children"]["processed"], 2)
        self.assertEqual(parent["result"]["children"]["succeeded"], 1)
        self.assertEqual(parent["result"]["children"]["held"], 1)
        children = self.client.get(accepted["status_url"] + "/children").json()["jobs"]
        self.assertEqual(len(children), 2)
        held_child = next(child for child in children if child["payload"]["report_id"] == "OLD-2")
        self.assertEqual(held_child["hold_reasons"][0]["code"], "fixture_missing")

    def test_ambiguous_initial_db_plan_is_held_without_initial_publication(self):
        before = self.current.resolve()
        self.service.enqueue_bootstrap()
        plan = {"status":"hold","findings":[{"code":"equipment_ambiguous","message":"설비 후보가 여러 개"}]}
        with patch("clew_okf.runtime.worker.scan_database", return_value=plan):
            result = self.worker.run_once()
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["result"]["findings"][0]["code"], "equipment_ambiguous")
        self.assertEqual(before, self.current.resolve())


if __name__ == "__main__":
    unittest.main()
