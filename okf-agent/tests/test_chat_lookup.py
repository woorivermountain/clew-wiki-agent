"""Approved Wiki lookups preserve identity scope and the release boundary."""

from pathlib import Path

import pytest
import yaml

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from clew_okf import WikiAgent, build_wiki
from clew_okf.interfaces.api import create_router
from clew_okf.workflows.release import approve_release, inspect_release_candidate


def test_chat_lookup_reads_only_the_approved_plant_and_equipment(tmp_path, monkeypatch):
    config = tmp_path / "wiki.yaml"
    config.write_text(yaml.safe_dump({
        "output": "published", "equipment_csv": "equipment.csv",
        "documents": {"root": "documents", "files": {"guide.md": {
            "title": "Pump guide", "revision": "Rev. 3", "equipment_ids": ["REF-01/P6"],
        }}},
    }))
    (tmp_path / "equipment.csv").write_text(
        "equipment_id,name,plant_id\nP6,Feed Pump,REF-01\nP6,Other Pump,REF-02\n"
    )
    (tmp_path / "documents").mkdir()
    guide = tmp_path / "documents" / "guide.md"
    guide.write_text("Synthetic reference text.\n")
    app = FastAPI()
    app.include_router(create_router(WikiAgent(config)))
    monkeypatch.delenv("CLEW_WIKI_LOOKUP_TOKEN", raising=False)
    payload = {"plant_id": "REF-01", "equipment_id": "P6"}
    endpoint = "/wiki/chat/lookup"
    with TestClient(app) as client:
        assert client.post(endpoint, json=payload).status_code == 503
        monkeypatch.setenv("CLEW_WIKI_LOOKUP_TOKEN", "wiki-lookup-test")
        assert client.post(endpoint, json=payload).status_code == 401
        headers = {"Authorization": "Bearer wiki-lookup-test"}

        build_wiki(config)
        assert client.post(endpoint, json=payload, headers=headers).json()["result"] == "unapproved"
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
        assert str(tmp_path) not in result.text

        other = client.post(endpoint, json={**payload, "plant_id": "REF-02"}, headers=headers).json()
        assert other["page"]["title"] == "Other Pump"
        assert other["sources"] == []
        assert client.post(endpoint, json={**payload, "plant_id": "REF-03"}, headers=headers).json()["result"] == "miss"

        (Path(approved["bundle"]) / hit["page"]["path"]).write_text("Tampered.\n")
        assert client.post(endpoint, json=payload, headers=headers).json()["result"] == "unapproved"
