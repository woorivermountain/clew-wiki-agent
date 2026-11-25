import unittest
from copy import deepcopy
from dataclasses import replace

from clew_okf.domain.model import Block, Source
from clew_okf.stages.step_04_compose.structured_evidence import resolve_structured_evidence


class StructuredEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.p46 = Block("page 46", "Oil change | Interval\nFurther oil changes | every 3000 operating hours6)")
        self.p47 = Block("page 47", "6) At least once a year\n7) Different application note")
        self.p26 = Block("page 26", "Unconfirmed material variant")
        self.source = Source(
            "docs:manual.pdf", "Manual", "document", "file:///manual.pdf",
            (self.p46, self.p47, self.p26), equipment_ids=("REF/P6",), revision="Rev.2",
        )
        self.lookup = {
            "src-row": (self.source, self.p46),
            "src-note": (self.source, self.p47),
            "src-variant": (self.source, self.p26),
        }
        self.synthesis = {
            "structured_evidence": [{
                "source_key": self.source.key,
                "protected_locators": ["page 46", "page 47", "page 26"],
                "groups": [{
                    "id": "oil-change",
                    "label": "오일 윤활 교환 주기",
                    "equipment_ids": ["REF/P6"],
                    "spans": [
                        {"locator": "page 46", "quote": "Further oil changes | every 3000 operating hours6)"},
                        {"locator": "page 47", "quote": "6) At least once a year"},
                    ],
                }],
            }],
        }

    def resolve(self):
        return resolve_structured_evidence(self.synthesis, self.lookup, {"REF/P6", "REF/P2"})

    def test_default_returns_no_structured_evidence(self):
        for synthesis in (None, {}, {"model": "writer"}):
            with self.subTest(synthesis=synthesis):
                self.assertEqual(resolve_structured_evidence(synthesis, self.lookup, {"REF/P6"}), (frozenset(), {}))

    def test_resolves_cross_page_condition_with_actual_citation_ids(self):
        before = deepcopy(self.lookup), deepcopy(self.synthesis)
        protected, groups = self.resolve()
        self.assertIsInstance(protected, frozenset)
        self.assertEqual(protected, {"src-row", "src-note", "src-variant"})
        group = groups["group-oil-change"]
        self.assertEqual(group.identifier, "group-oil-change")
        self.assertEqual(group.equipment_ids, ("REF/P6",))
        self.assertEqual(group.spans, (
            ("src-row", "Further oil changes | every 3000 operating hours6)"),
            ("src-note", "6) At least once a year"),
        ))
        self.assertEqual(group.block.text, "\n\n".join([group.label, *(quote for _, quote in group.spans)]))
        self.assertEqual((self.lookup, self.synthesis), before)
        self.assertEqual(self.lookup["src-note"][0].revision, "Rev.2")

    def test_required_defaults_to_false_and_requires_a_yaml_boolean(self):
        group = self.synthesis["structured_evidence"][0]["groups"][0]
        self.assertFalse(self.resolve()[1]["group-oil-change"].required)
        group["required"] = True
        self.assertTrue(self.resolve()[1]["group-oil-change"].required)
        for value in (None, "true", 1):
            with self.subTest(value=value):
                group["required"] = value
                with self.assertRaisesRegex(ValueError, "required"):
                    self.resolve()

    def test_can_protect_locations_without_exposing_an_unconfirmed_group(self):
        self.synthesis["structured_evidence"][0]["groups"] = []
        protected, groups = self.resolve()
        self.assertEqual(protected, {"src-row", "src-note", "src-variant"})
        self.assertEqual(groups, {})

    def test_rejects_missing_sources_disabled_sources_and_unsupported_scope(self):
        self.synthesis["structured_evidence"][0]["source_key"] = "docs:unknown.pdf"
        with self.assertRaisesRegex(ValueError, "합성할 수 있는 원문"):
            self.resolve()

        self.synthesis["structured_evidence"][0]["source_key"] = self.source.key
        old_lookup = self.lookup
        self.lookup = {identifier: (replace(source, synthesize=False), block)
                       for identifier, (source, block) in old_lookup.items()}
        with self.assertRaisesRegex(ValueError, "합성할 수 있는 원문"):
            self.resolve()

        self.lookup = old_lookup
        self.synthesis["structured_evidence"][0]["groups"][0]["equipment_ids"] = ["REF/P2"]
        with self.assertRaisesRegex(ValueError, "설비 범위"):
            self.resolve()

    def test_rejects_unprotected_missing_or_ambiguous_footnotes(self):
        spec = self.synthesis["structured_evidence"][0]
        spec["protected_locators"].remove("page 47")
        with self.assertRaisesRegex(ValueError, "보호 목록"):
            self.resolve()
        spec["protected_locators"].append("page 47")
        del self.lookup["src-note"]
        with self.assertRaisesRegex(ValueError, "원문 위치"):
            self.resolve()
        # The matching note on another page cannot be substituted.
        self.lookup["src-note"] = (self.source, Block("page 47", "9) At least once a year"))
        with self.assertRaisesRegex(ValueError, "정확히 한 번"):
            self.resolve()
        self.lookup["src-note"] = (self.source, self.p47)
        self.lookup["src-duplicate-note"] = (self.source, self.p47)
        with self.assertRaisesRegex(ValueError, "정확히 한 번"):
            self.resolve()
        del self.lookup["src-duplicate-note"]
        repeated = Block("page 47", "6) At least once a year\n6) At least once a year")
        self.lookup["src-note"] = (self.source, repeated)
        with self.assertRaisesRegex(ValueError, "반복"):
            self.resolve()

    def test_rejects_empty_config_unknown_fields_and_duplicate_identifiers(self):
        spec = self.synthesis["structured_evidence"][0]
        for bad in (None, [], {}, "not a list"):
            with self.subTest(bad=bad):
                self.synthesis["structured_evidence"] = bad
                with self.assertRaisesRegex(ValueError, "목록"):
                    self.resolve()
        self.synthesis["structured_evidence"] = [spec]
        spec["extra"] = "unknown"
        with self.assertRaisesRegex(ValueError, "필드"):
            self.resolve()
        del spec["extra"]
        self.synthesis["structured_evidence"].append(deepcopy(spec))
        with self.assertRaisesRegex(ValueError, "중복"):
            self.resolve()
        self.synthesis["structured_evidence"].pop()
        spec["groups"].append(deepcopy(spec["groups"][0]))
        with self.assertRaisesRegex(ValueError, "중복"):
            self.resolve()

    def test_rejects_oversize_span_or_group(self):
        spec = self.synthesis["structured_evidence"][0]
        group = spec["groups"][0]
        span = group["spans"][0]
        span["quote"] = "a" * 601
        with self.assertRaisesRegex(ValueError, "600자"):
            self.resolve()
        group["spans"] = []
        for i in range(7):
            quote = f"{i}: " + "a" * 597
            locator = f"extra {i}"
            self.lookup[locator] = (self.source, Block(locator, quote))
            spec["protected_locators"].append(locator)
            group["spans"].append({"locator": locator, "quote": quote})
        with self.assertRaisesRegex(ValueError, "4000자"):
            self.resolve()


if __name__ == "__main__":
    unittest.main()
