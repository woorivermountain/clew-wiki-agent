"""Read-only bootstrap planning uses catalog fixtures, never a live database."""

import copy
import importlib.util
import os
import unittest
from unittest.mock import MagicMock, patch

from clew_okf.stages.step_01_ingest.bootstrap import scan_database


@unittest.skipUnless(importlib.util.find_spec("psycopg"), "optional PostgreSQL dependency")
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.config = {"postgres": {"name": "factory", "dsn_env": "BOOT_DB"},
                       "bootstrap": {"schemas": ["factory"]}}
        self.catalog = [(10, "factory", "assets", "r", "설비 마스터", True, True)]
        self.columns = {10: [("asset_no", "text", False, "설비 ID"),
                             ("plant_id", "text", False, None), ("name", "text", True, None)]}
        self.constraints = {10: [("p", "assets_pkey", "PRIMARY KEY (asset_no)", ["asset_no"], None, [])]}
        self.jobs = {}
        self.queries = []
        self.connections = []
        env = patch.dict(os.environ, {"BOOT_DB": "private-catalog-dsn", "CANON_DB": "private-canonical-dsn",
                                      "RAW_DB": "private-raw-dsn"})
        env.start()
        self.addCleanup(env.stop)
        connection_patch = patch("psycopg.connect", side_effect=self.connect)
        self.connect_mock = connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def connect(self, dsn, **kwargs):
        cursor = MagicMock()
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connection.__enter__.return_value = connection
        self.connections.append(connection)

        def execute(query, params=None):
            text = query if isinstance(query, str) else query.as_string()
            self.queries.append((dsn, text, params))
            if "FROM pg_catalog.pg_class AS c" in text:
                cursor.fetchall.return_value = self.catalog
            elif "FROM pg_catalog.pg_attribute AS a" in text and "a.attrelid = %s::oid" in text:
                cursor.fetchall.return_value = self.columns.get(params[0], [])
            elif "FROM pg_catalog.pg_constraint AS con" in text:
                cursor.fetchall.return_value = self.constraints.get(params[0], [])
            elif text.startswith("SELECT "):
                cursor.fetchall.return_value = self.jobs.get(dsn, [])
        cursor.execute.side_effect = execute
        return connection

    def call(self):
        return scan_database(self.config)

    def test_catalog_only_ready_plan_metadata_constraints_and_no_mutation(self):
        self.constraints[10].append(("f", "plant_fk", "FOREIGN KEY (plant_id) REFERENCES plants(id)",
                                    ["plant_id"], "factory.plants", ["id"]))
        before = copy.deepcopy(self.config)
        result = self.call()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["proposed_postgres"]["equipment"], {
            "table": "factory.assets", "columns": {"equipment_id": "asset_no", "name": "name", "plant_id": "plant_id"}})
        table = result["tables"][0]
        self.assertEqual(table["primary_key"], ["asset_no"])
        self.assertEqual(table["foreign_keys"][0]["referenced_table"], "factory.plants")
        self.assertEqual(table["columns"][0], {"name": "asset_no", "type": "text", "nullable": False, "comment": "설비 ID"})
        self.assertEqual(result["report_jobs"], [])
        self.assertEqual(self.config, before)
        self.assertEqual(result["counts"]["columns"], 3)
        self.assertTrue(all("SELECT *" not in q and 'FROM "factory"."assets"' not in q for _, q, _ in self.queries))
        from psycopg import IsolationLevel
        self.assertTrue(self.connections[0].read_only)
        self.assertEqual(self.connections[0].isolation_level, IsolationLevel.REPEATABLE_READ)
        self.assertEqual(self.queries[0][1], "SET LOCAL statement_timeout = '15s'")

    def test_missing_allowlist_holds_before_connect_but_can_derive_explicit_schemas(self):
        del self.config["bootstrap"]["schemas"]
        self.assertEqual(self.call()["status"], "hold")
        self.connect_mock.assert_not_called()
        self.config["postgres"]["tables"] = ["factory.assets"]
        self.assertEqual(self.call()["status"], "ready")
        catalog_call = next(q for q in self.queries if "FROM pg_catalog.pg_class AS c" in q[1])
        self.assertEqual(catalog_call[2], (["factory"], 201))

    def test_schema_names_are_bound_values_not_sql_fragments(self):
        schema = 'factory"; SELECT secret; --'
        self.config["bootstrap"]["schemas"] = [schema]
        self.catalog[0] = (10, schema, "assets", "r", None, True, True)
        result = self.call()
        self.assertEqual(result["status"], "ready")
        _, query, params = next(q for q in self.queries if "FROM pg_catalog.pg_class AS c" in q[1])
        self.assertNotIn(schema, query)
        self.assertEqual(params[0], [schema])

    def test_table_budget_holds_without_reading_partial_rows_or_columns(self):
        self.config["bootstrap"]["max_tables"] = 1
        self.catalog.append((11, "factory", "extra", "r", None, True, True))
        result = self.call()
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["findings"][-1]["code"], "table_limit")
        self.assertEqual(result["report_jobs"], [])
        self.assertFalse(any("FROM pg_catalog.pg_attribute AS a" in q for _, q, _ in self.queries))

    def test_ambiguous_equipment_is_not_guessed_and_configured_mapping_is_authoritative(self):
        self.catalog.append((11, "factory", "other_assets", "r", None, True, True))
        self.columns[11] = [("equipment_id", "text", False, None)]
        self.constraints[11] = [("p", "other_pkey", "PRIMARY KEY (equipment_id)", ["equipment_id"], None, [])]
        result = self.call()
        self.assertEqual(result["status"], "hold")
        self.assertEqual(result["counts"]["equipment_candidates"], 2)
        self.assertIn("factory.other_assets", result["findings"][0]["message"])
        explicit = {"table": "factory.assets", "columns": {"equipment_id": "asset_no"},
                    "max_rows": 42, "filters": {"enabled": True}}
        self.config["postgres"]["equipment"] = explicit
        self.config["postgres"]["records"] = [{"table": "factory.audit", "id_column": "id", "columns": ["body"]}]
        result = self.call()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["proposed_postgres"]["equipment"], explicit)
        self.assertEqual(result["proposed_postgres"]["records"], self.config["postgres"]["records"])

    def test_ambiguous_id_columns_missing_inventory_and_permissions_hold(self):
        variants = [[], [("asset_no", "text", False, None), ("equipment_id", "text", False, None)]]
        for columns in variants:
            with self.subTest(columns=columns):
                self.columns[10] = columns
                self.assertEqual(self.call()["status"], "hold")
        self.columns[10] = [("asset_no", "text", False, None)]
        self.catalog[0] = (*self.catalog[0][:6], False)
        self.assertEqual(self.call()["status"], "hold")

    def configure_reports(self):
        self.config["admission"] = {
            "reports": {"table": 'reports.Canonical "Reports"', "id_column": 'Report "ID"',
                        "version_column": "version", "payload_column": "payload", "dsn_env": "CANON_DB"},
            "mapping": {"database": {"table": "reports.raw", "id_column": "id", "version_column": "version",
                                      "status_column": "status", "approval_column": "approved", "dsn_env": "RAW_DB"}}}

    def test_approved_report_identity_queries_support_separate_dsns_and_stable_jobs(self):
        self.configure_reports()
        self.jobs = {"private-canonical-dsn": [("R-2", 2), ("R-2", 1)], "private-raw-dsn": [("R-1", 1)]}
        result = self.call()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["report_jobs"], [
            {"report_id": "R-1", "version": 1, "source": "database"},
            {"report_id": "R-2", "version": 1, "source": "canonical"},
            {"report_id": "R-2", "version": 2, "source": "canonical"}])
        queries = [q for _, q, _ in self.queries if 'FROM "reports".' in q]
        self.assertEqual(len(queries), 2)
        self.assertIn('SELECT "Report ""ID""", "version" FROM "reports"."Canonical ""Reports"""', queries[0])
        self.assertIn("-> 'reuse_approved' = 'true'::jsonb", queries[0])
        self.assertIn('"approved" IS TRUE', queries[1])
        self.assertTrue(all('"payload",' not in q and "SELECT *" not in q for q in queries))
        self.assertTrue(all(c.read_only for c in self.connections))
        self.assertNotIn("private-", str(result))
        self.assertNotIn("records", result["proposed_postgres"])

    def test_same_dsn_reuses_readonly_connection_and_import_false_skips_report_rows(self):
        self.configure_reports()
        self.config["admission"]["reports"]["dsn_env"] = "BOOT_DB"
        self.config["admission"]["mapping"]["database"]["dsn_env"] = "BOOT_DB"
        self.assertEqual(self.call()["status"], "ready")
        self.assertEqual(self.connect_mock.call_count, 1)
        self.connect_mock.reset_mock()
        self.queries.clear()
        self.config["bootstrap"]["import_reports"] = False
        self.assertEqual(self.call()["report_jobs"], [])
        self.assertEqual(self.connect_mock.call_count, 1)
        self.assertFalse(any('FROM "reports".' in q for _, q, _ in self.queries))

    def test_report_limits_duplicate_identity_and_invalid_ids_never_return_partial_jobs(self):
        self.configure_reports()
        self.config["bootstrap"]["max_reports"] = 1
        for data in ([("R-1", 1), ("R-2", 1)], [("", 1)], [("R-1", True)], [("R-1", "1")]):
            with self.subTest(data=data):
                self.jobs = {"private-canonical-dsn": data}
                result = self.call()
                self.assertEqual(result["status"], "hold")
                self.assertEqual(result["report_jobs"], [])
        self.config["bootstrap"]["max_reports"] = 10
        self.jobs = {"private-canonical-dsn": [("R-1", 1)], "private-raw-dsn": [("R-1", 1)]}
        self.assertEqual(self.call()["findings"][-1]["code"], "report_identity_ambiguous")

    def test_composite_plant_equipment_primary_key_selects_factory_master(self):
        self.columns[10] = [("equipment_id", "text", False, None), ("plant_code", "text", False, None)]
        self.constraints[10] = [("p", "assets_pkey", "PRIMARY KEY (plant_code, equipment_id)",
                                 ["plant_code", "equipment_id"], None, [])]
        self.catalog.append((11, "factory", "current_maintenance", "r", None, True, True))
        self.columns[11] = [("record_id", "text", False, None), ("equipment_id", "text", False, None)]
        self.constraints[11] = [("p", "maintenance_pkey", "PRIMARY KEY (record_id)", ["record_id"], None, [])]
        result = self.call()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["proposed_postgres"]["equipment"], {
            "table": "factory.assets", "columns": {"equipment_id": "equipment_id", "plant_id": "plant_code"}})
        self.assertEqual(result["counts"]["equipment_candidates"], 1)

    def test_single_conventional_column_without_exact_primary_key_is_not_guessed(self):
        for primary in ([], ["asset_no", "event_id"], ["record_id"]):
            with self.subTest(primary=primary):
                self.constraints[10] = [("p", "pk", "PK", primary, None, [])] if primary else []
                result = self.call()
                self.assertEqual(result["status"], "hold")
                self.assertNotIn("equipment", result["proposed_postgres"])

    def test_unique_incoming_foreign_key_parent_resolves_strong_candidates(self):
        self.catalog.append((11, "factory", "current_maintenance", "r", None, True, True))
        self.columns[11] = [("equipment_id", "text", False, None)]
        self.constraints[11] = [
            ("p", "maintenance_pkey", "PRIMARY KEY (equipment_id)", ["equipment_id"], None, []),
            ("f", "asset_fk", "FOREIGN KEY (equipment_id) REFERENCES assets(asset_no)",
             ["equipment_id"], "factory.assets", ["asset_no"])]
        result = self.call()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["proposed_postgres"]["equipment"]["table"], "factory.assets")
        self.constraints[10].append(("f", "reverse_fk", "FK", ["asset_no"], "factory.current_maintenance", ["equipment_id"]))
        self.assertEqual(self.call()["status"], "hold")

    def test_explicit_record_relations_and_denied_candidates_are_excluded(self):
        self.catalog.append((11, "factory", "history", "r", None, True, True))
        self.columns[11] = [("equipment_id", "text", False, None)]
        self.constraints[11] = [("p", "history_pk", "PK", ["equipment_id"], None, [])]
        self.config["postgres"]["records"] = [{"table": "factory.history", "id_column": "equipment_id", "columns": ["body"]}]
        self.assertEqual(self.call()["proposed_postgres"]["equipment"]["table"], "factory.assets")
        del self.config["postgres"]["records"]
        self.catalog[1] = (*self.catalog[1][:6], False)
        self.assertEqual(self.call()["proposed_postgres"]["equipment"]["table"], "factory.assets")

    def test_declared_report_registry_is_not_an_inferred_equipment_master(self):
        self.config["admission"] = {"mapping": {"database": {
            "table": "factory.assets", "id_column": "id", "version_column": "version",
            "status_column": "status", "approval_column": "approved"}}}
        self.assertEqual(self.call()["findings"][-1]["code"], "equipment_missing")

    def test_invalid_options_and_database_errors_hold_without_exposing_credentials(self):
        for changes in ({"schemas": []}, {"max_tables": True}, {"max_reports": 0}, {"import_reports": "true"}):
            with self.subTest(changes=changes):
                self.config["bootstrap"] = {"schemas": ["factory"], **changes}
                self.assertEqual(self.call()["status"], "hold")
        self.config["bootstrap"] = {"schemas": ["factory"]}
        import psycopg
        self.connect_mock.side_effect = psycopg.OperationalError("private-catalog-dsn password")
        result = self.call()
        self.assertEqual(result["findings"][-1]["code"], "postgres_read_failed")
        self.assertNotIn("private", str(result))


if __name__ == "__main__":
    unittest.main()
