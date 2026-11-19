"""Load an identified report from the server-configured canonical PostgreSQL source."""

from __future__ import annotations

import json
import os
import re
from typing import TYPE_CHECKING

from clew_okf.stages.step_01_ingest.postgres import _identifier, _table_name

if TYPE_CHECKING:
    from clew_okf.domain.reports import ReportRecord

_REPORT_FIELDS = {"table", "id_column", "version_column", "payload_column", "dsn_env"}


class CanonicalReportHoldError(RuntimeError):
    """The database is readable but the identified report is not admissible yet."""
    def __init__(self, diagnostic: str, *, code: str, public_message: str):
        super().__init__(diagnostic)
        self.code, self.public_message = code, public_message


def load_report(
    admission: dict,
    postgres_config: dict | None,
    report_id: str,
    version: int,
) -> ReportRecord:
    """Read exactly one canonical payload and validate its requested identity."""
    if not isinstance(report_id, str) or not report_id.strip() or "\0" in report_id:
        raise ValueError("report_id must be a nonempty string")
    if type(version) is not int or version < 1:
        raise ValueError("report version must be a positive integer")
    if not isinstance(admission, dict):
        raise ValueError("admission must be a mapping")
    settings = admission.get("reports")
    if not isinstance(settings, dict) or settings.keys() - _REPORT_FIELDS:
        raise ValueError("admission.reports must be a mapping with supported report source fields")
    table = _table_name(settings.get("table"))
    columns = tuple(settings.get(field) for field in ("payload_column", "id_column", "version_column"))
    if not all(_identifier(column) for column in columns):
        raise ValueError("admission.reports must configure payload_column, id_column, and version_column")

    if postgres_config is not None and not isinstance(postgres_config, dict):
        raise ValueError("postgres must be a mapping")
    dsn_env = settings.get("dsn_env", (postgres_config or {}).get("dsn_env", "CLEW_POSTGRES_DSN"))
    if not isinstance(dsn_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dsn_env):
        raise ValueError("admission.reports.dsn_env must be an environment variable name")
    dsn = os.environ.get(dsn_env)
    if not dsn or not dsn.strip():
        raise ValueError("The canonical report connection environment variable is missing or empty")

    try:
        import psycopg
        from psycopg import IsolationLevel, sql
    except ImportError:
        raise RuntimeError("Canonical report lookup requires the PostgreSQL dependency") from None

    query = sql.SQL("SELECT {} FROM {} WHERE {} = %s AND {} = %s LIMIT 2").format(
        sql.Identifier(columns[0]),
        sql.Identifier(*table),
        sql.Identifier(columns[1]),
        sql.Identifier(columns[2]),
    )
    try:
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            connection.read_only = True
            connection.isolation_level = IsolationLevel.REPEATABLE_READ
            with connection.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = '15s'")
                cursor.execute("SET LOCAL search_path = pg_catalog")
                cursor.execute(query, (report_id, version))
                records = cursor.fetchall()
    except psycopg.Error:
        raise RuntimeError("Canonical report retrieval failed") from None

    if len(records) != 1:
        raise CanonicalReportHoldError("Canonical report lookup must match exactly one record",
            code="canonical_identity_unavailable", public_message="보고서 ID·버전으로 원본 한 건을 확정하지 못했습니다.")
    try:
        from clew_okf.domain.reports import ReportRecord

        payload = records[0][0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            raise ValueError("Report payload must be an object")
        if payload.get("status") != "finalized" or type(payload.get("reuse_approved")) is not bool or payload["reuse_approved"] is not True:
            raise CanonicalReportHoldError("Canonical report payload is invalid", code="awaiting_approval",
                public_message="보고서 정본의 확정과 재사용 승인이 필요합니다.")
        report = ReportRecord.model_validate_json(json.dumps(payload, allow_nan=False), strict=True)
    except (ValueError, TypeError, OverflowError):
        raise CanonicalReportHoldError("Canonical report payload is invalid", code="canonical_payload_invalid",
            public_message="DB 정본의 필수 보고서 필드·원문 근거를 확인하세요.") from None
    if report.report_id != report_id or report.version != version:
        raise CanonicalReportHoldError("Canonical report identity does not match the requested record",
            code="canonical_identity_mismatch", public_message="DB 정본의 보고서 ID·버전이 요청과 일치하지 않습니다.")
    return report
