from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import yaml

from clew_okf import RequiredEvidenceMissingError
from clew_okf.workflows.compiler import build_wiki
from clew_okf.workflows.release import (
    ReleaseNotApprovedError,
    approve_release,
    inspect_release_candidate,
    read_approved_release,
)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        originals = root / "originals"
        originals.mkdir()
        self.original = originals / "manual.md"
        self.original.write_text("원본 설비 사양 A\n", encoding="utf-8")
        self.config = root / "wiki.yaml"
        self.config.write_text(yaml.safe_dump({"documents": {"root": "originals"}}), encoding="utf-8")

    def inspect_and_approve(self):
        candidate = inspect_release_candidate(self.config)
        self.assertEqual(candidate["status"], "draft")
        return approve_release(
            self.config, snapshot=candidate["snapshot"], bundle_sha256=candidate["bundle_sha256"],
            reviewer="설비 담당자",
        )

    def test_build_does_not_approve_and_new_draft_does_not_replace_previous_release(self):
        build = build_wiki(self.config)
        self.assertEqual(build["state"], "published")
        with self.assertRaises(ReleaseNotApprovedError):
            read_approved_release(self.config)

        approved = self.inspect_and_approve()
        self.assertEqual(approved["snapshot"], build["snapshot"])
        self.assertEqual(approved["approved_by"], "설비 담당자")
        self.assertEqual(read_approved_release(self.config), approved)

        self.original.write_text("원본 설비 사양 B\n", encoding="utf-8")
        new_draft = build_wiki(self.config)
        self.assertNotEqual(new_draft["snapshot"], approved["snapshot"])
        self.assertEqual(read_approved_release(self.config), approved)
        with self.assertRaisesRegex(ReleaseNotApprovedError, "초안이 바뀌었습니다"):
            approve_release(
                self.config, snapshot=approved["snapshot"], bundle_sha256=approved["bundle_sha256"],
                reviewer="설비 담당자",
            )
        self.assertEqual(read_approved_release(self.config), approved)

    def test_byte_change_invalidates_reviewed_candidate_and_approved_snapshot(self):
        build_wiki(self.config)
        candidate = inspect_release_candidate(self.config)
        # A valid change to the same snapshot path must require a new review.
        index = Path(candidate["bundle"]) / "index.md"
        with index.open("a", encoding="utf-8") as stream:
            stream.write("\n추가 운전 조건\n")
        with self.assertRaisesRegex(ReleaseNotApprovedError, "초안이 바뀌었습니다"):
            approve_release(
                self.config, snapshot=candidate["snapshot"], bundle_sha256=candidate["bundle_sha256"],
                reviewer="설비 담당자",
            )
        self.inspect_and_approve()
        with index.open("a", encoding="utf-8") as stream:
            stream.write("\n다시 변경된 운전 조건\n")
        with self.assertRaisesRegex(ReleaseNotApprovedError, "변경됐습니다"):
            read_approved_release(self.config)

    def test_invalid_structured_reference_stops_before_model_and_preserves_release(self):
        build_wiki(self.config)
        approved = self.inspect_and_approve()
        self.config.write_text(yaml.safe_dump({
            "documents": {"root": "originals"},
            "synthesis": {
                "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
                "model": "stub",
                "structured_evidence": [{
                    "source_key": "docs:manual.md",
                    "protected_locators": ["page 47"],
                    "groups": [],
                }],
            },
        }), encoding="utf-8")
        with patch("clew_okf.providers.llm.ChatCompletionsModel.complete") as complete:
            with self.assertRaisesRegex(ValueError, "원문 위치를 찾지 못했습니다"):
                build_wiki(self.config)
            complete.assert_not_called()
        self.assertEqual(inspect_release_candidate(self.config)["snapshot"], approved["snapshot"])
        self.assertEqual(read_approved_release(self.config), approved)


    def test_required_group_hold_preserves_current_and_approved_release(self):
        root = self.config.parent
        (root / "equipment.csv").write_text(
            "equipment_id,name,plant_id\nP6,펌프 P6,REF-01\n", encoding="utf-8",
        )
        self.original.write_text("교환 주기 3,000시간\n최소 연 1회\n", encoding="utf-8")
        config = {
            "equipment_csv": "equipment.csv",
            "documents": {"root": "originals", "files": {"manual.md": {
                "title": "제조사 매뉴얼", "equipment_ids": ["REF-01/P6"], "revision": "Rev.3",
            }}},
        }
        self.config.write_text(yaml.safe_dump(config), encoding="utf-8")
        build_wiki(self.config)
        approved = self.inspect_and_approve()
        current = root / "output/current"
        prior = current.readlink()
        retained = (current / "index.md").read_bytes()
        config["synthesis"] = {
            "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
            "model": "stub",
            "review": False,
            "structured_evidence": [{
                "source_key": "docs:manual.md",
                "protected_locators": ["lines 1-2"],
                "groups": [{
                    "id": "oil-change",
                    "label": "오일 교환 조건",
                    "equipment_ids": ["REF-01/P6"],
                    "required": True,
                    "spans": [
                        {"locator": "lines 1-2", "quote": "교환 주기 3,000시간"},
                        {"locator": "lines 1-2", "quote": "최소 연 1회"},
                    ],
                }],
            }],
        }
        self.config.write_text(yaml.safe_dump(config), encoding="utf-8")

        def select_group(system, payload, **kwargs):
            evidence = payload["evidence"][0]
            return {"claims": [{"id": evidence["id"], "section": "적용 조건", "quote": "최소 연 1회"}]}

        transport = "clew_okf.providers.llm.ChatCompletionsModel.complete"
        with patch(transport, return_value={"claims": []}) as complete:
            with self.assertRaises(RequiredEvidenceMissingError) as held:
                build_wiki(self.config)
            complete.assert_called_once()
        self.assertEqual(current.readlink(), prior)
        self.assertEqual((current / "index.md").read_bytes(), retained)
        self.assertEqual(inspect_release_candidate(self.config)["snapshot"], approved["snapshot"])
        self.assertEqual(read_approved_release(self.config), approved)
        snapshots = root / "output/snapshots"
        self.assertEqual({p.name for p in snapshots.iterdir()}, {approved["snapshot"]})
        finding, = held.exception.findings
        self.assertEqual(finding["code"], "required_evidence_missing")
        self.assertEqual(finding["equipment_key"], "REF-01/P6")
        self.assertEqual(finding["group_id"], "oil-change")
        self.assertEqual(finding["label"], "오일 교환 조건")
        self.assertEqual([item["quote"] for item in finding["evidence"]], ["교환 주기 3,000시간", "최소 연 1회"])
        for item in finding["evidence"]:
            self.assertEqual(item["source_key"], "docs:manual.md")
            self.assertEqual(item["source_title"], "제조사 매뉴얼")
            self.assertEqual(item["revision"], "Rev.3")
            self.assertEqual(item["locator"], "lines 1-2")
            self.assertIn(item["evidence_id"], finding["evidence_ids"])

        with patch(transport, side_effect=select_group):
            accepted = build_wiki(self.config)
        self.assertEqual((accepted["state"], accepted["selected_groups"]), ("published", 1))
        self.assertNotEqual(current.readlink(), prior)
        self.assertEqual(read_approved_release(self.config), approved)
        page = next((current / "equipment").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("교환 주기 3,000시간", page)
        self.assertIn("최소 연 1회", page)

        self.config.write_text(yaml.safe_dump({**config, "output": "first-build"}), encoding="utf-8")
        with patch(transport, return_value={"claims": []}):
            with self.assertRaises(RequiredEvidenceMissingError):
                build_wiki(self.config)
        self.assertFalse((root / "first-build/current").is_symlink())
        self.assertEqual(list((root / "first-build/snapshots").iterdir()), [])
