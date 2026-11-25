import json
import os
import unittest
from http.client import IncompleteRead
from io import BytesIO
from threading import Event
from unittest.mock import Mock, patch

from clew_okf.llm import JsonModel
from clew_okf.domain.model import Block, Source
from clew_okf.stages.step_05_review.review import review_quotes
from clew_okf.stages.step_04_compose.synthesis import configure_synthesis, curate


class LlmTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
            "model": "writer-model",
            "review_model": "reviewer-model",
            "timeout_seconds": 13,
        }
        self.quote = "진동 기준 ±0.15 mm"
        self.block = Block("page 2", self.quote)
        self.evidence_id = "src-a"
        self.source = Source(
            "docs:manual.pdf", "Manual", "document", "file:///manual.pdf", (self.block,),
            revision="Rev.3",
        )
        self.lookup = {self.evidence_id: (self.source, self.block)}
        self.claims = [(self.evidence_id, "적용 조건", self.quote)]

    @staticmethod
    def response(result):
        return BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(result)}}]}).encode())

    def test_chat_completions_preserves_request_payload_and_uses_one_opener(self):
        opener = Mock()
        opener.open.side_effect = [self.response({"claims": []}), self.response({"issues": []})]
        with (
            patch("clew_okf.llm.build_opener", return_value=opener) as build_opener,
            patch.dict(os.environ, {"CLEW_TEST_KEY": "fixture-token"}),
        ):
            provider = configure_synthesis({**self.config, "api_key_env": "CLEW_TEST_KEY"})
            self.assertEqual(provider.complete("Write excerpts", {"근거": self.quote}), {"claims": []})
            self.assertEqual(
                provider.complete("Review excerpts", {"draft": [self.quote]}, model="reviewer-model"),
                {"issues": []},
            )
        build_opener.assert_called_once()
        for call, (model, prompt, user_data) in zip(opener.open.call_args_list, [
            ("writer-model", "Write excerpts", {"근거": self.quote}),
            ("reviewer-model", "Review excerpts", {"draft": [self.quote]}),
        ], strict=True):
            request, = call.args
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.full_url, self.config["endpoint"])
            self.assertEqual(call.kwargs["timeout"], 13)
            self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
            self.assertEqual(
                json.loads(request.data),
                {"model": model, "messages": [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(user_data, ensure_ascii=False)},
                ]},
            )

    def test_json_mode_requests_an_object(self):
        opener = Mock()
        opener.open.return_value = self.response({"claims": []})
        with patch("clew_okf.llm.build_opener", return_value=opener):
            provider = configure_synthesis({**self.config, "json_mode": True})
            self.assertEqual(provider.complete("Return JSON", {}), {"claims": []})
        request, = opener.open.call_args.args
        self.assertEqual(json.loads(request.data)["response_format"], {"type": "json_object"})

    def test_malformed_key_is_rejected_before_network_connection(self):
        with (
            patch.dict(os.environ, {"CLEW_TEST_KEY": "붙여넣은 안내 문구"}),
            patch("clew_okf.llm.build_opener") as build_opener,
            self.assertRaisesRegex(ValueError, "API 키"),
        ):
            configure_synthesis({**self.config, "api_key_env": "CLEW_TEST_KEY"})
        build_opener.assert_not_called()

    def test_truncated_http_read_degrades_to_advisory_unavailable(self):
        response = Mock(wraps=self.response({"issues": []}))
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = IncompleteRead(b'{"choices": [')
        opener = Mock()
        opener.open.return_value = response
        with patch("clew_okf.llm.build_opener", return_value=opener):
            provider = configure_synthesis(self.config)
            result = review_quotes(
                self.config, "P1/A", {}, self.lookup, self.claims, provider=provider
            )
        self.assertEqual((result.status, result.attempts), ("unavailable", 1))
        self.assertEqual(result.issues, [])
        self.assertIn("HTTP 응답", result.error)
        opener.open.assert_called_once()

    def test_incomplete_content_length_rejects_otherwise_valid_json(self):
        response = self.response({"issues": []})
        response.length = 3
        opener = Mock()
        opener.open.return_value = response
        with patch("clew_okf.llm.build_opener", return_value=opener):
            provider = configure_synthesis(self.config)
            with self.assertRaisesRegex(RuntimeError, "HTTP 응답"):
                provider.complete("Review excerpts", {"draft": [self.quote]})

    def test_writer_and_reviewer_support_one_injected_provider(self):
        provider = Mock(spec=JsonModel)
        provider.complete.side_effect = [
            {"claims": [{"id": self.evidence_id, "section": "적용 조건", "quote": self.quote}]},
            {"issues": []},
        ]
        evidence = [(self.evidence_id, self.block)]
        claims = curate({}, "P1/A", evidence, provider=provider)
        result = review_quotes(
            {"review_model": "second-model"}, "P1/A", {}, self.lookup, claims, provider=provider
        )
        self.assertEqual(claims, self.claims)
        self.assertEqual((result.status, result.attempts), ("passed", 1))
        writer, reviewer = provider.complete.call_args_list
        self.assertEqual(writer.args[1], {
            "equipment": "P1/A", "evidence": [{"id": self.evidence_id, "text": self.quote}],
        })
        self.assertEqual(writer.kwargs, {})
        self.assertEqual(reviewer.kwargs, {"model": "second-model"})
        self.assertEqual(reviewer.args[1]["role"], "independent_reviewer")
        self.assertEqual(reviewer.args[1]["original"][0]["revision"], "Rev.3")

    def test_parallel_writer_preserves_source_order_and_first_section(self):
        first_started = Event()
        second_finished = Event()
        first = "first"
        evidence = [
            (first, self.block),
            *[(f"other-{i}", self.block) for i in range(3)],
            (first, self.block),
            ("last", self.block),
        ]

        def complete(_system, payload):
            batch = payload["evidence"]
            if len(batch) == 4:
                first_started.set()
                if not second_finished.wait(3):
                    raise AssertionError("second batch did not complete concurrently")
                section = "적용 조건"
            else:
                if not first_started.wait(3):
                    raise AssertionError("first batch did not run concurrently")
                section = "주의 사항"
                second_finished.set()
            return {"claims": [
                {"id": row["id"], "section": section, "quote": row["text"]}
                for row in batch
            ]}

        provider = Mock(spec=JsonModel)
        provider.complete.side_effect = complete
        claims = curate(
            {"max_parallel_requests": 2}, "P1/A", evidence, provider=provider
        )
        self.assertEqual([claim[0] for claim in claims], [first, "other-0", "other-1", "other-2", "last"])
        self.assertEqual(claims[0], (first, "적용 조건", self.quote))
        self.assertEqual(claims[-1], ("last", "주의 사항", self.quote))

    def test_writer_rejects_invalid_parallel_request_limits(self):
        for value in (True, 0, 9, 2.0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "max_parallel_requests"):
                configure_synthesis({**self.config, "max_parallel_requests": value})

    def test_writer_retries_an_invalid_quote_without_relaxing_validation(self):
        provider = Mock(spec=JsonModel)
        valid = {"claims": [{"id": self.evidence_id, "section": "적용 조건", "quote": self.quote}]}
        bad = {"claims": [{**valid["claims"][0], "quote": self.quote + " 설명"}]}
        provider.complete.side_effect = [bad, valid]
        evidence = [(self.evidence_id, self.block)]
        config = {"max_validation_attempts": 2}
        self.assertEqual(curate(config, "P1/A", evidence, provider=provider), self.claims)
        first, retry = provider.complete.call_args_list
        self.assertEqual(first.args[1], retry.args[1])
        self.assertIn("failed validation", retry.args[0])

        provider.reset_mock()
        provider.complete.side_effect = [bad, bad]
        with self.assertRaisesRegex(RuntimeError, "인용문"):
            curate(config, "P1/A", evidence, provider=provider)
        self.assertEqual(provider.complete.call_count, 2)

    def test_writer_does_not_retry_provider_errors(self):
        provider = Mock(spec=JsonModel)
        provider.complete.side_effect = RuntimeError("provider error")
        with self.assertRaisesRegex(RuntimeError, "provider error"):
            curate({"max_validation_attempts": 2}, "P1/A", [(self.evidence_id, self.block)], provider=provider)
        provider.complete.assert_called_once()

    def test_candidate_fallback_returns_a_verbatim_paragraph_with_original_evidence_id(self):
        quote = "기준은 0.15 mm입니다.\n교체한 부품에만 적용합니다."
        text = f"일반 설명입니다.\n\n{quote}"
        evidence = [(self.evidence_id, Block("page 2", text))]
        provider = Mock(spec=JsonModel)
        provider.complete.side_effect = [
            {"claims": [{"id": self.evidence_id, "section": "적용 조건", "quote": "의역"}]},
            RuntimeError("모델이 빈 본문을 반환했습니다."),
            {"claims": [{"id": "candidate-2", "section": "적용 조건"}]},
        ]
        claims = curate(
            {"max_validation_attempts": 2, "quote_fallback": "candidate_ids"},
            "P1/A",
            evidence,
            provider=provider,
        )
        self.assertEqual(claims, [(self.evidence_id, "적용 조건", quote)])
        self.assertEqual(provider.complete.call_count, 3)
        payload = provider.complete.call_args.args[1]
        self.assertEqual(payload, {"equipment": "P1/A", "candidate_evidence": [
            {"id": "candidate-1", "source_id": self.evidence_id, "text": "일반 설명입니다."},
            {"id": "candidate-2", "source_id": self.evidence_id, "text": quote},
        ]})

    def test_candidate_fallback_rejects_unknown_ids_and_preserves_transport_errors(self):
        provider = Mock(spec=JsonModel)
        evidence = [(self.evidence_id, self.block)]
        config = {"quote_fallback": "candidate_ids"}
        provider.complete.side_effect = [
            {"claims": None},
            {"claims": [{"id": "unknown", "section": "적용 조건"}]},
        ]
        with self.assertRaisesRegex(RuntimeError, "제공되지 않은 인용 후보"):
            curate(config, "P1/A", evidence, provider=provider)
        self.assertEqual(provider.complete.call_count, 2)

        provider.reset_mock()
        provider.complete.side_effect = RuntimeError("모델 요청이 실패했습니다 (HTTP 401).")
        with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
            curate(config, "P1/A", evidence, provider=provider)
        provider.complete.assert_called_once()

    def test_empty_claims_require_no_provider_call(self):
        provider = Mock(spec=JsonModel)
        result = review_quotes({}, "P1/A", {}, self.lookup, [], provider=provider)
        self.assertEqual((result.status, result.attempts), ("no_claims", 0))
        self.assertEqual(result.total_blocks, 1)
        self.assertEqual(result.reviewed_blocks, 0)
        self.assertFalse(result.coverage_complete)
        provider.complete.assert_not_called()
