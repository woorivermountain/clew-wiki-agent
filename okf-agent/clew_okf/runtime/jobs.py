"""Durable serialized work queue with expiring, fenced execution leases."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

_KINDS = frozenset({"build", "bootstrap", "report"})
_TERMINAL = frozenset({"succeeded", "held", "failed"})
_MESSAGES = {"succeeded": "Completed", "held": "Requires attention", "failed": "Failed"}
_EXHAUSTED = {"code": "attempts_exhausted", "message": "The job exceeded its retry limit."}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('build', 'bootstrap', 'report')),
    payload_json TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'waiting', 'succeeded', 'held', 'failed')),
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    progress_json TEXT NOT NULL,
    result_json TEXT,
    error_json TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    parent_id TEXT REFERENCES jobs(id),
    available_at REAL NOT NULL,
    lease_token TEXT,
    lease_owner TEXT,
    lease_expires_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_running ON jobs((1)) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS jobs_available ON jobs(status, available_at);
CREATE INDEX IF NOT EXISTS jobs_parent ON jobs(parent_id);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS job_events_job ON job_events(job_id, id);
"""


def _timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).isoformat(timespec="microseconds")


def _duration(value: float, name: str, *, allow_zero: bool = False) -> float:
    if (
        type(value) not in (int, float) or not math.isfinite(value)
        or value < 0 or (value == 0 and not allow_zero)
    ):
        raise ValueError(f"{name} must be a {'nonnegative' if allow_zero else 'positive'} finite number")
    return float(value)


def _json_value(value: object) -> bool:
    if isinstance(value, dict):
        return all(isinstance(key, str) and _json_value(item) for key, item in value.items())
    if isinstance(value, list):
        return all(_json_value(item) for item in value)
    return value is None or type(value) in (str, bool, int, float)


def _object_json(value: object, name: str) -> str:
    try:
        if not isinstance(value, dict) or not _json_value(value):
            raise ValueError
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError(f"{name} must be a JSON object") from None


def _public(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "payload": json.loads(row["payload_json"]),
        "status": row["status"],
        "stage": row["stage"],
        "message": row["message"],
        "progress": json.loads(row["progress_json"]),
        "result": json.loads(row["result_json"]) if row["result_json"] is not None else None,
        "error": json.loads(row["error_json"]) if row["error_json"] is not None else None,
        "attempts": row["attempts"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "parent_id": row["parent_id"],
    }


def _event(conn: sqlite3.Connection, job_id: str, now: float, details: dict | None = None) -> None:
    row = conn.execute("SELECT status, stage, message FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.execute(
        "INSERT INTO job_events(job_id, status, stage, message, details_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, row["status"], row["stage"], row["message"], _object_json(details or {}, "details"),
         _timestamp(now)),
    )


def enqueue_in_transaction(
    conn: sqlite3.Connection,
    kind: str,
    payload: dict,
    dedupe_key: str,
    parent_id: str | None = None,
    *,
    now: float | None = None,
    max_attempts: int = 3,
) -> dict:
    """Enqueue using a caller's existing BEGIN IMMEDIATE transaction."""
    if not conn.in_transaction:
        raise RuntimeError("Enqueue requires an existing transaction")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError("Job kind must be build, bootstrap, or report")
    payload_json = _object_json(payload, "payload")
    if not isinstance(dedupe_key, str) or not dedupe_key.strip():
        raise ValueError("dedupe_key must be a nonempty string")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    if parent_id is not None and (
        not isinstance(parent_id, str)
        or conn.execute("SELECT 1 FROM jobs WHERE id = ?", (parent_id,)).fetchone() is None
    ):
        raise ValueError("parent_id must identify an existing job")

    existing = conn.execute("SELECT * FROM jobs WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
    if existing is not None:
        if existing["kind"] != kind or existing["payload_json"] != payload_json or existing["parent_id"] != parent_id:
            raise ValueError("dedupe_key already identifies a different job")
        return _public(existing)

    moment = time.time() if now is None else now
    timestamp = _timestamp(moment)
    identifier = uuid4().hex
    conn.execute(
        "INSERT INTO jobs "
        "(id, kind, payload_json, dedupe_key, status, stage, message, progress_json, "
        "max_attempts, created_at, updated_at, parent_id, available_at) "
        "VALUES (?, ?, ?, ?, 'queued', 'queued', 'Queued', '{}', ?, ?, ?, ?, ?)",
        (identifier, kind, payload_json, dedupe_key, max_attempts, timestamp, timestamp, parent_id, moment),
    )
    _event(conn, identifier, moment)
    return _public(conn.execute("SELECT * FROM jobs WHERE id = ?", (identifier,)).fetchone())


def get_in_transaction(conn: sqlite3.Connection, job_id: str) -> dict | None:
    """Read the public job while atomically updating related input state."""
    if not conn.in_transaction:
        raise RuntimeError("A job lookup requires an existing transaction")
    row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return _public(row) if row is not None else None


class JobStore:
    """Store one wiki's queue on disk; each operation opens its own connection."""

    def __init__(
        self,
        path: Path,
        *,
        now: Callable[[], float] | None = None,
        max_attempts: int = 3,
    ) -> None:
        if now is not None and not callable(now):
            raise ValueError("now must be a time provider")
        if type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or time.time
        self.max_attempts = max_attempts
        for attempt in range(3):
            conn = None
            try:
                conn = self._connect()
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                break
            except sqlite3.OperationalError as error:
                if not self._busy(error) or attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))
            finally:
                if conn is not None:
                    conn.close()

    @staticmethod
    def _busy(error: sqlite3.OperationalError) -> bool:
        return (getattr(error, "sqlite_errorcode", 0) & 255) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

    def _connect(self) -> sqlite3.Connection:
        for attempt in range(3):
            conn = None
            try:
                conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 5000")
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA synchronous = FULL")
                return conn
            except sqlite3.OperationalError as error:
                if conn is not None:
                    conn.close()
                if not self._busy(error) or attempt == 2:
                    raise
                time.sleep(0.02 * (attempt + 1))
        raise RuntimeError("Could not connect to job state")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Share an atomic transaction with related tables in the same database."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def enqueue(self, kind: str, payload: dict, *, dedupe_key: str, parent_id: str | None = None) -> dict:
        with self.transaction() as conn:
            return enqueue_in_transaction(
                conn, kind, payload, dedupe_key, parent_id, now=self._now(), max_attempts=self.max_attempts,
            )

    def get(self, job_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return _public(row) if row is not None else None
        finally:
            conn.close()

    def list_jobs(self, limit: int = 50) -> list:
        if type(limit) is not int or limit < 1:
            raise ValueError("limit must be a positive integer")
        conn = self._connect()
        try:
            return [_public(row) for row in conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,),
            ).fetchall()]
        finally:
            conn.close()

    def claim(self, owner: str, lease_seconds: float = 60) -> dict | None:
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be a nonempty string")
        lease_seconds = _duration(lease_seconds, "lease_seconds")
        now = self._now()
        with self.transaction() as conn:
            active = conn.execute("SELECT * FROM jobs WHERE status = 'running'").fetchone()
            if active is not None:
                if active["lease_expires_at"] > now:
                    return None
                if active["attempts"] >= active["max_attempts"]:
                    self._terminal(conn, active["id"], now, "failed", None, _EXHAUSTED)
                else:
                    conn.execute(
                        "UPDATE jobs SET status='queued', stage='queued', message=?, updated_at=?, "
                        "available_at=?, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL "
                        "WHERE id=?",
                        ("Retrying interrupted work", _timestamp(now), now, active["id"]),
                    )
                    _event(conn, active["id"], now)
            self._reconcile(conn, now)
            while True:
                row = conn.execute(
                    "SELECT * FROM jobs WHERE status='queued' AND available_at<=? "
                    "ORDER BY created_at, rowid LIMIT 1", (now,),
                ).fetchone()
                if row is None:
                    return None
                if row["attempts"] >= row["max_attempts"]:
                    self._terminal(conn, row["id"], now, "failed", None, _EXHAUSTED)
                    self._reconcile(conn, now)
                    continue
                token = uuid4().hex
                conn.execute(
                    "UPDATE jobs SET status='running', stage='running', message='Running', "
                    "attempts=attempts+1, started_at=COALESCE(started_at, ?), updated_at=?, "
                    "lease_token=?, lease_owner=?, lease_expires_at=? WHERE id=?",
                    (_timestamp(now), _timestamp(now), token, owner, now + lease_seconds, row["id"]),
                )
                _event(conn, row["id"], now)
                claimed = _public(conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
                return {**claimed, "lease_token": token}

    @staticmethod
    def _leased(conn: sqlite3.Connection, job_id: str, lease_token: str, now: float) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM jobs WHERE id=? AND status='running' AND lease_token=? AND lease_expires_at>?",
            (job_id, lease_token, now),
        ).fetchone()

    def heartbeat(self, job_id: str, lease_token: str, lease_seconds: float = 60) -> bool:
        lease_seconds = _duration(lease_seconds, "lease_seconds")
        now = self._now()
        with self.transaction() as conn:
            if self._leased(conn, job_id, lease_token, now) is None:
                return False
            conn.execute(
                "UPDATE jobs SET updated_at=?, lease_expires_at=? WHERE id=?",
                (_timestamp(now), now + lease_seconds, job_id),
            )
            return True

    def progress(
        self,
        job_id: str,
        lease_token: str,
        stage: str,
        message: str,
        details: dict | None = None,
    ) -> bool:
        if not isinstance(stage, str) or not stage.strip() or not isinstance(message, str):
            raise ValueError("stage and message must be strings, with a nonempty stage")
        if details is not None:
            _object_json(details, "details")
        now = self._now()
        with self.transaction() as conn:
            row = self._leased(conn, job_id, lease_token, now)
            if row is None:
                return False
            progress = {**json.loads(row["progress_json"]), **(details or {})}
            conn.execute(
                "UPDATE jobs SET stage=?, message=?, progress_json=?, updated_at=? WHERE id=?",
                (stage, message, _object_json(progress, "progress"), _timestamp(now), job_id),
            )
            _event(conn, job_id, now, details)
            return True

    def finish(
        self,
        job_id: str,
        lease_token: str,
        *,
        status: str,
        result: dict | None = None,
        error: dict | None = None,
    ) -> bool:
        if not isinstance(status, str) or status not in _TERMINAL:
            raise ValueError("A finished job must be succeeded, held, or failed")
        if result is not None:
            _object_json(result, "result")
        if error is not None:
            _object_json(error, "error")
        now = self._now()
        with self.transaction() as conn:
            if self._leased(conn, job_id, lease_token, now) is None:
                return False
            self._terminal(conn, job_id, now, status, result, error)
            self._reconcile(conn, now)
            return True

    @staticmethod
    def _terminal(
        conn: sqlite3.Connection,
        job_id: str,
        now: float,
        status: str,
        result: dict | None,
        error: dict | None,
    ) -> None:
        if status == "failed" and error is None:
            error = {"code": "job_failed", "message": "The job could not be completed."}
        conn.execute(
            "UPDATE jobs SET status=?, stage=?, message=?, result_json=?, error_json=?, "
            "updated_at=?, finished_at=?, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=?",
            (status, status, _MESSAGES[status], _object_json(result, "result") if result is not None else None,
             _object_json(error, "error") if error is not None else None,
             _timestamp(now), _timestamp(now), job_id),
        )
        _event(conn, job_id, now)

    def defer(
        self, job_id: str, lease_token: str, *, delay_seconds: float = 2,
        message: str = "Waiting to retry",
        consume_attempt: bool = True,
    ) -> bool:
        delay_seconds = _duration(delay_seconds, "delay_seconds", allow_zero=True)
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        if type(consume_attempt) is not bool:
            raise ValueError("consume_attempt must be a boolean")
        now = self._now()
        with self.transaction() as conn:
            row = self._leased(conn, job_id, lease_token, now)
            if row is None:
                return False
            if consume_attempt and row["attempts"] >= row["max_attempts"]:
                self._terminal(conn, job_id, now, "failed", None, _EXHAUSTED)
                self._reconcile(conn, now)
            else:
                attempts = row["attempts"] if consume_attempt else row["attempts"] - 1
                conn.execute(
                    "UPDATE jobs SET status='queued', stage='queued', message=?, updated_at=?, "
                    "available_at=?, attempts=?, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL "
                    "WHERE id=?",
                    (message, _timestamp(now), now + delay_seconds, attempts, job_id),
                )
                _event(conn, job_id, now)
            return True

    def set_waiting(
        self,
        job_id: str,
        lease_token: str,
        *,
        result: dict,
        children: list[dict],
    ) -> list:
        result_json = _object_json(result, "result")
        if not isinstance(children, list) or any(
            not isinstance(child, dict) or child.keys() != {"kind", "payload", "dedupe_key"}
            for child in children
        ):
            raise ValueError("children must contain kind, payload, and dedupe_key")
        now = self._now()
        with self.transaction() as conn:
            if self._leased(conn, job_id, lease_token, now) is None:
                return []
            queued = {}
            for child in children:
                item = enqueue_in_transaction(
                    conn, child["kind"], child["payload"], child["dedupe_key"], job_id,
                    now=now, max_attempts=self.max_attempts,
                )
                queued[item["id"]] = item
            conn.execute(
                "UPDATE jobs SET status='waiting', stage='waiting', message='Waiting for child jobs', "
                "result_json=?, updated_at=?, lease_token=NULL, lease_owner=NULL, lease_expires_at=NULL "
                "WHERE id=?",
                (result_json, _timestamp(now), job_id),
            )
            _event(conn, job_id, now)
            self._reconcile(conn, now)
            return list(queued.values())

    def reconcile_parents(self) -> None:
        with self.transaction() as conn:
            self._reconcile(conn, self._now())

    def _reconcile(self, conn: sqlite3.Connection, now: float) -> None:
        changed = True
        while changed:
            changed = False
            parents = conn.execute(
                "SELECT * FROM jobs WHERE status='waiting' ORDER BY created_at DESC, rowid DESC",
            ).fetchall()
            for parent in parents:
                rows = conn.execute("SELECT status FROM jobs WHERE parent_id=?", (parent["id"],)).fetchall()
                counts = Counter(row["status"] for row in rows)
                summary = {status: counts[status] for status in ("queued", "running", "waiting", "succeeded", "held", "failed")}
                processed = sum(counts[status] for status in _TERMINAL)
                summary.update(total=len(rows), processed=processed)
                result = {**json.loads(parent["result_json"] or "{}"), "children": summary}
                progress = {**json.loads(parent["progress_json"]),
                            "scanned": len(rows), "processed": processed, "total": len(rows)}
                if processed == len(rows):
                    status = "failed" if counts["failed"] else "held" if counts["held"] else "succeeded"
                    error = {"code": "child_failed", "message": "One or more child jobs failed."} if status == "failed" else None
                    conn.execute("UPDATE jobs SET progress_json=? WHERE id=?",
                                 (_object_json(progress, "progress"), parent["id"]))
                    self._terminal(conn, parent["id"], now, status, result, error)
                    changed = True
                else:
                    conn.execute(
                        "UPDATE jobs SET progress_json=?, result_json=?, updated_at=? WHERE id=?",
                        (_object_json(progress, "progress"), _object_json(result, "result"),
                         _timestamp(now), parent["id"]),
                    )

    def get_events(self, job_id: str, after: int = 0) -> list:
        if type(after) is not int or after < 0:
            raise ValueError("after must be a nonnegative integer")
        conn = self._connect()
        try:
            return [
                {"id": row["id"], "job_id": row["job_id"], "status": row["status"],
                 "stage": row["stage"], "message": row["message"],
                 "details": json.loads(row["details_json"]), "created_at": row["created_at"]}
                for row in conn.execute(
                    "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id", (job_id, after),
                ).fetchall()
            ]
        finally:
            conn.close()
