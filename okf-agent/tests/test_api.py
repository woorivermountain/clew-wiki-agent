import csv
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml

try:
    import fcntl
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ImportError:
    HAS_API = False
else:
    HAS_API = True
    from clew_okf import WikiAgent
    from clew_okf.domain.errors import RequiredEvidenceMissingError
    from clew_okf.api import create_app, create_router
    from clew_okf.workflows.compiler import BuildInProgressError
    from clew_okf.workflows.release import approve_release, inspect_release_candidate


@unittest.skipUnless(HAS_API, "Install the optional API and test dependencies")
class ApiTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.originals = self.root / "originals"
        self.originals.mkdir()
        (self.originals / "manual.md").write_text("진동 허용오차 ±0.15 mm\n", encoding="utf-8")
        (self.root / "equipment.csv").write_text(
            "equipment_id,name,plant_id\nC-2100,공기압축기,P1\n", encoding="utf-8"
        )
        self.config = self.root / "clew.yaml"
        self.config.write_text(yaml.safe_dump({
            "equipment_csv": "equipment.csv",
            "output": "published",
            "documents": {
                "root": "originals",
                "files": {"manual.md": {"equipment_ids": ["P1/C-2100"], "revision": "Rev.1"}},
            },
        }, allow_unicode=True), encoding="utf-8")

    def client(self, agent=None, *, prefix="/wiki"):
        app = FastAPI()
        app.include_router(create_router(agent or WikiAgent(self.config), prefix=prefix))
        return TestClient(app)

    def test_native_input_build_repeat_and_server_owned_configuration(self):
        with self.client() as client:
            first = client.post("/wiki/build", json={"config_path": str(self.root / "untrusted.yaml")})
            self.assertEqual(first.status_code, 200, first.text)
            result = first.json()
            self.assertEqual(result["state"], "published")
            self.assertEqual((result["equipment"], result["sources"]), (1, 1))
            self.assertEqual(result["wiki_reviews"], [])
            reference, = (Path(result["bundle"]) / "references").glob("*.md")
            self.assertIn("±0.15 mm", reference.read_text(encoding="utf-8"))
            again = client.post("/wiki/build")
            self.assertEqual(again.status_code, 200, again.text)
            self.assertEqual(again.json()["state"], "unchanged")
            self.assertEqual(again.json()["snapshot"], result["snapshot"])
            self.assertEqual(again.json()["bundle"], result["bundle"])

    def test_release_returns_approved_bundle_only(self):
        with self.client(prefix="/internal/wiki") as client:
            self.assertEqual(client.post("/internal/wiki/build").status_code, 200)
            draft_only = client.get("/internal/wiki/release")
            self.assertEqual(draft_only.status_code, 404, draft_only.text)
            self.assertEqual(draft_only.headers["cache-control"], "no-store")

            candidate = inspect_release_candidate(self.config)
            release = approve_release(self.config, snapshot=candidate["snapshot"],
                bundle_sha256=candidate["bundle_sha256"], reviewer="supervisor")
            approved = client.get("/internal/wiki/release", params={"config_path": "untrusted.yaml"})
            self.assertEqual(approved.status_code, 200, approved.text)
            self.assertEqual(approved.headers["cache-control"], "no-store")
            self.assertEqual(approved.json(), release)

            (self.originals / "manual.md").write_text("새 위키 초안\n", encoding="utf-8")
            newer_draft = client.post("/internal/wiki/build")
            self.assertEqual(newer_draft.status_code, 200, newer_draft.text)
            self.assertNotEqual(newer_draft.json()["snapshot"], release["snapshot"])
            self.assertEqual(client.get("/internal/wiki/release").json(), release)

            card, = (Path(release["bundle"]) / "equipment").glob("*.md")
            card.write_text(card.read_text(encoding="utf-8") + "\n내용이 변조됐습니다.\n", encoding="utf-8")
            tampered = client.get("/internal/wiki/release")
            self.assertEqual(tampered.status_code, 404, tampered.text)
            self.assertEqual(tampered.headers["cache-control"], "no-store")
            self.assertEqual(set(tampered.json()), {"detail"})

    def test_conflicting_build_returns_409_and_preserves_current(self):
        with self.client() as client:
            first = client.post("/wiki/build")
            self.assertEqual(first.status_code, 200, first.text)
            current = Path(first.json()["bundle"])
            previous = current.resolve()
            lock_path = self.root / "published" / ".build.lock"
            with lock_path.open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                response = client.post("/wiki/build")
            self.assertEqual(response.status_code, 409)
            self.assertEqual(current.resolve(), previous)
            self.assertEqual(client.post("/wiki/build").status_code, 200)

    def test_required_evidence_hold_exposes_review_details(self):
        findings = [{
            "code": "required_evidence_missing", "group_id": "inspection",
            "evidence": [{"source_key": "docs:manual.md", "locator": "line 1", "quote": "점검 조건"}],
        }]
        agent = Mock(spec=WikiAgent)
        agent.run.side_effect = RequiredEvidenceMissingError(findings)
        with self.client(agent) as client:
            response = client.post("/wiki/build")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.json(), {"detail": {"status": "hold", "findings": findings}})

    def test_errors_are_mapped_without_exposing_internal_details(self):
        secret = "private-password database-host /private/manual.pdf"
        failures = (
            (BuildInProgressError(secret), 409),
            (ValueError(secret), 422),
            (yaml.YAMLError(secret), 422),
            (csv.Error(secret), 422),
            (RuntimeError(secret), 502),
            (PermissionError(secret), 503),
        )
        for error, status in failures:
            with self.subTest(error=type(error).__name__):
                agent = Mock(spec=WikiAgent)
                agent.run.side_effect = error
                with self.client(agent, prefix="/internal/wiki") as client:
                    response = client.post("/internal/wiki/build")
                self.assertEqual(response.status_code, status, response.text)
                self.assertNotIn(secret, response.text)
                self.assertEqual(set(response.json()), {"detail"})
                agent.run.assert_called_once_with()

    def test_app_factory_uses_environment_configuration(self):
        with patch.dict(os.environ, {"CLEW_WIKI_CONFIG": str(self.config)}):
            app = create_app()
        with TestClient(app) as client:
            response = client.post("/wiki/build")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["state"], "published")

    def test_app_factory_requires_configuration(self):
        with patch.dict(os.environ, {"CLEW_WIKI_CONFIG": ""}):
            with self.assertRaisesRegex(ValueError, "CLEW_WIKI_CONFIG"):
                create_app()
