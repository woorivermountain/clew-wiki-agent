"""Plan an initial wiki from an allowlisted, read-only PostgreSQL catalog."""

from copy import deepcopy
import os
import re

from clew_okf.stages.step_01_ingest.postgres import _equipment_config, _identifier, _record_specs, _table_name

_CATALOG = """
SELECT c.oid, n.nspname, c.relname, c.relkind,
       pg_catalog.obj_description(c.oid, 'pg_class'),
       pg_catalog.has_schema_privilege(n.oid, 'USAGE'),
       pg_catalog.has_table_privilege(c.oid, 'SELECT')
FROM pg_catalog.pg_class AS c
JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%s) AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
ORDER BY n.nspname, c.relname
LIMIT %s
"""
_COLUMNS = """
SELECT a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
       NOT (a.attnotnull OR (t.typtype = 'd' AND t.typnotnull)),
       pg_catalog.col_description(a.attrelid, a.attnum)
FROM pg_catalog.pg_attribute AS a
JOIN pg_catalog.pg_type AS t ON t.oid = a.atttypid
WHERE a.attrelid = %s::oid AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
_CONSTRAINTS = """
SELECT con.contype, con.conname, pg_catalog.pg_get_constraintdef(con.oid),
       ARRAY(SELECT a.attname FROM unnest(con.conkey) WITH ORDINALITY AS k(num, ord)
             JOIN pg_catalog.pg_attribute AS a ON a.attrelid = con.conrelid AND a.attnum = k.num
             ORDER BY k.ord),
       CASE WHEN target.oid IS NOT NULL THEN ns.nspname || '.' || target.relname END,
       ARRAY(SELECT a.attname FROM unnest(con.confkey) WITH ORDINALITY AS k(num, ord)
             JOIN pg_catalog.pg_attribute AS a ON a.attrelid = con.confrelid AND a.attnum = k.num
             ORDER BY k.ord)
FROM pg_catalog.pg_constraint AS con
LEFT JOIN pg_catalog.pg_class AS target ON target.oid = con.confrelid
LEFT JOIN pg_catalog.pg_namespace AS ns ON ns.oid = target.relnamespace
WHERE con.conrelid = %s::oid AND con.contype IN ('p', 'f')
ORDER BY con.contype DESC, con.conname
"""
_EQ_IDS = ("equipment_id", "asset_no", "asset_id")
_OPTIONAL = {
    "name": ("name", "equipment_name", "asset_name"),
    "plant_id": ("plant_id", "plant_no", "plant_code"),
    "process_id": ("process_id", "process_no"),
    "model": ("model", "model_name"),
    "aliases": ("aliases",),
}


def _finding(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _result(proposed: dict) -> dict:
    return {"status": "hold", "findings": [], "tables": [], "proposed_postgres": proposed,
            "report_jobs": [], "counts": {"schemas": 0, "tables": 0, "columns": 0,
                                           "equipment_candidates": 0, "report_jobs": 0}}


def _dsn(env: object) -> str:
    if not isinstance(env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env):
        raise ValueError("invalid connection environment")
    value = os.environ.get(env)
    if not value or not value.strip():
        raise ValueError("missing connection environment")
    return value


def _sources(config: dict) -> list[tuple[str, dict]]:
    admission = config.get("admission", {})
    if not isinstance(admission, dict):
        raise ValueError("invalid admission settings")
    mapping = admission.get("mapping", {})
    if not isinstance(mapping, dict):
        raise ValueError("invalid mapping settings")
    result = []
    for kind, spec in (("canonical", admission.get("reports")), ("database", mapping.get("database"))):
        if spec is not None:
            if not isinstance(spec, dict):
                raise ValueError("invalid report source")
            _table_name(spec.get("table"))
            result.append((kind, spec))
    return result


def _settings(config: dict) -> tuple[dict, list[str], int, int, bool, list[tuple[str, dict]]]:
    if not isinstance(config, dict) or not isinstance(config.get("postgres"), dict):
        raise ValueError("postgres settings required")
    postgres = config["postgres"]
    options = config.get("bootstrap", {})
    if not isinstance(options, dict) or options.keys() - {"schemas", "max_tables", "max_reports", "import_reports"}:
        raise ValueError("invalid bootstrap settings")
    max_tables, max_reports = options.get("max_tables", 200), options.get("max_reports", 1000)
    if any(type(value) is not int or value < 1 for value in (max_tables, max_reports)):
        raise ValueError("positive limits required")
    imports = options.get("import_reports", True)
    if type(imports) is not bool:
        raise ValueError("import_reports must be boolean")
    configured_tables = postgres.get("tables", [])
    if not isinstance(configured_tables, list):
        raise ValueError("invalid table list")
    declared = [_table_name(table) for table in configured_tables]
    equipment = _equipment_config(postgres.get("equipment"))
    if equipment:
        declared.append(equipment[0])
    records = _record_specs(postgres.get("records", []))
    declared.extend(spec.table for spec in records)
    sources = _sources(config)
    declared.extend(_table_name(spec["table"]) for _, spec in sources)
    if "schemas" in options:
        schemas = options["schemas"]
        if not isinstance(schemas, list) or not schemas or not all(_identifier(value) for value in schemas):
            raise ValueError("nonempty schema allowlist required")
    else:
        schemas = [schema for schema, _ in declared]
    if not schemas:
        raise ValueError("schema allowlist or configured relations required")
    return postgres, sorted(set(schemas)), max_tables, max_reports, imports, sources


def _prepare_connection(connection, isolation) -> None:
    connection.read_only = True
    connection.isolation_level = isolation


def _cursor_setup(cursor) -> None:
    cursor.execute("SET LOCAL statement_timeout = '15s'")
    cursor.execute("SET LOCAL search_path = pg_catalog")


def _equipment_plan(result: dict, postgres: dict, report_tables: set[str]) -> None:
    tables = result["tables"]
    if "equipment" in postgres and postgres["equipment"] is not None:
        spec = postgres["equipment"]
        table = next((row for row in tables if row["table"] == spec["table"]), None)
        if table is None or not table["schema_usage"] or not table["select_allowed"]:
            result["findings"].append(_finding("equipment_unavailable", "지정한 설비 테이블과 SELECT 권한을 확인하지 못했습니다."))
        elif not set(spec["columns"].values()) <= {col["name"] for col in table["columns"]}:
            result["findings"].append(_finding("equipment_columns_missing", "지정한 설비 매핑의 일부 컬럼이 존재하지 않습니다."))
        return
    eligible = [row for row in tables if row["table"] not in report_tables
                and row["schema_usage"] and row["select_allowed"]]
    candidates = []
    for table in eligible:
        names = {col["name"] for col in table["columns"]}
        identifiers = [name for name in _EQ_IDS if name in names]
        if len(identifiers) != 1:
            continue
        identifier = identifiers[0]
        primary = set(table["primary_key"])
        plant_keys = [name for name in _OPTIONAL["plant_id"]
                      if name in names and primary == {name, identifier}]
        if primary == {identifier} or len(plant_keys) == 1:
            candidates.append((table, identifier, plant_keys[0] if plant_keys else None))
    result["counts"]["equipment_candidates"] = len(candidates)
    if len(candidates) > 1:
        # A foreign key proves the target key is a unique referenced identity.
        # Only a unique parent breaks ties; counts, table names and row values do not.
        parents = [candidate for candidate in candidates if any(
            row["table"] != candidate[0]["table"]
            and foreign["referenced_table"] == candidate[0]["table"]
            and len(foreign["referenced_columns"]) == len(candidate[0]["primary_key"])
            and set(foreign["referenced_columns"]) == set(candidate[0]["primary_key"])
            for row in tables for foreign in row["foreign_keys"])]
        if len(parents) == 1:
            candidates = parents
    if len(candidates) != 1:
        names = ", ".join(row[0]["table"] for row in candidates) or "없음"
        result["findings"].append(_finding("equipment_ambiguous" if candidates else "equipment_missing",
            f"PK·FK로 고유한 설비 마스터를 확정하지 못했습니다. 후보: {names}"))
        return
    table, identifier, primary_plant = candidates[0]
    names = {col["name"] for col in table["columns"]}
    columns = {"equipment_id": identifier}
    for field, aliases in _OPTIONAL.items():
        matches = [primary_plant] if field == "plant_id" and primary_plant else [name for name in aliases if name in names]
        if len(matches) > 1:
            result["findings"].append(_finding("equipment_mapping_ambiguous", f"{table['table']}의 {field} 후보 컬럼이 여러 개입니다."))
        elif matches:
            columns[field] = matches[0]
    result["proposed_postgres"]["equipment"] = {"table": table["table"], "columns": columns}


def _identity_jobs(cursor, sql, kind: str, spec: dict, remaining: int) -> list[dict]:
    relation = _table_name(spec.get("table"))
    id_column, version_column = spec.get("id_column"), spec.get("version_column")
    if not all(_identifier(value) for value in (id_column, version_column)):
        raise ValueError("invalid report identity columns")
    if kind == "canonical":
        payload = spec.get("payload_column")
        if not _identifier(payload):
            raise ValueError("invalid canonical payload column")
        where = sql.SQL("{} ->> 'status' = %s AND {} -> 'reuse_approved' = 'true'::jsonb").format(
            sql.Identifier(payload), sql.Identifier(payload))
    else:
        status, approval = spec.get("status_column"), spec.get("approval_column")
        if not all(_identifier(value) for value in (status, approval)):
            raise ValueError("invalid report approval columns")
        where = sql.SQL("{} = %s AND {} IS TRUE").format(sql.Identifier(status), sql.Identifier(approval))
    query = sql.SQL("SELECT {}, {} FROM {} WHERE {} ORDER BY {}, {} LIMIT %s").format(
        sql.Identifier(id_column), sql.Identifier(version_column), sql.Identifier(*relation), where,
        sql.Identifier(id_column), sql.Identifier(version_column))
    cursor.execute(query, ("finalized", remaining + 1))
    rows = cursor.fetchall()
    jobs = []
    for report_id, version in rows:
        if (not isinstance(report_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", report_id)
                or type(version) is not int or version < 1):
            raise ValueError("invalid report identity")
        jobs.append({"report_id": report_id, "version": version, "source": kind})
    return jobs


def scan_database(config: dict) -> dict:
    """Return a reviewable plan; never read arbitrary rows, run a model, or modify a factory."""
    proposed = deepcopy(config.get("postgres", {})) if isinstance(config, dict) else {}
    result = _result(proposed if isinstance(proposed, dict) else {})
    try:
        postgres, schemas, max_tables, max_reports, imports, sources = _settings(config)
        primary_env = postgres.get("dsn_env", "CLEW_POSTGRES_DSN")
        dsn = _dsn(primary_env)
    except ValueError:
        result["findings"].append(_finding("bootstrap_config_invalid", "스키마 허용 목록·DB 환경 변수·설비/보고서 설정과 한도를 확인하세요."))
        return result
    result["counts"]["schemas"] = len(schemas)
    try:
        import psycopg
        from psycopg import IsolationLevel, sql
    except ImportError:
        result["findings"].append(_finding("postgres_unavailable", "PostgreSQL 의존성을 설치해야 초기 구성을 계획할 수 있습니다."))
        return result
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            _prepare_connection(connection, IsolationLevel.REPEATABLE_READ)
            with connection.cursor() as cursor:
                _cursor_setup(cursor)
                cursor.execute(_CATALOG, (schemas, max_tables + 1))
                catalog = cursor.fetchall()
                result["counts"]["tables"] = len(catalog)
                for oid, schema, name, kind, comment, usage, select in catalog:
                    table = {"table": f"{schema}.{name}", "kind": kind, "comment": comment,
                             "schema_usage": bool(usage), "select_allowed": bool(select),
                             "columns": [], "primary_key": [], "foreign_keys": []}
                    result["tables"].append(table)
                    if "." in schema or "." in name:
                        result["findings"].append(_finding("relation_name_unsupported", "일부 관계 이름을 현재 schema.table 설정으로 표현할 수 없습니다."))
                    if not usage:
                        result["findings"].append(_finding("schema_usage_denied", "일부 허용 스키마에 USAGE 권한이 없습니다."))
                    if len(catalog) <= max_tables:
                        cursor.execute(_COLUMNS, (oid,))
                        table["columns"] = [{"name": c, "type": t, "nullable": bool(n), "comment": m}
                                            for c, t, n, m in cursor.fetchall()]
                        result["counts"]["columns"] += len(table["columns"])
                        cursor.execute(_CONSTRAINTS, (oid,))
                        for ctype, cname, definition, columns, target, target_columns in cursor.fetchall():
                            if ctype == "p":
                                table["primary_key"] = list(columns)
                            else:
                                table["foreign_keys"].append({"name": cname, "columns": list(columns),
                                    "referenced_table": target, "referenced_columns": list(target_columns),
                                    "definition": definition})
                if len(catalog) > max_tables:
                    result["findings"].append(_finding("table_limit", "카탈로그가 max_tables를 넘어 전체 탐색을 완료하지 않았습니다. 범위를 좁히세요."))
                    return result
                if not catalog:
                    result["findings"].append(_finding("catalog_empty", "허용 스키마에서 접근 가능한 테이블을 찾지 못했습니다."))
                discovered = {table["table"] for table in result["tables"]}
                result["proposed_postgres"]["tables"] = sorted(set(postgres.get("tables", [])) | discovered)
                excluded = {spec["table"] for _, spec in sources}
                excluded.update(spec["table"] for spec in postgres.get("records", []))
                _equipment_plan(result, postgres, excluded)
                if result["findings"]:
                    return result
                if imports:
                    jobs = []
                    for kind, spec in sources:
                        report_dsn = _dsn(spec.get("dsn_env", primary_env))
                        if report_dsn == dsn:
                            found = _identity_jobs(cursor, sql, kind, spec, max_reports - len(jobs))
                        else:
                            with psycopg.connect(report_dsn, connect_timeout=5) as reports_connection:
                                _prepare_connection(reports_connection, IsolationLevel.REPEATABLE_READ)
                                with reports_connection.cursor() as reports_cursor:
                                    _cursor_setup(reports_cursor)
                                    found = _identity_jobs(reports_cursor, sql, kind, spec, max_reports - len(jobs))
                        jobs.extend(found)
                        if len(jobs) > max_reports:
                            result["findings"].append(_finding("report_limit", "승인 보고서가 max_reports를 넘어 전체 목록을 확정하지 못했습니다."))
                            return result
                    identities = {(job["report_id"], job["version"]) for job in jobs}
                    if len(identities) != len(jobs):
                        result["findings"].append(_finding("report_identity_ambiguous", "보고서 ID·버전이 중복되어 입력 출처를 확정하지 못했습니다."))
                        return result
                    result["report_jobs"] = sorted(jobs, key=lambda row: (row["report_id"], row["version"], row["source"]))
                    result["counts"]["report_jobs"] = len(jobs)
    except ValueError:
        result["findings"].append(_finding("bootstrap_input_invalid", "보고서 저장소의 설정 또는 승인된 보고서 식별자를 확인하세요."))
        return result
    except psycopg.Error:
        result["findings"].append(_finding("postgres_read_failed", "DB 카탈로그 또는 승인 보고서 목록 조회를 완료하지 못했습니다."))
        return result
    result["status"] = "ready"
    return result
