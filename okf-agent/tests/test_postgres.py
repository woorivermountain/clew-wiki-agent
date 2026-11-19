import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml

from clew_okf.cli import _build
from clew_okf.domain.model import Block, Source
from clew_okf.stages.step_01_ingest.postgres import _equipment_config, _load_equipment, _load_records, _record_specs, _select_rows

try:
    from psycopg import sql
except ImportError:
    sql = None


class PostgresTests(unittest.TestCase):
    def test_filters_validate_both_equipment_and_records(self):
        inventory = {"table": "wiki.assets", "columns": {"equipment_id": "asset_no"}}
        record = {"table": "wiki.history", "id_column": "record_id", "columns": ["finding"]}
        for filters in (None, [1], {"col": None}, {"col": [1]}, {"col": {"key": 1}}, {"": "x"}):
            with self.subTest(filters=filters):
                with self.assertRaisesRegex(ValueError, "equipment.filters"):
                    _equipment_config({**inventory, "filters": filters})
                with self.assertRaisesRegex(ValueError, "records.filters"):
                    _record_specs([{**record, "filters": filters}])

        self.assertEqual(_equipment_config(inventory)[-1], {})
        self.assertEqual(_record_specs([record])[0].filters, {})
        values = {"enabled": True, "priority": 2, "updated_on": date(2026, 11, 18)}
        self.assertEqual(_equipment_config({**inventory, "filters": values})[-1], values)
        self.assertEqual(_record_specs([{**record, "filters": values}])[0].filters, values)

    def test_record_synthesis_flag_is_strict_and_propagates(self):
        config = {
            "table": "wiki.history", "id_column": "case_id", "columns": ["finding"],
            "equipment_column": "asset_id", "revision_column": "revision",
        }
        for synthesize in ("false", 0, 1, None):
            with self.subTest(value=synthesize):
                with self.assertRaisesRegex(ValueError, "synthesize must be a boolean"):
                    _record_specs([{**config, "synthesize": synthesize}])

        for value in (True, False):
            with self.subTest(synthesize=value):
                settings = config if value else {**config, "synthesize": False}
                spec = _record_specs([settings])[0]
                with patch("clew_okf.stages.step_01_ingest.postgres._select_rows", return_value=[
                    ("CASE-1", "Observed pressure 7.0 bar", "C-2100", "LOG1"),
                    ("CASE-2", "Observed pressure 7.1 bar", "C-2100", "LOG2"),
                ]):
                    sources = _load_records(None, None, "factory", spec)
                self.assertEqual(len(sources), 2)
                self.assertTrue(all(source.synthesize is value for source in sources))
                self.assertEqual(sources[0].equipment_ids, ("C-2100",))
                self.assertEqual(sources[0].blocks[0].text, "Observed pressure 7.0 bar")
        self.assertTrue(Source("source", "Source", "document", "file:///source", ()).synthesize)

    @unittest.skipIf(sql is None, "psycopg is an optional PostgreSQL dependency")
    def test_query_filters_quote_columns_and_bind_values_before_limit(self):
        cursor = Mock()
        cursor.fetchall.return_value = [("C-2100",)]
        args = (cursor, sql, ("wiki", "Assets"), ("asset_no",), "asset_no", 3, "equipment")

        self.assertEqual(_select_rows(*args, filters={'Status "Code"': "current 'approved'", "active": True}), [
            ("C-2100",),
        ])
        query, params = cursor.execute.call_args.args
        self.assertEqual(
            query.as_string(),
            'SELECT "asset_no" FROM "wiki"."Assets" WHERE "Status ""Code""" = %s AND "active" = %s '
            'ORDER BY "asset_no" LIMIT %s',
        )
        self.assertEqual(params, ("current 'approved'", True, 4))

        _select_rows(*args, filters={})
        query, params = cursor.execute.call_args.args
        self.assertEqual(query.as_string(), 'SELECT "asset_no" FROM "wiki"."Assets" ORDER BY "asset_no" LIMIT %s')
        self.assertEqual(params, (4,))

    def test_empty_configured_inventory_keeps_existing_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            manuals = root / "originals"
            manuals.mkdir()
            (manuals / "manual.md").write_text("Inspection threshold\n", encoding="utf-8")
            (root / "equipment.csv").write_text("equipment_id,name\nC-2100,Compressor\n", encoding="utf-8")
            config_path = root / "okf.yaml"
            config = {"output": "published", "documents": {"root": "originals"}}
            config_path.write_text(yaml.safe_dump({**config, "equipment_csv": "equipment.csv"}), encoding="utf-8")
            _build(config_path)

            current = root / "published/current"
            published = current.readlink()
            snapshots = root / "published/snapshots"
            previous_snapshots = set(snapshots.iterdir())
            config["postgres"] = {
                "tables": ["wiki.history"],
                "equipment": {
                    "table": "wiki.assets",
                    "columns": {"equipment_id": "asset_no"},
                    "filters": {"plant": "nonexistent"},
                },
            }
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            catalog = Source(
                "postgres:test/wiki/history", "wiki.history", "postgres", "postgresql://test/wiki/history",
                (Block("table", "history"),),
            )

            def postgres_with_empty_view(settings):
                with patch("clew_okf.stages.step_01_ingest.postgres._select_rows", return_value=[]):
                    rows = _load_equipment(None, None, *_equipment_config(settings["equipment"]))
                return [catalog], rows

            with (
                patch("clew_okf.workflows.compiler.load_postgres", side_effect=postgres_with_empty_view),
                self.assertRaisesRegex(RuntimeError, r"returned no rows: wiki\.assets"),
            ):
                _build(config_path)

            self.assertEqual(current.readlink(), published)
            self.assertEqual(set(snapshots.iterdir()), previous_snapshots)


if __name__ == "__main__":
    unittest.main()
