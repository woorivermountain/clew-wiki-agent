"""Check how protected source regions reach the writer, reviewer, and OKF bundle."""

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clew_okf.domain.model import Block, Equipment, Source
from clew_okf.stages.step_06_publish.bundle import evidence_id, file_key, validate_bundle, write_bundle


class StructuredBundleTests(unittest.TestCase):
    def setUp(self):
        work = TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.output = Path(work.name) / "bundle"
        self.equipment = [
            Equipment(tag, {"plant_id": "REF-01", "name": tag}, "file:///equipment.csv", f"CSV {index}행")
            for index, tag in enumerate(("P5", "P6"), 2)
        ]
        self.source = Source(
            key="docs:manual.pdf",
            title="펌프 매뉴얼",
            kind="document",
            resource="file:///manual.pdf",
            blocks=(
                Block("page 1", "작업 전 장비 태그를 확인합니다."),
                Block("page 25", "065-040-250은 다른 모델 행입니다."),
                Block("page 46", "오일 교환 | 매 3,000시간6)"),
                Block("page 47", "6) 최소 연 1회"),
            ),
            equipment_ids=("REF-01/P5", "REF-01/P6"),
            revision="Rev 3",
        )

    def _synthesis(self, note="6) 최소 연 1회"):
        return {
            "model": "writer",
            "structured_evidence": [
                {
                    "source_key": self.source.key,
                    "protected_locators": ["page 25", "page 46", "page 47"],
                    "groups": [
                        {
                            "id": "oil-change",
                            "label": "오일 교환 기준",
                            "equipment_ids": ["REF-01/P6"],
                            "spans": [
                                {"locator": "page 46", "quote": "오일 교환 | 매 3,000시간6)"},
                                {"locator": "page 47", "quote": note},
                            ],
                        },
                    ],
                },
            ],
        }

    def _build(self, synthesis, provider):
        return write_bundle(
            self.output, "테스트 위키", [self.source], self.equipment,
            "2026-11-23T00:00:00+00:00", synthesis, None,
            provider=provider,
        )

    def test_scoped_group_keeps_every_span_and_original_citation(self):
        calls = []
        raw_id = evidence_id(self.source.key, "page 1")
        table_id = evidence_id(self.source.key, "page 46")
        note_id = evidence_id(self.source.key, "page 47")

        class Model:
            def complete(self, system, payload, *, model=None):
                calls.append((system, payload))
                if payload.get("role") == "independent_reviewer":
                    if payload["equipment"]["key"] == "REF-01/P6":
                        return {"issues": [{
                            "claim_id": 3, "category": "scope",
                            "message": "정비 조건을 확인하세요.", "evidence_ids": [note_id],
                        }]}
                    return {"issues": []}

                regular = {"id": raw_id, "section": "데이터 설명", "quote": "장비 태그를 확인합니다."}
                if payload["equipment"] == "REF-01/P5":
                    return {"claims": [regular]}
                group = next(row for row in payload["evidence"] if "3,000시간" in row["text"])
                return {"claims": [
                    regular,
                    {"id": group["id"], "section": "점검 절차", "quote": "매 3,000시간"},
                    {"id": group["id"], "section": "주의 사항", "quote": "최소 연 1회"},
                ]}

        result = self._build(self._synthesis(), Model())
        self.assertEqual(result.selected_groups, 1)
        self.assertEqual(result.selected_quotes, 4)
        self.assertEqual(validate_bundle(self.output), 3)
        writes = {payload["equipment"]: payload for _, payload in calls if "evidence" in payload}
        reviews = [(system, payload) for system, payload in calls if payload.get("role") == "independent_reviewer"]
        p5_input = writes["REF-01/P5"]["evidence"]
        p6_input = writes["REF-01/P6"]["evidence"]
        self.assertEqual(p5_input, [{"id": raw_id, "text": "작업 전 장비 태그를 확인합니다."}])
        self.assertEqual(len(p6_input), 2)
        group = next(row for row in p6_input if row["id"] != raw_id)
        self.assertIn("매 3,000시간6)", group["text"])
        self.assertIn("최소 연 1회", group["text"])
        self.assertTrue(all("065-040-250" not in row["text"] for row in p6_input))
        self.assertTrue(all(row["id"] not in {table_id, note_id} for row in p6_input))

        _, p5_review = next(row for row in reviews if row[1]["equipment"]["key"] == "REF-01/P5")
        prompt, p6_review = next(row for row in reviews if row[1]["equipment"]["key"] == "REF-01/P6")
        self.assertNotIn("draft_groups", p5_review)
        self.assertIn("Read all its quoted spans together", prompt)
        self.assertEqual(p6_review["draft_groups"], [{"label": "오일 교환 기준", "claim_ids": [2, 3]}])
        self.assertEqual([item["id"] for item in p6_review["draft"]], [raw_id, table_id, note_id])
        self.assertNotIn(group["id"], {item["id"] for item in p6_review["original"]})

        p6 = (self.output / "equipment" / f"{file_key('REF-01/P6')}.md").read_text(encoding="utf-8")
        self.assertEqual(p6.count("- **오일 교환 기준**"), 1)
        self.assertIn(f"  - 오일 교환 \\| 매 3,000시간6) [^{table_id}]", p6)
        self.assertIn(f"  - 6) 최소 연 1회 [^{note_id}]", p6)
        self.assertIn(f"    - **검토 필요** · 정비 조건을 확인하세요. [^{note_id}]", p6)
        self.assertIn(f"[^{table_id}]: 펌프 매뉴얼 · Rev 3 · page 46", p6)
        self.assertIn(f"[^{note_id}]: 펌프 매뉴얼 · Rev 3 · page 47", p6)
        self.assertNotIn(group["id"], p6)
        self.assertNotIn("065-040-250", p6)
        p5 = (self.output / "equipment" / f"{file_key('REF-01/P5')}.md").read_text(encoding="utf-8")
        self.assertNotIn("3,000시간", p5)
        self.assertNotIn("최소 연 1회", p5)
        self.assertNotIn("065-040-250", p5)
        reference = next((self.output / "references").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("065-040-250", reference)

    def test_manifest_text_mismatch_fails_before_model_request(self):
        class Model:
            def complete(self, *args, **kwargs):
                raise AssertionError("검증에 실패한 근거는 모델에 전달하지 않습니다.")

        with self.assertRaises(ValueError):
            self._build(self._synthesis("6) 최소 연 2회"), Model())
        self.assertFalse((self.output / "index.md").exists())

    def test_legacy_writer_and_reviewer_have_no_group_payload(self):
        calls = []
        raw_id = evidence_id(self.source.key, "page 1")

        class Model:
            def complete(self, system, payload, *, model=None):
                calls.append(payload)
                if payload.get("role") == "independent_reviewer":
                    return {"issues": []}
                return {"claims": [{"id": raw_id, "section": "점검 절차", "quote": "장비 태그를 확인합니다."}]}

        result = self._build({"model": "legacy"}, Model())
        self.assertEqual((result.selected_quotes, result.selected_groups), (2, 0))
        self.assertTrue(all("draft_groups" not in payload for payload in calls))
        writer = next(payload for payload in calls if "evidence" in payload)
        self.assertEqual(len(writer["evidence"]), 4)
        p6 = (self.output / "equipment" / f"{file_key('REF-01/P6')}.md").read_text(encoding="utf-8")
        frontmatter = yaml.safe_load(p6.split("---", 2)[1])
        self.assertEqual(frontmatter["clew_review"]["status"], "passed")
        self.assertIn(f"\n- 장비 태그를 확인합니다. [^{raw_id}]", p6)
