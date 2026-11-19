"""Read an allowlisted original report for server-side automatic mapping."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

from clew_okf.stages.step_01_ingest.documents import load_documents
from clew_okf.domain.model import Block, Source
from clew_okf.runtime.progress import emit
from clew_okf.stages.step_01_ingest.postgres import _evidence_text, _identifier, _table_name, postgres_resource

_METADATA_FIELDS = {"plant_id", "equipment_id", "event_id", "supersedes_version"}
_METADATA_ERROR = "Raw report metadata is invalid or not approved"
_INPUT_ERROR = "Raw report original is unavailable or invalid"
_FILE_SETTINGS = {"root", "kordoc", "timeout_seconds", "max_bytes", "entries", "registry"}
_FILE_ENTRY_FIELDS = {"report_id", "version", "path", "revision", "status", "reuse_approved", "metadata"}
_DATABASE_FIELDS = {
    "table", "id_column", "version_column", "revision_column", "status_column", "approval_column",
    "content_columns", "metadata_columns", "dsn_env",
}
_SUPPORTED_DOCUMENTS = {".hwp", ".hwpx", ".pdf", ".md", ".markdown", ".txt", ".csv", ".docx", ".pptx", ".xlsx"}


@dataclass(frozen=True, slots=True)
class RawReport:
    report_id: str
    version: int
    source: Source
    metadata: dict
    fingerprint: str
    origin: dict


def load_raw_report(
    mapping: dict,
    postgres_config: dict | None,
    base: Path,
    output: Path,
    report_id: str,
    version: int,
    kind: str,
) -> RawReport:
    """Load one approved report from server-owned file entries or a database."""
    if (
        not isinstance(report_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", report_id)
        or type(version) is not int or version < 1
    ):
        raise ValueError("A valid report_id and positive integer version are required")
    if not isinstance(mapping, dict):
        raise ValueError("admission.mapping must be a mapping")
    emit("reading_report", "보고서 원본과 승인 상태를 확인합니다.", input_source=kind, input_report_id=report_id, input_version=version)
    if kind == "file":
        return _load_file(mapping.get("files"), Path(base).resolve(), Path(output), report_id, version)
    if kind == "managed":
        return _load_managed(mapping.get("managed"), Path(base).resolve(), report_id, version)
    if kind == "database":
        return _load_database(mapping.get("database"), postgres_config, report_id, version)
    raise ValueError("Raw report kind must be file, managed, or database")


def _metadata(report_id: str, version: int, status: object, approval: object, extra: object) -> dict:
    if status != "finalized" or type(approval) is not bool or approval is not True:
        raise RuntimeError(_METADATA_ERROR)
    if not isinstance(extra, dict) or extra.keys() - _METADATA_FIELDS:
        raise RuntimeError(_METADATA_ERROR)
    for key, value in extra.items():
        if key == "supersedes_version":
            if value is not None and (type(value) is not int or value < 1):
                raise RuntimeError(_METADATA_ERROR)
        elif not isinstance(value, str) or not value or "/" in value or re.search(r"\s", value):
            raise RuntimeError(_METADATA_ERROR)
    return {
        **extra,
        "report_id": report_id,
        "version": version,
        "status": "finalized",
        "reuse_approved": True,
    }


def _revision(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(_METADATA_ERROR)
    return value


def _make_report(
    report_id: str,
    version: int,
    source: Source,
    metadata: dict,
    kind: str,
    *,
    bytes_digest: str | None = None,
) -> RawReport:
    source = replace(
        source,
        key=f"raw-report:{kind}:{report_id}@{version}",
        kind="report_input",
        equipment_ids=(),
        concept_type="Report Original",
        synthesize=False,
    )
    payload = {"source": asdict(source), "metadata": metadata, "bytes_sha256": bytes_digest}
    fingerprint = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return RawReport(
        report_id, version, source, metadata, fingerprint,
        {"kind": kind, "input_digest": fingerprint},
    )


def _load_file(
    settings: object, base: Path, output: Path, report_id: str, version: int
) -> RawReport:
    if not isinstance(settings, dict) or settings.keys() - _FILE_SETTINGS:
        raise ValueError("admission.mapping.files must contain supported file source settings")
    root_value = settings.get("root")
    if not isinstance(root_value, str) or not root_value.strip() or "\0" in root_value:
        raise ValueError("admission.mapping.files.root must be a directory path")
    raw_root = base / root_value
    if raw_root.is_symlink():
        raise ValueError("Raw report root must not be a symbolic link")
    root = raw_root.resolve()
    if not root.is_dir():
        raise ValueError("Raw report root must be an existing directory")
    output = (base / output).resolve()
    if root.is_relative_to(output) or output.is_relative_to(root):
        raise ValueError("Raw report root and wiki output directory must not overlap")
    entries = settings.get("entries", [] if "registry" in settings else None)
    if not isinstance(entries, list) or any(
        not isinstance(entry, dict) or entry.keys() - _FILE_ENTRY_FIELDS for entry in entries
    ):
        raise ValueError("admission.mapping.files.entries must be a list of supported manifest entries")
    matches = [
        entry for entry in entries
        if entry.get("report_id") == report_id and type(entry.get("version")) is int
        and entry["version"] == version
    ]
    managed = None
    if "registry" in settings:
        from clew_okf.stages.step_01_ingest.managed_intake import lookup_managed
        registry = settings["registry"]
        if not isinstance(registry, str) or not registry.strip():
            raise ValueError("Raw report registry must be a path")
        managed = lookup_managed((base / registry).resolve(), report_id, version)
        if managed is not None and managed["source"] == "file":
            matches.append({"report_id": report_id, "version": version, "path": managed["path"],
                            "revision": managed["revision"], "metadata": managed["metadata"],
                            "status": managed["status"], "reuse_approved": managed["reuse_approved"]})
    if len(matches) != 1:
        raise RuntimeError(_METADATA_ERROR)
    entry = matches[0]
    metadata = _metadata(
        report_id, version, entry.get("status"), entry.get("reuse_approved"), entry.get("metadata", {})
    )
    revision = _revision(entry.get("revision"))
    relative_value = entry.get("path")
    if not isinstance(relative_value, str) or "\0" in relative_value:
        raise ValueError("Raw report path must be a relative POSIX file path")
    relative = PurePosixPath(relative_value)
    if (
        relative.is_absolute() or ".." in relative.parts or relative.as_posix() != relative_value
        or relative_value == "."
    ):
        raise ValueError("Raw report path must stay inside its configured root")
    if relative.suffix.lower() not in _SUPPORTED_DOCUMENTS:
        raise ValueError("Raw report path uses an unsupported document format")

    path = root / relative
    _check_file(path, root)
    timeout = settings.get("timeout_seconds", 120)
    max_bytes = settings.get("max_bytes", 64 * 1024 * 1024)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Raw report timeout_seconds must be a positive number")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("Raw report max_bytes must be a positive integer")
    before = _file_digest(path, max_bytes)
    if managed is not None and before != managed["content_sha"]:
        raise RuntimeError(_INPUT_ERROR)
    config = {
        key: settings[key]
        for key in ("kordoc", "timeout_seconds", "max_bytes")
        if key in settings
    }
    file_options = {"revision": revision}
    if managed is not None:
        file_options["title"] = managed["filename"]
    config.update(root=str(root), files={relative_value: file_options}, include_only_listed=True)
    emit("extracting_document", "업로드 문서의 원문과 위치를 추출합니다.", input_report_id=report_id, input_version=version)
    try:
        sources, _ = load_documents(config, base, output=output, cache=output / "extraction-cache")
    except (ValueError, OSError, RuntimeError):
        raise RuntimeError(_INPUT_ERROR) from None
    _check_file(path, root)
    if before != _file_digest(path, max_bytes) or len(sources) != 1 or not sources[0].blocks:
        raise RuntimeError(_INPUT_ERROR)
    return _make_report(report_id, version, sources[0], metadata, "file", bytes_digest=before)


def _check_file(path: Path, root: Path) -> None:
    cursor = path
    while cursor != root:
        if cursor.is_symlink():
            raise ValueError("Raw report path must not contain symbolic links")
        cursor = cursor.parent
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise RuntimeError(_INPUT_ERROR)


def _file_digest(path: Path, max_bytes: int) -> str:
    try:
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or size > max_bytes:
                raise RuntimeError(_INPUT_ERROR)
            digest = hashlib.sha256()
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
                remaining -= len(chunk)
    except OSError:
        raise RuntimeError(_INPUT_ERROR) from None
    raise RuntimeError(_INPUT_ERROR)


def _load_database(settings: object, postgres_config: dict | None, report_id: str, version: int) -> RawReport:
    if not isinstance(settings, dict) or settings.keys() - _DATABASE_FIELDS:
        raise ValueError("admission.mapping.database must contain supported database source settings")
    table = _table_name(settings.get("table"))
    fields = ("id_column", "version_column", "revision_column", "status_column", "approval_column")
    configured = {field: settings.get(field) for field in fields}
    if not all(_identifier(column) for column in configured.values()):
        raise ValueError("Raw report database identity, revision, status, and approval columns are required")
    content_columns = settings.get("content_columns")
    if (
        not isinstance(content_columns, list) or not content_columns
        or not all(_identifier(column) for column in content_columns)
        or len(content_columns) != len(set(content_columns))
    ):
        raise ValueError("Raw report content_columns must be a nonempty list of distinct column names")
    metadata_columns = settings.get("metadata_columns", {})
    if (
        not isinstance(metadata_columns, dict) or metadata_columns.keys() - _METADATA_FIELDS
        or not all(_identifier(column) for column in metadata_columns.values())
    ):
        raise ValueError("Raw report metadata_columns must map supported metadata fields to column names")
    if postgres_config is not None and not isinstance(postgres_config, dict):
        raise ValueError("postgres must be a mapping")
    postgres = postgres_config or {}
    dsn_env = settings.get("dsn_env", postgres.get("dsn_env", "CLEW_POSTGRES_DSN"))
    if not isinstance(dsn_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dsn_env):
        raise ValueError("Raw report dsn_env must name an environment variable")
    dsn = os.environ.get(dsn_env)
    if not dsn or not dsn.strip():
        raise ValueError("The raw report database connection environment variable is missing or empty")
    name = postgres.get("name", "postgres")
    if not isinstance(name, str) or not re.fullmatch(r"[\w.-]+", name) or name in {".", ".."}:
        raise ValueError("Raw report database name must be a logical database label")
    projection = tuple(dict.fromkeys((*configured.values(), *content_columns, *metadata_columns.values())))

    try:
        import psycopg
        from psycopg import IsolationLevel, sql
    except ImportError:
        raise RuntimeError("Raw report database lookup requires the PostgreSQL dependency") from None
    query = sql.SQL("SELECT {} FROM {} WHERE {} = %s AND {} = %s LIMIT 2").format(
        sql.SQL(", ").join(sql.Identifier(column) for column in projection),
        sql.Identifier(*table),
        sql.Identifier(configured["id_column"]),
        sql.Identifier(configured["version_column"]),
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
        raise RuntimeError("Raw report database retrieval failed") from None
    if len(records) != 1:
        raise RuntimeError(_METADATA_ERROR)
    values = dict(zip(projection, records[0], strict=True))
    if (
        type(values[configured["id_column"]]) is not str or values[configured["id_column"]] != report_id
        or type(values[configured["version_column"]]) is not int
        or values[configured["version_column"]] != version
    ):
        raise RuntimeError(_METADATA_ERROR)
    metadata = _metadata(
        report_id, version, values[configured["status_column"]], values[configured["approval_column"]],
        {key: values[column] for key, column in metadata_columns.items()},
    )
    revision = _revision(values[configured["revision_column"]])
    blocks = tuple(Block(f"column {column}", _evidence_text(values[column])) for column in content_columns)
    if not any(block.text.strip() for block in blocks):
        raise RuntimeError(_INPUT_ERROR)
    source = Source(
        key="",
        title=report_id,
        kind="report_input",
        resource=postgres_resource(name, ".".join(table)),
        blocks=blocks,
        revision=revision,
    )
    return _make_report(report_id, version, source, metadata, "database")


def _load_managed(settings: object, base: Path, report_id: str, version: int) -> RawReport:
    from clew_okf.stages.step_01_ingest.managed_intake import lookup_managed
    if not isinstance(settings, dict) or set(settings) != {"registry"}:
        raise ValueError("admission.mapping.managed requires a registry path")
    registry = settings["registry"]
    if not isinstance(registry, str) or not registry.strip():
        raise ValueError("Managed registry must be a path")
    row = lookup_managed((base / registry).resolve(), report_id, version)
    if row is None or row["source"] != "text":
        raise RuntimeError(_INPUT_ERROR)
    metadata = _metadata(report_id, version, row["status"], row["reuse_approved"], row["metadata"])
    body = row["body"]
    if (not isinstance(body, str) or not body.strip()
            or hashlib.sha256(body.encode("utf-8")).hexdigest() != row["content_sha"]):
        raise RuntimeError(_INPUT_ERROR)
    source = Source("", report_id, "report_input", f"sqlite://managed/{report_id}/{version}",
                    (Block("보고서 본문", body),), revision=_revision(row["revision"]))
    return _make_report(report_id, version, source, metadata, "managed", bytes_digest=row["content_sha"])
