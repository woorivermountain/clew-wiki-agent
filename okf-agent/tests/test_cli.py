from contextlib import redirect_stdout
import csv
from io import StringIO
import json
import os
import subprocess
import sys
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from unittest.mock import patch

import yaml

from clew_okf.cli import _build, main
from clew_okf.domain.errors import RequiredEvidenceMissingError

PACKAGE = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.manuals = self.root / "originals"
        self.manuals.mkdir()
        self.manual = self.manuals / "manual.md"
        self.manual.write_text("진동 기준 ±0.15 mm\n", encoding="utf-8")
        self.config = {
            "equipment_csv": "equipment.csv",
            "output": "published",
            "documents": {
                "root": "originals",
                "files": {"manual.md": {
                    "equipment_ids": ["A/C-2100"],
                    "revision": "Rev.1",
                }},
            },
        }
        with (self.root / "equipment.csv").open("w", encoding="utf-8", newline="") as output:
            writer = csv.writer(output)
            writer.writerows([
                ["equipment_id", "name", "plant_id", "process_id"],
                ["C-2100", "설비 A", "A", "회전"],
                ["C-2100", "설비 B", "B", "포장"],
            ])
        self.config_path = self.root / "clew.yaml"
        self.current = self.root / "published/current"

    def run_cli(self, *args):
        env = {**os.environ, "PYTHONPATH": str(PACKAGE), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run(
            [sys.executable, "-m", "clew_okf.cli", *map(str, args)],
            env=env, cwd=self.root, text=True, capture_output=True, timeout=20, check=False,
        )

    def build(self, expected=0):
        self.config_path.write_text(yaml.safe_dump(self.config, allow_unicode=True), encoding="utf-8")
        run = self.run_cli("build", "--config", self.config_path)
        self.assertEqual(run.returncode, expected, run.stderr)
        return json.loads(run.stdout) if expected == 0 else run.stderr

    def equipment_pages(self):
        return {
            yaml.safe_load(contents.split("---", 2)[1])["equipment_key"]: contents
            for path in (self.current / "equipment").glob("*.md")
            if (contents := path.read_text(encoding="utf-8"))
        }

    def review_for(self, report, equipment_key="A/C-2100"):
        return next(review for review in report["wiki_reviews"] if review["equipment_key"] == equipment_key)

    def test_postgres_equipment_uses_id_as_title_when_name_is_unmapped(self):
        del self.config["equipment_csv"]
        postgres = {"equipment": {
            "table": "inventory.assets",
            "columns": {"equipment_id": "asset_no", "plant_id": "plant_code"},
        }}
        self.config["postgres"] = postgres
        self.config_path.write_text(yaml.safe_dump(self.config), encoding="utf-8")
        rows = [{"equipment_id": "C-2100", "plant_id": "A"}]
        with patch("clew_okf.workflows.compiler.load_postgres", return_value=([], rows)) as load_postgres:
            result = _build(self.config_path)
        load_postgres.assert_called_with(postgres)
        self.assertEqual(result["state"], "published")
        page = self.equipment_pages()["A/C-2100"]
        self.assertEqual(yaml.safe_load(page.split("---", 2)[1])["title"], "C-2100")

    def test_build_hold_returns_nonzero_with_structured_findings(self):
        findings = [{"code": "required_evidence_missing", "label": "최소 연 1회"}]
        output = StringIO()
        with (
            patch.object(sys, "argv", ["clew-okf", "build", "--config", str(self.config_path)]),
            patch("clew_okf.cli._build", side_effect=RequiredEvidenceMissingError(findings)),
            redirect_stdout(output),
        ):
            self.assertEqual(main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {"status": "hold", "findings": findings})

    def test_csv_equipment_still_requires_nonempty_name(self):
        (self.root / "equipment.csv").write_text("equipment_id,name\nC-2100,  \n", encoding="utf-8")
        self.assertIn("CSV에 name이 필요합니다", self.build(expected=1))

    def test_scoped_links_and_valid_bundle(self):
        result = self.build()
        self.assertEqual(result["state"], "published")
        pages = self.equipment_pages()
        self.assertIn("manual", pages["A/C-2100"])
        self.assertNotIn("manual", pages["B/C-2100"])
        self.assertEqual(
            json.loads(self.run_cli("check", self.current).stdout), {"state": "valid", "pages": 3}
        )

    def test_exact_directory_scope_and_document_type(self):
        relative = Path("B/C-2100/manual.md")
        target = self.manuals / relative
        target.parent.mkdir(parents=True)
        self.manual.rename(target)
        self.config["documents"].update(
            scope_from_path=True,
            files={relative.as_posix(): {"type": "Maintenance Procedure"}},
        )
        report = self.build()
        self.assertEqual(report["unmapped_documents"], 0)
        pages = self.equipment_pages()
        self.assertNotIn("manual", pages["A/C-2100"])
        self.assertIn("manual", pages["B/C-2100"])
        reference, = (self.current / "references").glob("*.md")
        self.assertIn("type: Maintenance Procedure", reference.read_text(encoding="utf-8"))

    def test_change_reuse_and_failed_update(self):
        first = self.build()
        old = self.current.resolve()
        self.assertEqual(self.build()["state"], "unchanged")
        self.manual.write_text("진동 기준 ±0.20 mm\n", encoding="utf-8")
        self.config["documents"]["files"]["manual.md"]["revision"] = "Rev.2"
        second = self.build()
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        new = self.current.resolve()
        self.assertIn("Rev.2", self.equipment_pages()["A/C-2100"])
        self.assertTrue(old.exists())
        self.manual.write_text("", encoding="utf-8")
        self.build(expected=1)
        self.assertEqual(self.current.resolve(), new)

    def test_deletion_and_unsupported_report(self):
        self.build()
        self.manual.unlink()
        self.config["documents"]["files"].clear()
        (self.manuals / "drawing.dwg").write_text("test", encoding="utf-8")
        report = self.build()
        self.assertEqual(report["sources"], 0)
        self.assertTrue(any("drawing.dwg" in warning for warning in report["warnings"]))
        self.assertIn("**Deletion**", (self.current / "log.md").read_text(encoding="utf-8"))

    def test_unknown_scope_keeps_previous_publication(self):
        self.build()
        previous = self.current.resolve()
        self.config["documents"]["files"]["manual.md"]["equipment_ids"] = ["WRONG/C-2100"]
        self.assertIn("WRONG/C-2100", self.build(expected=1))
        self.assertEqual(self.current.resolve(), previous)

    def synthesis_model(self, respond):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                payload = json.loads(data["messages"][-1]["content"])
                requests.append((data["model"], payload))
                result = respond(payload)
                if isinstance(result, int):
                    self.send_error(result)
                    return
                if isinstance(result, bytes):
                    self.send_response(200)
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(result)
                    self.close_connection = True
                    return
                response = {"choices": [{"message": {"content": json.dumps(result)}}]}
                reply = json.dumps(response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.config["synthesis"] = {
            "endpoint": f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
            "model": "test-writer",
            "review_model": "test-reviewer",
        }
        return requests

    def test_reference_only_document_remains_linked_and_stays_out_of_model_inputs(self):
        restricted = "보존하는 과거 사례 원문"
        (self.manuals / "case-history.md").write_text(restricted, encoding="utf-8")
        self.config["documents"]["files"]["case-history.md"] = {
            "title": "보존 사례",
            "equipment_ids": ["A/C-2100"],
            "synthesize": False,
        }

        def respond(payload):
            if payload.get("role") == "independent_reviewer":
                return {"issues": []}
            return {"claims": [{
                "id": payload["evidence"][0]["id"],
                "section": "적용 조건",
                "quote": "±0.15 mm",
            }]}

        requests = self.synthesis_model(respond)
        report = self.build()

        self.assertEqual(report["sources"], 2)
        self.assertEqual(report["selected_quotes"], 1)
        self.assertEqual([model for model, _ in requests], ["test-writer", "test-reviewer"])
        self.assertTrue(self.review_for(report)["coverage_complete"])
        self.assertEqual(self.review_for(report)["total_blocks"], 1)
        self.assertNotIn(restricted, json.dumps(requests, ensure_ascii=False))
        self.assertIn("보존 사례", self.equipment_pages()["A/C-2100"])
        self.assertIn("보존 사례", (self.current / "index.md").read_text(encoding="utf-8"))
        references = (self.current / "references").glob("*.md")
        self.assertTrue(any(restricted in page.read_text(encoding="utf-8") for page in references))

    def test_parallel_equipment_preserves_publication_order_and_scope(self):
        second_quote = "압력 기준 0.55 MPa"
        (self.manuals / "second.md").write_text(second_quote, encoding="utf-8")
        self.config["documents"]["files"]["second.md"] = {"equipment_ids": ["B/C-2100"]}
        writers = Barrier(2, timeout=5)

        def respond(payload):
            if payload.get("role") == "independent_reviewer":
                return {"issues": []}
            source, = payload["evidence"]
            writers.wait()
            return {"claims": [{
                "id": source["id"],
                "section": "적용 조건",
                "quote": source["text"].strip(),
            }]}

        requests = self.synthesis_model(respond)
        self.config["synthesis"]["max_parallel_equipment"] = 2
        report = self.build()

        self.assertEqual(report["state"], "published")
        self.assertEqual(report["selected_quotes"], 2)
        self.assertEqual([r["equipment_key"] for r in report["wiki_reviews"]], ["A/C-2100", "B/C-2100"])
        self.assertTrue(all(r["status"] == "passed" for r in report["wiki_reviews"]))
        self.assertCountEqual([model for model, _ in requests], ["test-writer", "test-reviewer"] * 2)
        pages = self.equipment_pages()
        self.assertIn("±0.15 mm", pages["A/C-2100"])
        self.assertNotIn(second_quote, pages["A/C-2100"])
        self.assertIn(second_quote, pages["B/C-2100"])
        self.assertNotIn("±0.15 mm", pages["B/C-2100"])
        self.assertEqual(self.run_cli("check", self.current).returncode, 0)

    def test_parallel_equipment_limit_is_validated(self):
        self.synthesis_model(lambda _: {"claims": []})
        for value in (0, 7, True, "2"):
            with self.subTest(value=value):
                self.config["synthesis"]["max_parallel_equipment"] = value
                self.assertIn("synthesis.max_parallel_equipment", self.build(expected=1))

    def test_model_empty_claims_are_reported_without_calling_reviewer(self):
        requests = self.synthesis_model(lambda _: {"claims": []})
        report = self.build()
        self.assertEqual(report["state"], "published")
        self.assertEqual(report["selected_quotes"], 0)
        self.assertEqual([model for model, _ in requests], ["test-writer"])
        pages = self.equipment_pages()
        for key, original_count in (("A/C-2100", 1), ("B/C-2100", 0)):
            with self.subTest(equipment=key):
                review = self.review_for(report, key)
                self.assertEqual(
                    (review["status"], review["attempts"], review["total_blocks"]),
                    ("no_claims", 0, original_count),
                )
                self.assertIn("검토할 인용문이 없습니다", pages[key])
        self.assertEqual(self.run_cli("check", self.current).returncode, 0)

    def test_model_quote_must_come_from_its_evidence(self):
        quotes = iter(["±0.15 mm", "±0.25 mm"])

        def respond(payload):
            if payload.get("role") == "independent_reviewer":
                return {"issues": []}
            return {"claims": [{
                "id": payload["evidence"][0]["id"], "section": "적용 조건", "quote": next(quotes),
            }]}

        requests = self.synthesis_model(respond)
        first = self.build()
        self.assertEqual(first["selected_quotes"], 1)
        self.assertEqual(first["wiki_reviews"][0]["status"], "passed")
        self.assertIn("±0.15 mm", self.equipment_pages()["A/C-2100"])
        self.assertNotIn("±0.15 mm", self.equipment_pages()["B/C-2100"])
        previous = self.current.resolve()
        self.manual.write_text("개정된 진동 기준 ±0.20 mm\n", encoding="utf-8")
        self.assertIn("원문에서 확인할 수 없습니다", self.build(expected=1))
        self.assertEqual(self.current.resolve(), previous)
        self.assertEqual([model for model, _ in requests], ["test-writer", "test-reviewer", "test-writer"])

    def test_advisory_review_preserves_writer_and_flags_only_matching_claim(self):
        scope = "공장 A의 C-2100 설비에만 적용합니다."
        first = "진동 측정 전에 설비를 정지합니다."
        middle = "정지 상태에서 진동 값을 기록합니다."
        last = "작업을 마치면 정비 일지를 작성합니다."
        finding = "진동 측정 위치가 빠졌는지 확인하세요."
        quotes = (first, middle, last)
        self.manual.write_text("\n".join((scope, *quotes)) + "\n", encoding="utf-8")
        (self.manuals / "unrelated.md").write_text("미연결 작업표준서", encoding="utf-8")
        location = "진동값은 측정 위치와 함께 기록합니다."
        (self.manuals / "scope.md").write_text(location + "\n", encoding="utf-8")
        self.config["documents"]["files"]["scope.md"] = {
            "equipment_ids": ["A/C-2100"],
            "revision": "Rev.2",
        }

        def respond(payload):
            if payload.get("role") == "independent_reviewer":
                original = next(item for item in payload["original"] if location in item["text"])
                return {"issues": [{
                    "claim_id": 2, "category": "coverage", "message": finding,
                    "evidence_ids": [original["id"]],
                }]}
            original = next(item for item in payload["evidence"] if first in item["text"])
            return {"claims": [
                {"id": original["id"], "section": "점검 절차", "quote": quote}
                for quote in quotes
            ]}

        requests = self.synthesis_model(respond)
        self.config["synthesis"]["review"] = False
        baseline = self.build()
        plain_page = self.equipment_pages()["A/C-2100"]
        self.assertEqual(baseline["selected_quotes"], 3)
        self.assertFalse(baseline.get("wiki_reviews"))
        self.assertNotIn(finding, plain_page)
        self.assertEqual([model for model, _ in requests], ["test-writer"])

        del self.config["synthesis"]["review"]
        report = self.build()
        self.assertEqual(report["selected_quotes"], baseline["selected_quotes"])
        review = self.review_for(report)
        self.assertEqual((review["status"], review["attempts"]), ("needs_attention", 1))
        issue, = review["issues"]
        self.assertEqual((issue["claim_id"], issue["message"]), (2, finding))
        self.assertEqual(
            [model for model, _ in requests], ["test-writer", "test-writer", "test-reviewer"]
        )
        _, writer = requests[1]
        _, reviewer = requests[2]
        self.assertEqual(writer, requests[0][1])
        self.assertEqual(set(writer), {"equipment", "evidence"})
        self.assertEqual(writer["equipment"], "A/C-2100")
        self.assertEqual(reviewer["role"], "independent_reviewer")
        self.assertEqual(reviewer["equipment"]["key"], writer["equipment"])
        self.assertEqual(reviewer["equipment"]["plant_id"], "A")
        self.assertEqual([claim["claim_id"] for claim in reviewer["draft"]], [1, 2, 3])
        self.assertEqual(len(reviewer["original"]), 2)
        original = next(item for item in reviewer["original"] if scope in item["text"])
        supporting_note = next(item for item in reviewer["original"] if location in item["text"])
        self.assertEqual(original["id"], reviewer["draft"][1]["id"])
        self.assertEqual(original["revision"], "Rev.1")
        self.assertEqual(supporting_note["revision"], "Rev.2")
        for sentence in (scope, *quotes):
            self.assertIn(sentence, original["text"])
        self.assertNotIn("미연결 작업표준서", json.dumps(reviewer, ensure_ascii=False))

        reviewed = self.equipment_pages()
        page = reviewed["A/C-2100"]
        lines = page.splitlines()
        baseline_quotes = [line for line in plain_page.splitlines() if any(q in line for q in quotes)]
        self.assertEqual([line for line in lines if line in baseline_quotes], baseline_quotes)
        self.assertEqual(len(baseline_quotes), 3)
        note_index, = (i for i, line in enumerate(lines) if finding in line)
        self.assertEqual(lines[note_index - 1], baseline_quotes[1])
        evidence_id = supporting_note["id"]
        self.assertEqual(issue["evidence_ids"], [evidence_id])
        self.assertIn(f"[^{evidence_id}]", lines[note_index])
        self.assertIn(f"[^{evidence_id}]:", page)
        self.assertNotIn(f"[^{evidence_id}]", baseline_quotes[1])
        self.assertNotIn(finding, reviewed["B/C-2100"])
        self.assertEqual(yaml.safe_load(page.split("---", 2)[1])["status"], "draft")

    def test_legacy_workflows_keep_the_same_writer_payload_and_clean_review(self):
        def respond(payload):
            if payload.get("role") == "independent_reviewer":
                return {"issues": []}
            return {"claims": [{
                "id": payload["evidence"][0]["id"], "section": "적용 조건", "quote": "진동 기준 ±0.15 mm",
            }]}

        requests = self.synthesis_model(respond)
        for workflow in ("quotes", "reviewed"):
            with self.subTest(workflow=workflow):
                self.config["synthesis"]["workflow"] = workflow
                self.config["output"] = f"published-{workflow}"
                self.current = self.root / self.config["output"] / "current"
                report = self.build()
                self.assertEqual(report["selected_quotes"], 1)
                review = self.review_for(report)
                self.assertEqual((review["status"], review["attempts"], review["issues"]), ("passed", 1, []))
                self.assertIn("진동 기준 ±0.15 mm", self.equipment_pages()["A/C-2100"])
        self.assertEqual(
            [model for model, _ in requests], ["test-writer", "test-reviewer"] * 2
        )
        self.assertEqual(requests[0][1], requests[2][1])

    def test_invalid_or_unavailable_reviewer_still_publishes_checked_draft(self):
        quote = "진동 기준 ±0.15 mm"
        invalid = "형식이 틀린 검토 메시지"
        replies = iter([
            {"order": [1], "issues": []},
            {"issues": [{"claim_id": 999, "category": "coverage", "message": invalid}]},
            {"issues": [{"claim_id": 1, "category": "scope", "message": invalid, "evidence_ids": []}]},
            {"issues": [{"claim_id": 1, "category": "scope", "message": invalid, "evidence_ids": ["unknown"]}]},
            503,
            b"20\r\nincomplete",
            {"issues": []},
        ])

        def respond(payload):
            if payload.get("role") == "independent_reviewer":
                return next(replies)
            return {"claims": [{
                "id": payload["evidence"][0]["id"], "section": "적용 조건", "quote": quote,
            }]}

        requests = self.synthesis_model(respond)
        cases = ("legacy order", "unknown claim", "empty evidence", "unknown evidence", "HTTP 503", "truncated HTTP")
        for index, case in enumerate(cases, start=1):
            with self.subTest(case=case):
                self.manual.write_text(f"{quote}\n원문 표본 {index}\n", encoding="utf-8")
                report = self.build()
                self.assertEqual(report["state"], "published")
                self.assertEqual(report["selected_quotes"], 1)
                review = self.review_for(report)
                self.assertEqual((review["status"], review["attempts"]), ("unavailable", 1))
                page = self.equipment_pages()["A/C-2100"]
                fields = yaml.safe_load(page.split("---", 2)[1])
                self.assertEqual(fields["status"], "draft")
                self.assertEqual(fields["clew_review"]["status"], "unavailable")
                self.assertIn("## 검토 상태", page)
                self.assertIn("독립 검토를 완료하지 못했습니다", page)
                self.assertIn(quote, page)
                self.assertNotIn(invalid, page)
                reference, = (self.current / "references").glob("*.md")
                self.assertIn(f"원문 표본 {index}", reference.read_text(encoding="utf-8"))
        interrupted_bundle = self.current.resolve()
        recovered = self.build()
        self.assertEqual(recovered["state"], "published")
        self.assertEqual(recovered["wiki_reviews"][0]["status"], "passed")
        self.assertNotIn("## 검토 상태", self.equipment_pages()["A/C-2100"])
        self.assertTrue(interrupted_bundle.is_dir())
        self.assertEqual(self.build()["state"], "unchanged")
        self.assertEqual(
            [model for model, _ in requests], ["test-writer", "test-reviewer"] * (len(cases) + 1)
        )

    def test_advisory_review_reports_partial_original_coverage(self):
        quote = "판정 기준은 ±0.15 mm입니다."
        tail = "원문 말미에는 다음 정비 계획이 있습니다."
        self.manual.write_text(
            quote + "\n" + ("원문에 남겨 둘 중간 기록입니다.\n" * 500) + tail + "\n",
            encoding="utf-8",
        )

        def respond(payload):
            if payload.get("role") == "independent_reviewer":
                return {"issues": []}
            return {"claims": [{
                "id": payload["evidence"][0]["id"], "section": "적용 조건", "quote": quote,
            }]}

        requests = self.synthesis_model(respond)
        self.config["synthesis"]["review_input_chars"] = 4096
        report = self.build()
        self.assertEqual(report["selected_quotes"], 1)
        review = self.review_for(report)
        self.assertEqual((review["status"], review["attempts"]), ("partial", 1))
        self.assertEqual(len(requests), 2)
        reviewer = requests[1][1]
        self.assertFalse(reviewer["coverage"]["complete"])
        original = reviewer["original"][0]["text"]
        self.assertLessEqual(len(original), 4096)
        self.assertIn(quote, original)
        self.assertNotIn(tail, original)
        page = self.equipment_pages()["A/C-2100"]
        self.assertIn("검토 범위", page)
        self.assertIn(quote, page)
        reference, = (self.current / "references").glob("*.md")
        self.assertIn(tail, reference.read_text(encoding="utf-8"))
