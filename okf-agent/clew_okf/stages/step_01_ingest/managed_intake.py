"""Durably save managed reports and enqueue work in one SQLite transaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import BinaryIO, Callable
from urllib.parse import quote
from uuid import uuid4

from clew_okf.runtime.jobs import JobStore, enqueue_in_transaction, get_in_transaction


class InputValidationError(ValueError):
    """The managed report input or approval state is invalid."""


class InputConflictError(ValueError):
    """An existing report ID and version has different immutable content."""


class InputNotFoundError(LookupError):
    """The requested managed report has not been saved."""


_METADATA_FIELDS = frozenset({"plant_id", "equipment_id", "event_id", "supersedes_version"})
_EXTENSIONS = frozenset({".hwp", ".hwpx", ".pdf"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_reports (
    report_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    source TEXT NOT NULL CHECK (source IN ('text', 'file')),
    body TEXT,
    path TEXT UNIQUE,
    filename TEXT,
    revision TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('draft', 'finalized')),
    reuse_approved INTEGER NOT NULL CHECK (reuse_approved IN (0, 1)),
    content_sha TEXT NOT NULL,
    approval_revision INTEGER NOT NULL CHECK (approval_revision > 0),
    last_job_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (report_id, version),
    CHECK (reuse_approved = 0 OR status = 'finalized'),
    CHECK ((source = 'text' AND body IS NOT NULL AND path IS NULL AND filename IS NULL)
        OR (source = 'file' AND body IS NULL AND path IS NOT NULL AND filename IS NOT NULL))
)
"""


def _identity(report_id: str, version: int) -> None:
    if (
        not isinstance(report_id, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", report_id) is None
        or type(version) is not int
        or version < 1
    ):
        raise InputValidationError("report_id and positive integer version are required")


def _approval(status: str, reuse_approved: bool) -> None:
    if (
        type(status) is not str
        or status not in {"draft", "finalized"}
        or type(reuse_approved) is not bool
        or (reuse_approved and status != "finalized")
    ):
        raise InputValidationError("approval must be a strict boolean and true requires finalized status")


def _metadata(metadata: dict) -> str:
    if not isinstance(metadata, dict) or metadata.keys() - _METADATA_FIELDS:
        raise InputValidationError("metadata contains unsupported fields")
    values = dict(metadata)
    for key, value in values.items():
        if key == "supersedes_version":
            if value is not None and (type(value) is not int or value < 1):
                raise InputValidationError("supersedes_version must be a positive integer or null")
        elif (
            not isinstance(value, str)
            or not value
            or "/" in value
            or re.search(r"\s", value)
            or any(ord(char) < 32 for char in value)
        ):
            raise InputValidationError("metadata identifiers must be nonempty and contain no slash or whitespace")
    if values.get("supersedes_version") is None:
        values.pop("supersedes_version", None)
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _revision(revision: str) -> None:
    if not isinstance(revision, str) or not revision.strip() or "\0" in revision:
        raise InputValidationError("revision must be a nonempty string")


def _report(row: sqlite3.Row) -> dict:
    return {
        "report_id": row["report_id"], "version": row["version"], "source": row["source"],
        "body": row["body"], "path": row["path"], "filename": row["filename"],
        "revision": row["revision"], "metadata": json.loads(row["metadata_json"]),
        "status": row["status"], "reuse_approved": bool(row["reuse_approved"]),
        "content_sha": row["content_sha"], "approval_revision": row["approval_revision"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _read_connection(path: Path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path.resolve()), safe="/") + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def lookup_managed(path: Path, report_id: str, version: int) -> dict | None:
    """Read a managed report without creating a database, table or job."""
    _identity(report_id, version)
    path = Path(path)
    if not path.is_file():
        return None
    connection = _read_connection(path)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'managed_reports'"
        ).fetchone()
        if table is None:
            return None
        row = connection.execute(
            "SELECT * FROM managed_reports WHERE report_id = ? AND version = ?", (report_id, version)
        ).fetchone()
        return _report(row) if row is not None else None
    finally:
        connection.close()


class ManagedIntake:
    """Store immutable report versions; change approval through set_approval only."""

    def __init__(self, state_db: Path, files_root: Path, max_upload_bytes: int = 64 * 1024 * 1024):
        if type(max_upload_bytes) is not int or max_upload_bytes < 1:
            raise InputValidationError("max_upload_bytes must be a positive integer")
        self.state_db = Path(state_db).resolve()
        root = Path(files_root)
        if root.is_symlink():
            raise InputValidationError("files_root must not be a symbolic link")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not root.is_dir():
            raise InputValidationError("files_root must be a directory")
        self.files_root = root.resolve()
        self.max_upload_bytes = max_upload_bytes
        self.jobs = JobStore(self.state_db)
        with self.jobs.transaction() as connection:
            connection.execute(_SCHEMA)

    def save_text(
        self, report_id: str, version: int, *, body: str, revision: str, metadata: dict,
        status: str = "draft", reuse_approved: bool = False,
    ) -> dict:
        _identity(report_id, version)
        _revision(revision)
        _approval(status, reuse_approved)
        metadata_json = _metadata(metadata)
        if not isinstance(body, str) or not body.strip():
            raise InputValidationError("body must contain report text")
        try:
            encoded = body.encode("utf-8")
        except UnicodeError:
            raise InputValidationError("body must be valid UTF-8 text") from None
        if len(encoded) > self.max_upload_bytes:
            raise InputValidationError("report text exceeds max_upload_bytes")
        with self.jobs.transaction() as connection:
            return self._save(
                connection, report_id, version, source="text", body=body, filename=None,
                revision=revision, metadata_json=metadata_json, status=status,
                reuse_approved=reuse_approved, content_sha=hashlib.sha256(encoded).hexdigest(),
            )

    def save_file(
        self, report_id: str, version: int, *, stream: BinaryIO, filename: str,
        revision: str, metadata: dict, status: str = "draft", reuse_approved: bool = False,
    ) -> dict:
        _identity(report_id, version)
        _revision(revision)
        _approval(status, reuse_approved)
        metadata_json = _metadata(metadata)
        suffix = self._filename(filename)
        temporary: Path | None = None
        installed: Path | None = None
        committed = False
        try:
            descriptor, name = tempfile.mkstemp(prefix=".intake-", suffix=".tmp", dir=self.files_root)
            temporary = Path(name)
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    chunk = stream.read(min(64 * 1024, self.max_upload_bytes - total + 1))
                    if not isinstance(chunk, bytes):
                        raise InputValidationError("upload stream must return bytes")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_upload_bytes:
                        raise InputValidationError("upload exceeds max_upload_bytes")
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            self._signature(temporary, suffix)

            def install() -> str:
                nonlocal installed
                for _ in range(10):
                    target = self.files_root / (uuid4().hex + suffix)
                    try:
                        reservation = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    except FileExistsError:
                        continue
                    os.close(reservation)
                    installed = target  # Only this attempt owns the reserved destination.
                    break
                else:
                    raise RuntimeError("could not allocate an unused managed upload path")
                os.replace(temporary, target)
                self._sync_files_root()
                return target.name

            with self.jobs.transaction() as connection:
                result = self._save(
                    connection, report_id, version, source="file", body=None, filename=filename,
                    revision=revision, metadata_json=metadata_json, status=status,
                    reuse_approved=reuse_approved, content_sha=digest.hexdigest(), install=install,
                )
            committed = True
            return result
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if installed is not None and not committed:
                self._cleanup_unreferenced(installed)

    def set_approval(self, report_id: str, version: int, status: str, reuse_approved: bool) -> dict:
        """Apply a trusted workflow transition and atomically enqueue a new generation."""
        _identity(report_id, version)
        _approval(status, reuse_approved)
        with self.jobs.transaction() as connection:
            row = self._row(connection, report_id, version)
            if row is None:
                raise InputNotFoundError("managed report not found")
            if row["status"] == status and bool(row["reuse_approved"]) == reuse_approved and row["last_job_id"]:
                latest = get_in_transaction(connection, row["last_job_id"])
                if latest is not None:
                    return {"report": _report(row), "job": latest}
            if row["status"] != status or bool(row["reuse_approved"]) != reuse_approved:
                connection.execute(
                    "UPDATE managed_reports SET status = ?, reuse_approved = ?, "
                    "approval_revision = approval_revision + 1, updated_at = ? "
                    "WHERE report_id = ? AND version = ?",
                    (status, int(reuse_approved), time.time(), report_id, version),
                )
                row = self._row(connection, report_id, version)
            return self._enqueue(connection, row)

    @staticmethod
    def _row(connection: sqlite3.Connection, report_id: str, version: int) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM managed_reports WHERE report_id = ? AND version = ?", (report_id, version)
        ).fetchone()

    def _save(
        self, connection: sqlite3.Connection, report_id: str, version: int, *, source: str,
        body: str | None, filename: str | None, revision: str, metadata_json: str,
        status: str, reuse_approved: bool, content_sha: str, install: Callable[[], str] | None = None,
    ) -> dict:
        row = self._row(connection, report_id, version)
        if row is not None:
            if (
                row["source"] != source or row["body"] != body or row["revision"] != revision
                or row["metadata_json"] != metadata_json or row["content_sha"] != content_sha
            ):
                raise InputConflictError("report identity already has different immutable content")
            # Preserve a newer retry or approval-generation job on an idempotent save.
            if row["last_job_id"]:
                latest = get_in_transaction(connection, row["last_job_id"])
                if latest is not None:
                    return {"report": _report(row), "job": latest}
            return self._enqueue(connection, row)
        relative = install() if install is not None else None
        now = time.time()
        connection.execute(
            "INSERT INTO managed_reports (report_id, version, source, body, path, filename, revision, "
            "metadata_json, status, reuse_approved, content_sha, approval_revision, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
            (report_id, version, source, body, relative, filename, revision, metadata_json,
             status, int(reuse_approved), content_sha, now, now),
        )
        return self._enqueue(connection, self._row(connection, report_id, version))

    def _enqueue(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        report = _report(row)
        identity = {
            key: report[key] for key in (
                "report_id", "version", "source", "revision", "metadata", "content_sha",
                "status", "reuse_approved", "approval_revision",
            )
        }
        fingerprint = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        job = enqueue_in_transaction(
            connection, "report",
            {"report_id": report["report_id"], "version": report["version"],
             "source": "managed_text" if report["source"] == "text" else "file"},
            "managed-report:" + fingerprint,
        )
        if row["last_job_id"] != job["id"]:
            connection.execute(
                "UPDATE managed_reports SET last_job_id = ? WHERE report_id = ? AND version = ?",
                (job["id"], report["report_id"], report["version"]),
            )
        return {"report": report, "job": job}

    @staticmethod
    def _filename(filename: str) -> str:
        if (
            not isinstance(filename, str) or not filename or filename.strip() != filename
            or len(filename) > 255 or any(char in filename for char in ("/", "\\", ":"))
            or any(ord(char) < 32 for char in filename)
        ):
            raise InputValidationError("filename must be a basename without path components")
        suffix = Path(filename).suffix.lower()
        if suffix not in _EXTENSIONS:
            raise InputValidationError("only HWP, HWPX and PDF uploads are supported")
        return suffix

    @staticmethod
    def _signature(path: Path, suffix: str) -> None:
        with path.open("rb") as stream:
            header = stream.read(16)
        valid = (
            suffix == ".pdf" and header.startswith(b"%PDF-")
            or suffix == ".hwp" and header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            or suffix == ".hwpx" and header.startswith(b"PK\x03\x04")
        )
        if not valid:
            raise InputValidationError("uploaded file signature does not match its extension")

    def _sync_files_root(self) -> None:
        descriptor = os.open(self.files_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _cleanup_unreferenced(self, path: Path) -> None:
        # Delete only this attempt's server-generated file after proving it is
        # unreferenced. An uncertain DB state must never delete committed content.
        try:
            connection = _read_connection(self.state_db)
            try:
                referenced = connection.execute(
                    "SELECT 1 FROM managed_reports WHERE path = ?", (path.name,)
                ).fetchone()
            finally:
                connection.close()
            if referenced is None:
                path.unlink(missing_ok=True)
                self._sync_files_root()
        except (sqlite3.Error, OSError):
            pass


def submission_descriptor(report_id: str, version: int, *, source: str, content_sha: str,
                          revision: str, metadata: dict) -> dict:
    """Bind a trusted approval to all immutable input fields, never just its ID."""
    _identity(report_id, version)
    _revision(revision)
    metadata_json = _metadata(metadata)
    if source not in {"text", "file"} or not isinstance(content_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", content_sha):
        raise InputValidationError("Invalid source content fingerprint")
    value = {"report_id": report_id, "version": version, "source": source,
             "content_sha": content_sha, "revision": revision, "metadata": json.loads(metadata_json)}
    encoded = json.dumps({"protocol": "clew-intake-v1", **value}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**value, "fingerprint": hashlib.sha256(encoded).hexdigest()}
