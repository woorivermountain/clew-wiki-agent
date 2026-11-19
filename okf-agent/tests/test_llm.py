import json
import os
import unittest
from http.client import IncompleteRead
from io import BytesIO
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

    def test_empty_claims_require_no_provider_call(self):
        provider = Mock(spec=JsonModel)
        result = review_quotes({}, "P1/A", {}, self.lookup, [], provider=provider)
        self.assertEqual((result.status, result.attempts), ("no_claims", 0))
        self.assertEqual(result.total_blocks, 1)
        self.assertEqual(result.reviewed_blocks, 0)
        self.assertFalse(result.coverage_complete)
        provider.complete.assert_not_called()
