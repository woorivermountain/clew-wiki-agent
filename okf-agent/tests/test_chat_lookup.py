"""Approved Wiki lookups preserve identity scope and the release boundary."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    HAS_API = False
else:
    HAS_API = True
    from clew_okf import WikiAgent, build_wiki
    from clew_okf.interfaces.api import create_router
    from clew_okf.interfaces.agent_runs import create_agent_runs_router
    from clew_okf.interfaces.chat_lookup import create_evaluation_lookup_router, local_evaluation_access
    from clew_okf.workflows.release import approve_release, inspect_release_candidate


@unittest.skipUnless(HAS_API, "Install the optional API and test dependencies")
class ChatLookupTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="clew-chat-lookup-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()

    def test_chat_lookup_reads_only_the_approved_plant_and_equipment(self):
        config = self.root / "wiki.yaml"
        config.write_text(yaml.safe_dump({
            "output": "published", "equipment_csv": "equipment.csv",
            "documents": {"root": "documents", "files": {"guide.md": {
                "title": "Pump guide", "revision": "Rev. 3", "equipment_ids": ["REF-01/P6"],
            }}},
        }))
        (self.root / "equipment.csv").write_text(
            "equipment_id,name,plant_id\nP6,Feed Pump,REF-01\nP6,Other Pump,REF-02\n"
        )
        (self.root / "documents").mkdir()
        guide = self.root / "documents" / "guide.md"
        guide.write_text("Synthetic reference text.\n")
        app = FastAPI()
        app.include_router(create_router(WikiAgent(config)))
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("CLEW_WIKI_LOOKUP_TOKEN", None)
        payload = {"plant_id": "REF-01", "equipment_id": "P6"}
        endpoint = "/wiki/chat/lookup"
        with TestClient(app) as client:
            assert client.post(endpoint, json=payload).status_code == 503
            os.environ["CLEW_WIKI_LOOKUP_TOKEN"] = "wiki-lookup-test"
            assert client.post(endpoint, json=payload).status_code == 401
            headers = {"Authorization": "Bearer wiki-lookup-test"}

            build_wiki(config)
            plain = client.post(endpoint, json=payload, headers=headers).json()
            assert plain["result"] == "unapproved"
            assert "trace" not in plain
            blocked = client.post(endpoint, json={**payload, "include_trace": True}, headers=headers).json()
            assert blocked["trace"]["steps"][0]["details"]["rule_id"] == "approved_release_required"
            assert blocked["trace"]["steps"][0]["details"]["decision"] == "blocked"
            candidate = inspect_release_candidate(config)
            approved = approve_release(config, snapshot=candidate["snapshot"], bundle_sha256=candidate["bundle_sha256"],
                                       reviewer="test operator")
            guide.write_text("Changed draft reference text.\n")
            assert build_wiki(config)["snapshot"] != approved["snapshot"]

            result = client.post(endpoint, json=payload, headers=headers)
            assert result.status_code == 200
            assert result.headers["cache-control"] == "no-store"
            hit = result.json()
            assert hit["result"] == "hit"
            assert hit["snapshot"] == approved["snapshot"]
            assert hit["page"]["title"] == "Feed Pump"
            assert hit["source_count"] == 1
            assert hit["sources"][0]["title"] == "Pump guide"
            assert hit["sources"][0]["revision"] == "Rev. 3"
            assert str(self.root) not in result.text
            traced = client.post(endpoint, json={**payload, "include_trace": True}, headers=headers).json()
            steps = traced.pop("trace")["steps"]
            assert traced == hit
            assert [step["name"] for step in steps] == [
                "wiki.release.approval", "wiki.equipment.scope", "wiki.sources.linked",
            ]
            assert all(step["duration_ms"] >= 0 for step in steps)
            assert steps[-1]["details"]["sources"] == hit["sources"]
            assert all(step["kind"] != "llm" for step in steps)

            other = client.post(endpoint, json={**payload, "plant_id": "REF-02"}, headers=headers).json()
            assert other["page"]["title"] == "Other Pump"
            assert other["sources"] == []
            assert client.post(endpoint, json={**payload, "plant_id": "REF-03"}, headers=headers).json()["result"] == "miss"

            (Path(approved["bundle"]) / hit["page"]["path"]).write_text("Tampered.\n")
            assert client.post(endpoint, json=payload, headers=headers).json()["result"] == "unapproved"


    def test_local_evaluation_persists_a_separate_draft_trace(self):
        config = self.root / "wiki.yaml"
        config.write_text("output: wiki\nequipment_csv: equipment.csv\n")
        (self.root / "equipment.csv").write_text("equipment_id,name,plant_id\nP6,Feed Pump,REF-01\n")
        snapshot = build_wiki(config)["snapshot"]
        app = FastAPI()
        port = 8765
        origin = f"http://127.0.0.1:{port}"
        access = local_evaluation_access(port)
        app.include_router(create_evaluation_lookup_router(config, self.root, authorize=access), prefix="/wiki")
        app.include_router(create_agent_runs_router(self.root, read_dependency=access), prefix="/wiki")
        endpoint = "/wiki/studio/evaluation-lookup"
        target = {"plant_id": "REF-01", "equipment_id": "P6"}

        def with_peer(host):
            async def request_from_peer(scope, receive, send):
                await app({**scope, "client": (host, 50000)}, receive, send)
            return request_from_peer

        with TestClient(with_peer("203.0.113.10"), base_url=origin) as client:
            assert client.post(endpoint, json=target).status_code == 403
            assert client.get("/wiki/agent-runs").status_code == 403
        with TestClient(with_peer("127.0.0.1"), base_url=origin) as client:
            assert client.post(endpoint, json=target, headers={"Origin": "https://example.org"}).status_code == 403
            assert client.get("/wiki/agent-runs", headers={"Host": "example.org"}).status_code == 403
            assert client.post(endpoint, content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
            assert not client.get("/wiki/agent-runs").json()["runs"]
            response = client.post(endpoint, json=target, headers={"Origin": origin})
            assert response.status_code == 200
            result = response.json()
            assert result["result"] == "hit"
            assert result["snapshot"] == snapshot
            detail = client.get(f"/wiki/agent-runs/{result['request_id']}").json()
            assert detail["origin"] == "wiki_evaluation"
            assert detail["request"] == {}
            assert [step["name"] for step in detail["steps"]] == [
                "wiki.evaluation.draft", "wiki.equipment.scope", "wiki.sources.linked",
            ]
            assert detail["steps"] == result["trace"]["steps"]
            assert client.get("/wiki/agent-runs").json()["runs"][0]["origin"] == "wiki_evaluation"
