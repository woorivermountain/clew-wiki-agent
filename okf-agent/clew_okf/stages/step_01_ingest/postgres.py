"""Read an allowlisted PostgreSQL catalog and equipment inventory."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from clew_okf.domain.model import Block, Source

_DEFAULT_DSN_ENV = "CLEW_POSTGRES_DSN"
_EQUIPMENT_FIELDS = ("equipment_id", "name", "process_id", "plant_id", "model", "aliases")
_RECORD_OPTIONAL_COLUMNS = ("equipment_column", "plant_column", "revision_column", "title_column")
_RECORD_FIELDS = {"table", "id_column", "columns", "max_rows", "filters", "synthesize", *_RECORD_OPTIONAL_COLUMNS}
_RowFilters = dict[str, str | int | float | bool | date]


@dataclass(frozen=True, slots=True)
class _RecordSpec:
    table: tuple[str, str]
    id_column: str
    columns: tuple[str, ...]
    equipment_column: str | None
    plant_column: str | None
    revision_column: str | None
    title_column: str | None
    max_rows: int
    filters: _RowFilters
    synthesize: bool = True

_TABLE_QUERY = """
SELECT relation.oid, pg_catalog.obj_description(relation.oid, 'pg_class')
FROM pg_catalog.pg_class AS relation
JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
WHERE namespace.nspname = %s
  AND relation.relname = %s
  AND relation.relkind IN ('r', 'p', 'v', 'm', 'f')
  AND pg_catalog.has_schema_privilege(namespace.oid, 'USAGE')
"""

_COLUMNS_QUERY = """
SELECT
    attribute.attname,
    pg_catalog.format_type(attribute.atttypid, attribute.atttypmod),
    attribute.attnotnull OR (data_type.typtype = 'd' AND data_type.typnotnull),
    pg_catalog.col_description(attribute.attrelid, attribute.attnum)
FROM pg_catalog.pg_attribute AS attribute
JOIN pg_catalog.pg_type AS data_type ON data_type.oid = attribute.atttypid
WHERE attribute.attrelid = %s::oid
  AND attribute.attnum > 0
  AND NOT attribute.attisdropped
ORDER BY attribute.attnum
"""

_CONSTRAINTS_QUERY = """
SELECT
    constraint_row.conname,
    pg_catalog.pg_get_constraintdef(constraint_row.oid)
FROM pg_catalog.pg_constraint AS constraint_row
WHERE constraint_row.conrelid = %s::oid
  AND constraint_row.contype IN ('p', 'f')
ORDER BY constraint_row.contype DESC, constraint_row.conname
"""


def load_postgres(config: dict) -> tuple[list[Source], list[dict[str, str]]]:
    """Load configured table metadata, equipment, and projected evidence rows."""
    name = config.get("name", "postgres")
    if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."}:
        raise ValueError("postgres.name must use only letters, digits, underscores, dots, or hyphens")
    dsn_env = config.get("dsn_env", _DEFAULT_DSN_ENV)
    if not isinstance(dsn_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dsn_env):
        raise ValueError("postgres.dsn_env must be an environment variable name")

    table_specs = config.get("tables", [])
    if not isinstance(table_specs, list):
        raise ValueError("postgres.tables must be a list of exact schema.table names")
    tables = sorted({_table_name(value) for value in table_specs})
    equipment = _equipment_config(config.get("equipment"))
    records = _record_specs(config.get("records", []))

    dsn = os.environ.get(dsn_env)
    if not dsn or not dsn.strip():
        raise ValueError(f"PostgreSQL connection environment variable {dsn_env} is missing or empty")

    try:
        import psycopg
        from psycopg import IsolationLevel, sql
    except ImportError:
        raise RuntimeError("PostgreSQL support requires psycopg with a working libpq installation") from None

    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.read_only = True
            connection.isolation_level = IsolationLevel.REPEATABLE_READ
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '15s'")
                cursor.execute("SET LOCAL search_path = pg_catalog")
                sources = [_load_table(cursor, name, *table) for table in tables]
                equipment_rows = _load_equipment(cursor, sql, *equipment) if equipment else []
                for spec in records:
                    sources.extend(_load_records(cursor, sql, name, spec))
    except psycopg.Error as error:
        # Driver errors can include connection strings, usernames, and hostnames.
        state = error.sqlstate
        details = f" (SQLSTATE {state})" if state and re.fullmatch(r"[A-Z0-9]{5}", state) else ""
        raise RuntimeError(f"PostgreSQL connection or read failed{details}") from None

    return sources, equipment_rows


def postgres_resource(name: str, table: str) -> str:
    """Return the logical source URI for an exact configured table name."""
    schema, relation = _table_name(table)
    return f"postgresql://{_table_path(name, schema, relation)}"


def _table_path(name: str, schema: str, table: str) -> str:
    return "/".join(quote(part, safe="") for part in (name, schema, table))


def _table_name(value: object) -> tuple[str, str]:
    if isinstance(value, str):
        parts = value.split(".")
        if len(parts) == 2 and all(_identifier(part) for part in parts):
            return parts[0], parts[1]
    raise ValueError("PostgreSQL tables must use exact schema.table names")


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value.strip() == value and "\0" not in value


def _row_filters(value: object, label: str) -> _RowFilters:
    if not isinstance(value, dict) or any(
        not _identifier(column) or not isinstance(expected, (str, int, float, bool, date))
        for column, expected in value.items()
    ):
        raise ValueError(f"{label}.filters must map column names to non-null scalar values")
    return value


def _equipment_config(
    config: object,
) -> tuple[tuple[str, str], dict[str, str], int, _RowFilters] | None:
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError("postgres.equipment must be a mapping")

    table = _table_name(config.get("table"))
    columns = config.get("columns")
    if (
        not isinstance(columns, dict)
        or "equipment_id" not in columns
        or any(field not in _EQUIPMENT_FIELDS or not _identifier(column) for field, column in columns.items())
    ):
        raise ValueError(
            "postgres.equipment.columns must map equipment_id and optional canonical fields to column names"
        )

    max_rows = config.get("max_rows", 10_000)
    if type(max_rows) is not int or max_rows < 1:
        raise ValueError("postgres.equipment.max_rows must be a positive integer")
    ordered_columns = {field: columns[field] for field in _EQUIPMENT_FIELDS if field in columns}
    filters = _row_filters(config.get("filters", {}), "postgres.equipment")
    return table, ordered_columns, max_rows, filters


def _record_specs(config: object) -> list[_RecordSpec]:
    if not isinstance(config, list):
        raise ValueError("postgres.records must be a list of evidence row configurations")

    specs = []
    seen_tables = set()
    for item in config:
        if not isinstance(item, dict) or item.keys() - _RECORD_FIELDS:
            raise ValueError("postgres.records entries must be mappings with supported fields")
        table = _table_name(item.get("table"))
        if table in seen_tables:
            raise ValueError("postgres.records must configure each table or view at most once")
        seen_tables.add(table)

        id_column = item.get("id_column")
        columns = item.get("columns")
        if not _identifier(id_column):
            raise ValueError("postgres.records.id_column must be a column name")
        if not isinstance(columns, list) or not columns or not all(_identifier(column) for column in columns):
            raise ValueError("postgres.records.columns must be a nonempty list of column names")
        if len(columns) != len(set(columns)):
            raise ValueError("postgres.records.columns must not repeat column names")

        optional = {field: item.get(field) for field in _RECORD_OPTIONAL_COLUMNS}
        if any(value is not None and not _identifier(value) for value in optional.values()):
            raise ValueError("postgres.records optional column mappings must name columns")
        if optional["plant_column"] is not None and optional["equipment_column"] is None:
            raise ValueError("postgres.records.plant_column requires equipment_column")

        max_rows = item.get("max_rows", 1_000)
        if type(max_rows) is not int or max_rows < 1:
            raise ValueError("postgres.records.max_rows must be a positive integer")
        filters = _row_filters(item.get("filters", {}), "postgres.records")
        synthesize = item.get("synthesize", True)
        if type(synthesize) is not bool:
            raise ValueError("postgres.records.synthesize must be a boolean")
        specs.append(_RecordSpec(
            table, id_column, tuple(columns), max_rows=max_rows, filters=filters,
            synthesize=synthesize, **optional,
        ))
    return specs


def _load_table(cursor, name: str, schema: str, table: str) -> Source:
    cursor.execute(_TABLE_QUERY, (schema, table))
    record = cursor.fetchone()
    title = f"{schema}.{table}"
    if record is None:
        raise RuntimeError(f"Configured PostgreSQL table is missing or its schema is inaccessible: {title}")
    table_oid, comment = record
    blocks = [Block("table", f"PostgreSQL table: {title}" + (f"\n{comment}" if comment else ""))]

    cursor.execute(_COLUMNS_QUERY, (table_oid,))
    for column, data_type, not_null, comment in cursor.fetchall():
        nullable = "NOT NULL" if not_null else "NULL allowed"
        text = f"{column}: {data_type}, {nullable}" + (f"\n{comment}" if comment else "")
        blocks.append(Block(f"column:{column}", text))

    cursor.execute(_CONSTRAINTS_QUERY, (table_oid,))
    for constraint, definition in cursor.fetchall():
        blocks.append(Block(f"constraint:{constraint}", definition))

    key_path = _table_path(name, schema, table)
    return Source(
        key=f"postgres:{key_path}",
        title=title,
        kind="postgres",
        resource=f"postgresql://{key_path}",
        blocks=tuple(blocks),
    )


def _load_equipment(
    cursor,
    sql,
    table: tuple[str, str],
    columns: dict[str, str],
    max_rows: int,
    filters: _RowFilters,
) -> list[dict[str, str]]:
    records = _select_rows(
        cursor, sql, table, tuple(columns.values()), columns["equipment_id"], max_rows, "equipment",
        filters=filters,
    )
    if not records:
        raise RuntimeError(f"Configured PostgreSQL equipment table returned no rows: {'.'.join(table)}")
    equipment_rows = [
        {key: _equipment_value(value) for key, value in zip(columns, record, strict=True)}
        for record in records
    ]
    if any(not row["equipment_id"] for row in equipment_rows):
        raise RuntimeError("Configured PostgreSQL equipment contains an empty equipment_id")
    return equipment_rows


def _load_records(cursor, sql, name: str, spec: _RecordSpec) -> list[Source]:
    metadata_columns = (
        spec.equipment_column, spec.plant_column, spec.revision_column, spec.title_column
    )
    projection = tuple(dict.fromkeys(
        (spec.id_column, *spec.columns, *(column for column in metadata_columns if column is not None))
    ))
    table = ".".join(spec.table)
    key_path = _table_path(name, *spec.table)
    records = _select_rows(
        cursor, sql, spec.table, projection, spec.id_column, spec.max_rows, f"records from {table}",
        filters=spec.filters,
    )
    sources = []
    seen_ids = set()
    for record in records:
        values = dict(zip(projection, record, strict=True))
        id_value = values[spec.id_column]
        record_id = str(id_value) if id_value is not None else ""
        if not record_id.strip():
            raise RuntimeError(f"Configured PostgreSQL records from {table} contain an empty row ID")
        if record_id in seen_ids:
            raise RuntimeError(f"Configured PostgreSQL records from {table} contain a duplicate row ID")
        seen_ids.add(record_id)

        equipment_ids = ()
        if spec.equipment_column is not None:
            equipment = _equipment_value(values[spec.equipment_column])
            plant = _equipment_value(values.get(spec.plant_column))
            if not equipment or "/" in equipment or "/" in plant:
                raise RuntimeError(f"Configured PostgreSQL records from {table} contain an invalid equipment key")
            equipment_ids = (f"{plant}/{equipment}" if plant else equipment,)

        locator = f"row {spec.id_column}={record_id}"
        blocks = tuple(
            Block(f"{locator} / column {column}", _evidence_text(values[column]))
            for column in spec.columns
        )
        title = _equipment_value(values.get(spec.title_column)) or f"{table} {locator}"
        revision = _equipment_value(values.get(spec.revision_column)) or None
        sources.append(Source(
            key=f"postgres-row:{key_path}/{quote(record_id, safe='')}",
            title=title,
            kind="postgres_record",
            resource=postgres_resource(name, table),
            blocks=blocks,
            equipment_ids=equipment_ids,
            revision=revision,
            synthesize=spec.synthesize,
        ))
    return sources


def _select_rows(
    cursor,
    sql,
    table: tuple[str, str],
    projection: tuple[str, ...],
    order_by: str,
    max_rows: int,
    label: str,
    *,
    filters: _RowFilters,
) -> list[tuple]:
    where = sql.SQL("")
    if filters:
        predicates = (sql.SQL("{} = %s").format(sql.Identifier(column)) for column in filters)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(predicates)
    query = sql.SQL("SELECT {} FROM {}{} ORDER BY {} LIMIT %s").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in projection),
        sql.Identifier(*table),
        where,
        sql.Identifier(order_by),
    )
    cursor.execute(query, (*filters.values(), max_rows + 1))
    records = cursor.fetchall()
    if len(records) > max_rows:
        raise RuntimeError(f"Configured PostgreSQL {label} exceeds max_rows ({max_rows})")
    return records


def _evidence_text(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return str(value)


def _equipment_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(text for item in value if (text := _equipment_value(item)))
    return str(value).strip()
