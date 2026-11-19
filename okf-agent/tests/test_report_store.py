import copy
import importlib.util
import json
import os
import unittest
from unittest.mock import MagicMock, patch

from clew_okf.stages.step_01_ingest.report_store import load_report

_HAS_POSTGRES = importlib.util.find_spec("psycopg") is not None


@unittest.skipUnless(_HAS_POSTGRES, "psycopg is an optional PostgreSQL dependency")
class ReportStoreTests(unittest.TestCase):
    def setUp(self):
        self.admission = {"reports": {
            "table": "reports.Approved Reports",
            "id_column": "Report ID",
            "version_column": "Report Version",
            "payload_column": 'Payload "JSON"',
        }}
        self.payload = {
            "report_id": "REPORT-1",
            "version": 1,
            "plant_id": "P1",
            "equipment_id": "EQ1",
            "event_id": "EVENT-1",
            "title": "정비 결과",
            "condition": "정상 운전 중",
            "observation": "진동 발생",
            "action": "베어링 교체",
            "result": "진동 해소",
            "status": "finalized",
            "reuse_approved": True,
            "evidence": [{
                "evidence_id": "src-" + "a" * 24,
                "revision": "R1",
                "sha256": "b" * 64,
                "quote": "원문 내용",
            }],
        }
        self.cursor = MagicMock()
        self.connection = MagicMock()
        self.connection.cursor.return_value.__enter__.return_value = self.cursor
        self.connect = MagicMock()
        self.connect.return_value.__enter__.return_value = self.connection
        self.addCleanup(patch.stopall)
        patch("psycopg.connect", self.connect).start()
        patch.dict(os.environ, {
            "CLEW_POSTGRES_DSN": "fixture-default-dsn",
            "INHERITED_REPORT_DSN": "fixture-inherited-dsn",
            "CANONICAL_REPORT_DSN": "fixture-reports-dsn",
        }).start()

    def load(self, rows, *, postgres=None, report_id="REPORT-1", version=1):
        self.cursor.fetchall.return_value = rows
        return load_report(self.admission, postgres, report_id, version)

    def test_payload_is_loaded_by_bound_identity_in_read_only_snapshot(self):
        from psycopg import IsolationLevel

        report = self.load([(self.payload,)])
        self.assertEqual((report.report_id, report.version), ("REPORT-1", 1))
        self.assertIs(self.connection.read_only, True)
        self.assertEqual(self.connection.isolation_level, IsolationLevel.REPEATABLE_READ)
        self.assertEqual(self.cursor.execute.call_args_list[0].args, ("SET LOCAL statement_timeout = '15s'",))
        query, values = self.cursor.execute.call_args.args
        self.assertEqual(
            query.as_string(),
            'SELECT "Payload ""JSON""" FROM "reports"."Approved Reports" '
            'WHERE "Report ID" = %s AND "Report Version" = %s LIMIT 2',
        )
        self.assertEqual(values, ("REPORT-1", 1))
        self.connect.assert_called_once_with("fixture-default-dsn", connect_timeout=5)

    def test_dsn_inherits_postgres_config_and_report_override(self):
        self.load([(json.dumps(self.payload),)], postgres={"dsn_env": "INHERITED_REPORT_DSN"})
        self.assertEqual(self.connect.call_args.args[0], "fixture-inherited-dsn")
        self.admission["reports"]["dsn_env"] = "CANONICAL_REPORT_DSN"
        self.load([(self.payload,)], postgres={"dsn_env": "INHERITED_REPORT_DSN"})
        self.assertEqual(self.connect.call_args.args[0], "fixture-reports-dsn")

    def test_missing_duplicate_or_mismatched_identity_is_rejected(self):
        for rows in ([], [(self.payload,), (self.payload,)]):
            with self.subTest(row_count=len(rows)):
                with self.assertRaisesRegex(RuntimeError, "exactly one record"):
                    self.load(rows)
        for identity in ({"report_id": "OTHER-1"}, {"version": 2}):
            with self.subTest(identity=identity):
                with self.assertRaisesRegex(RuntimeError, "identity"):
                    self.load([({**self.payload, **identity},)])

    def test_invalid_or_coerced_canonical_payload_is_rejected_without_contents(self):
        values = [
            None, [], "{invalid secret-report-body", {**self.payload, "version": "1"},
            {**self.payload, "reuse_approved": "true"}, {**self.payload, "reuse_approved": False},
            {**self.payload, "status": "draft"}, {**self.payload, "unknown": "secret-report-body"},
        ]
        nested = copy.deepcopy(self.payload)
        nested["evidence"][0]["sha256"] = "secret-report-body"
        values.append(nested)
        for value in values:
            with self.subTest(payload_type=type(value).__name__):
                with self.assertRaisesRegex(RuntimeError, "payload is invalid") as error:
                    self.load([(value,)])
                self.assertNotIn("secret-report-body", str(error.exception))

    def test_configuration_and_request_validation_happen_before_connect(self):
        configurations = [
            {}, {"reports": None},
            {"reports": {**self.admission["reports"], "unexpected": True}},
            {"reports": {**self.admission["reports"], "table": "unqualified"}},
            {"reports": {**self.admission["reports"], "id_column": ""}},
            {"reports": {**self.admission["reports"], "dsn_env": "bad env"}},
        ]
        for admission in configurations:
            with self.subTest(admission=admission):
                with self.assertRaises(ValueError):
                    load_report(admission, None, "REPORT-1", 1)
        for report_id, version in (("", 1), ("REPORT-1", True), ("REPORT-1", 0), ("REPORT-1", "1")):
            with self.subTest(report_id=report_id, version=version):
                with self.assertRaises(ValueError):
                    load_report(self.admission, None, report_id, version)
        self.connect.assert_not_called()

    def test_database_error_is_redacted(self):
        import psycopg

        self.cursor.execute.side_effect = psycopg.OperationalError(
            "database failed: secret-password private-db.internal"
        )
        with self.assertRaisesRegex(RuntimeError, "^Canonical report retrieval failed$"):
            self.load([(self.payload,)])


if __name__ == "__main__":
    unittest.main()
