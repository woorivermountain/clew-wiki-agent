import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

try:
    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.testclient import TestClient
except ImportError:
    HAS_API = False
else:
    HAS_API = True
    from clew_okf import WikiAgent, build_wiki
    from clew_okf.interfaces.api import create_router
    from clew_okf.interfaces.async_api import create_service_router
    from clew_okf.runtime.service import WikiService
    from clew_okf.workflows.release import approve_release, inspect_release_candidate


@unittest.skipUnless(HAS_API, "Install the optional API and test dependencies")
class StudioApiTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory(prefix="clew-studio-(test)-")
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        originals = self.root / "originals"
        originals.mkdir()
        self.manual = originals / "manual.md"
        self.manual.write_text("P6 진동 허용 기준 3 mm/s\n", encoding="utf-8")
        (self.root / "equipment.csv").write_text(
            "equipment_id,name,plant_id\nP6,상압잔사유 이송 펌프,REF-01\n", encoding="utf-8",
        )
        self.config = self.root / "wiki.yaml"
        self.settings = {
            "title": "P6 위키",
            "output": "published",
            "equipment_csv": "equipment.csv",
            "documents": {"root": "originals", "files": {"manual.md": {
                "title": "제조사 매뉴얼", "equipment_ids": ["REF-01/P6"], "revision": "Rev.3",
            }}},
        }
        self.write_config()

    def write_config(self):
        self.config.write_text(yaml.safe_dump(self.settings, allow_unicode=True), encoding="utf-8")

    def test_graph_and_review_evidence_come_from_actual_okf_bundle(self):
        self.settings["synthesis"] = {"endpoint": "http://127.0.0.1:11434/v1/chat/completions", "model": "stub"}
        self.write_config()

        def model_response(prompt, payload, **kwargs):
            if payload.get("role") == "independent_reviewer":
                return {"issues": [{"claim_id": 1, "category": "scope", "message": "설비 조건을 확인하세요.",
                    "evidence_ids": [payload["original"][0]["id"]]}]}
            return {"claims": [{"id": payload["evidence"][0]["id"], "section": "적용 조건",
                "quote": "P6 진동 허용 기준 3 mm/s"}]}

        with patch("clew_okf.providers.llm.ChatCompletionsModel.complete", side_effect=model_response):
            built = build_wiki(self.config)

        app = FastAPI()
        app.include_router(create_router(WikiAgent(self.config), prefix="/internal/wiki"))
        with TestClient(app) as client, patch.dict(os.environ, {"CLEW_WIKI_STUDIO_CONTEXT": "evaluation"}):
            result = client.get("/internal/wiki/studio/overview")
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(result.headers["cache-control"], "no-store")
            overview = result.json()
            self.assertEqual(overview["context"], "evaluation")
            self.assertFalse(overview["jobs_available"])
            self.assertIsNone(overview["approved"])
            self.assertEqual(overview["draft"]["snapshot"], built["snapshot"])
            self.assertEqual(overview["draft"]["counts"],
                             {"pages": 4, "equipment": 1, "sources": 1, "selected_quotes": 1, "selected_groups": 0})
            equipment = next(p for p in overview["pages"] if p["type"] == "Equipment")
            manual = next(p for p in overview["pages"] if p["source_key"] == "docs:manual.md")
            self.assertIn({"source": equipment["path"], "target": manual["path"]}, overview["links"])
            issue, = overview["reviews"][0]["issues"]
            self.assertEqual(issue["message"], "설비 조건을 확인하세요.")

            result = client.get("/internal/wiki/studio/page", params={
                "path": equipment["path"], "snapshot": built["snapshot"],
            })
            self.assertEqual(result.status_code, 200, result.text)
            self.assertNotIn("file://", result.text)
            self.assertNotIn(str(self.root), result.text)
            page = result.json()
            self.assertIn("설비 조건을 확인하세요.", page["markdown"])
            self.assertEqual(page["backlinks"], ["index.md"])
            evidence = next(row for row in page["evidence"] if row["id"] in issue["evidence_ids"])
            self.assertEqual(evidence, {"id": issue["evidence_ids"][0], "title": "제조사 매뉴얼",
                "revision": "Rev.3", "locator": "line 1", "path": manual["path"]})

    def test_selected_evidence_groups_stay_bound_to_snapshot_and_exact_citations(self):
        self.manual.write_text("Oil change | Interval\\nApply only when oil lubricated\\n", encoding="utf-8")
        self.settings["synthesis"] = {
            "endpoint": "http://127.0.0.1:11434/v1/chat/completions", "model": "fixture", "review": False,
            "structured_evidence": [{
                "source_key": "docs:manual.md", "protected_locators": ["line 1"], "groups": [{
                    "id": "oil", "label": "Oil schedule", "required": True, "equipment_ids": ["REF-01/P6"],
                    "spans": [
                        {"locator": "line 1", "quote": "Oil change | Interval"},
                        {"locator": "line 1", "quote": "Apply only when oil lubricated"},
                    ],
                }],
            }],
        }
        self.write_config()
        with patch("clew_okf.providers.llm.ChatCompletionsModel.complete", return_value={
            "claims": [{"id": "group-oil", "section": "적용 조건", "quote": "Oil schedule"}],
        }):
            built = build_wiki(self.config)

        app = FastAPI()
        app.include_router(create_router(WikiAgent(self.config)))
        with TestClient(app) as client:
            overview = client.get("/wiki/studio/overview").json()
            path = next(page["path"] for page in overview["pages"] if page["type"] == "Equipment")
            response = client.get("/wiki/studio/page", params={"path": path, "snapshot": built["snapshot"]})
            self.assertEqual(response.status_code, 200, response.text)
            data = response.json()
            groups = data["structured_evidence"]["groups"]
            selected, = groups
            self.assertEqual(selected["relationship"], "selected")
            self.assertTrue(selected["required"])
            self.assertEqual([span["quote"] for span in selected["evidence"]],
                             ["Oil change | Interval", "Apply only when oil lubricated"])
            for span in selected["evidence"]:
                self.assertEqual(span["revision"], "Rev.3")
                self.assertEqual(span["evidence_id"], span["id"])
                self.assertTrue(span["path"].startswith("references/"))
            self.assertEqual(data["build"]["snapshot"], built["snapshot"])
            self.assertIsNone(data["build"]["approval"])

    def test_approved_view_stays_bound_to_its_version_and_rejects_tampering(self):
        app = FastAPI()
        app.include_router(create_router(WikiAgent(self.config)))
        with TestClient(app) as client:
            self.assertIsNone(client.get("/wiki/studio/overview").json()["draft"])
            original = build_wiki(self.config)
            candidate = inspect_release_candidate(self.config)
            approved = approve_release(self.config, snapshot=candidate["snapshot"],
                bundle_sha256=candidate["bundle_sha256"], reviewer="설비 담당자")
            self.manual.write_text("P6 진동 허용 기준 4 mm/s\n", encoding="utf-8")
            newest = build_wiki(self.config)
            overview = client.get("/wiki/studio/overview", params={"view": "approved"}).json()
            self.assertEqual(overview["draft"]["snapshot"], newest["snapshot"])
            self.assertEqual(overview["approved"]["snapshot"], original["snapshot"])
            self.assertEqual(overview["approved"]["approved_by"], "설비 담당자")
            manual = next(p for p in overview["pages"] if p["source_key"] == "docs:manual.md")["path"]
            self.assertEqual(client.get("/wiki/studio/page", params={"path": manual,
                "snapshot": original["snapshot"]}).status_code, 409)
            response = client.get("/wiki/studio/page", params={"path": manual, "view": "approved",
                "snapshot": original["snapshot"]})
            self.assertEqual(response.status_code, 200)
            self.assertIn("3 mm/s", response.json()["markdown"])

            (Path(approved["bundle"]) / manual).write_text("변경됨", encoding="utf-8")
            self.assertIsNone(client.get("/wiki/studio/overview").json()["approved"])
            self.assertEqual(client.get("/wiki/studio/page",
                params={"path": manual, "view": "approved"}).status_code, 404)

    def test_async_workspace_uses_existing_router_dependencies_and_rejects_unlisted_paths(self):
        build = build_wiki(self.config)
        outside = self.root / "private.md"
        outside.write_text("outside snapshot", encoding="utf-8")
        references = Path(build["bundle"]) / "references"
        (references / "linked.md").symlink_to(outside)

        def require_token(x_demo_token: str | None = Header(None)):
            if x_demo_token != "viewer":
                raise HTTPException(401)

        app = FastAPI()
        app.include_router(create_service_router(WikiService(self.config), prefix="/clew",
            dependencies=[Depends(require_token)]))
        with TestClient(app) as client:
            self.assertEqual(client.get("/clew/studio").status_code, 401)
            self.assertEqual(client.get("/clew/studio/overview").status_code, 401)
            headers = {"x-demo-token": "viewer"}
            overview = client.get("/clew/studio/overview", headers=headers).json()
            self.assertTrue(overview["jobs_available"])
            self.assertNotIn("references/linked.md", {page["path"] for page in overview["pages"]})
            for path in ("references/linked.md", "../private.md", str(outside), "references/../index.md"):
                with self.subTest(path=path):
                    self.assertEqual(client.get("/clew/studio/page", headers=headers,
                        params={"path": path}).status_code, 404)
            self.assertEqual(client.get("/clew/studio/assets/private.md", headers=headers).status_code, 404)
