"""Execution trace access, snapshot ordering, retention, and service mounting."""

from copy import deepcopy
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from clew_okf.interfaces.async_api import install_wiki_service
from clew_okf.runtime import agent_runs
from clew_okf.runtime.service import WikiService


_URL = "/wiki/agent-runs"
_INGEST = {"Authorization": "Bearer uploader"}
_ADMIN = {"Authorization": "Bearer operator"}


def _run(request_id="request-1", session_id="session-1", *, status="running", minute="00"):
    terminal = status != "running"
    return {
        "version": 1,
        "request_id": request_id,
        "session_id": session_id,
        "started_at": f"2026-11-25T09:{minute}:00+09:00",
        "finished_at": f"2026-11-25T09:{minute}:03+09:00" if terminal else None,
        "status": status,
        "request": {"message": "비공개 요청"},
        "response": {"message": "비공개 응답", "type": "answer"} if terminal else None,
        "steps": [{
            "kind": "chat", "name": "chat.received", "source": "BE/app/modules/chat/router.py::stream",
            "at": f"2026-11-25T09:{minute}:01+09:00", "status": "completed", "details": {},
        }],
    }


class AgentRunTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "wiki.yaml"
        self.path.write_text("output: wiki\nservice:\n  bootstrap_on_startup: false\n", encoding="utf-8")
        tokens = patch.dict(os.environ, CLEW_MONITOR_INGEST_TOKEN="uploader", CLEW_MONITOR_ADMIN_TOKEN="operator")
        tokens.start()
        self.addCleanup(tokens.stop)
        self.client = self.make_client()

    def make_client(self, dependencies=None):
        app = FastAPI()
        install_wiki_service(app, WikiService(self.path), dependencies=dependencies)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    def post(self, run):
        return self.client.post(_URL, headers=_INGEST, json=run)

    def test_ingest_and_read_authorization_are_separate_and_fail_closed(self):
        for url in (_URL, _URL + "/request-1"):
            self.assertEqual(self.client.get(url).status_code, 401)
            self.assertEqual(self.client.get(url, headers=_INGEST).status_code, 401)
        self.assertEqual(self.client.post(_URL, headers=_ADMIN, json=_run()).status_code, 401)
        with patch.dict(os.environ, CLEW_MONITOR_INGEST_TOKEN="", CLEW_MONITOR_ADMIN_TOKEN=""):
            self.assertEqual(self.post(_run()).status_code, 503)
            self.assertEqual(self.client.get(_URL, headers=_ADMIN).status_code, 503)
        self.assertEqual(self.client.get(_URL + "/request-1", headers=_ADMIN).status_code, 404)

    def test_existing_host_dependencies_are_still_applied(self):
        def access(host_session: str = Header(default="")):
            if host_session != "session":
                raise HTTPException(403)

        client = self.make_client(dependencies=[Depends(access)])
        self.assertEqual(client.get(_URL, headers=_ADMIN).status_code, 403)
        self.assertEqual(client.get(_URL, headers={**_ADMIN, "host-session": "session"}).status_code, 200)

    def test_terminal_snapshot_is_idempotent_and_visible_across_app_instances(self):
        live = _run()
        completed = _run(status="completed")
        completed["steps"].append({
            "kind": "wiki", "name": "wiki.lookup", "source": "BE/app/modules/chat/wiki.py::lookup",
            "at": "2026-11-25T00:00:02Z", "status": "completed", "details": {"hits": 2},
        })
        for snapshot in (live, completed, completed, live):
            result = self.post(snapshot)
            self.assertEqual(result.status_code, 204, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
        wrong_session = _run(session_id="other")
        wrong_terminal = deepcopy(completed)
        wrong_terminal["response"]["message"] = "재전송에서 변조된 응답"
        self.assertEqual(self.post(wrong_session).status_code, 409)
        self.assertEqual(self.post(wrong_terminal).status_code, 409)
        reader = self.make_client()
        detail = reader.get(_URL + "/request-1", headers=_ADMIN)
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.headers["cache-control"], "no-store")
        self.assertEqual(detail.headers["vary"], "Authorization")
        stored = detail.json()
        self.assertEqual(stored["response"]["message"], completed["response"]["message"])
        self.assertEqual(stored["status"], "completed")
        self.assertEqual(stored["steps"][1]["details"], {"hits": 2})
        self.assertEqual(stored["started_at"], "2026-11-25T00:00:00.000000+00:00")

    def test_model_steps_are_recorded_without_claiming_external_execution(self):
        completed = _run(status="completed")
        completed["steps"].append({
            "kind": "llm", "name": "response.model", "source": "BE/chat::compose",
            "at": "2026-11-25T00:00:02Z", "status": "completed", "duration_ms": 120,
            "details": {"provider": "provider-name", "model": "model-id", "output_tokens": 17},
        })
        forged = {**completed, "origin": "wiki_evaluation"}
        self.assertEqual(self.post(forged).status_code, 422)
        self.assertEqual(self.post(completed).status_code, 204)
        stored = self.client.get(_URL + "/request-1", headers=_ADMIN).json()
        self.assertEqual(stored["steps"][1]["duration_ms"], 120)
        self.assertEqual(stored["steps"][1]["details"]["model"], "model-id")

    def test_recent_summary_filters_and_never_contains_chat_content(self):
        self.assertEqual(self.post(_run(status="completed")).status_code, 204)
        self.assertEqual(self.post(_run("request-2", "session-2", minute="30")).status_code, 204)
        recent = self.client.get(_URL, params={"limit": 1}, headers=_ADMIN)
        self.assertEqual(recent.json()["runs"][0]["request_id"], "request-2")
        self.assertEqual(recent.headers["cache-control"], "no-store")
        session = self.client.get(_URL, params={"session_id": "session-1"}, headers=_ADMIN)
        self.assertEqual(len(session.json()["runs"]), 1)
        summary = session.json()["runs"][0]
        self.assertEqual(summary["request_id"], "request-1")
        self.assertEqual(summary["response_type"], "answer")
        self.assertEqual(summary["kinds"], ["chat"])
        self.assertEqual(summary["step_count"], 1)
        self.assertTrue({"request", "response", "steps"}.isdisjoint(summary))
        self.assertNotIn("비공개", session.text)

    def test_rejects_oversize_malformed_and_inconsistent_snapshots(self):
        oversized = self.client.post(_URL, headers=_INGEST, content=b" " * ((128 * 1024) + 1))
        self.assertEqual(oversized.status_code, 413)
        broken = self.client.post(_URL, headers=_INGEST, content=b'{"message":"SECRET_VALUE",')
        self.assertEqual(broken.status_code, 422)
        self.assertNotIn("SECRET_VALUE", broken.text)
        invalid = [
            {**_run(), "other": "irrelevant"},
            {**_run(), "request_id": ".."},
            {**_run(), "status": "failed"},
            {**_run(), "started_at": "2026-11-25T00:00:00"},
            {**_run(), "started_at": "2026-11-25T01:00:00Z"},
            {**_run(), "steps": _run()["steps"] * 65},
            {**_run(), "request": {"message": "가" * 6001}},
        ]
        for run in invalid:
            self.assertEqual(self.post(run).status_code, 422, run["request_id"])
        self.assertEqual(self.client.get(_URL, headers=_ADMIN).json(), {"runs": []})

    def test_retention_hides_expired_rows_and_caps_the_store(self):
        with patch.object(agent_runs, "MAX_RUNS", 2):
            for minute in ("00", "01", "02"):
                self.assertEqual(self.post(_run(f"request-{minute}", minute=minute)).status_code, 204)
        list_response = self.client.get(_URL, headers=_ADMIN)
        self.assertEqual([run["request_id"] for run in list_response.json()["runs"]], ["request-02", "request-01"])
        self.assertEqual(self.client.get(_URL + "/request-00", headers=_ADMIN).status_code, 404)
        with patch.object(agent_runs, "RETENTION_SECONDS", -1):
            self.assertEqual(self.client.get(_URL, headers=_ADMIN).json(), {"runs": []})
            self.assertEqual(self.client.get(_URL + "/request-01", headers=_ADMIN).status_code, 404)
