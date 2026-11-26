"""Bounded, durable snapshots of observed CLEW chat executions."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time


RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_RUNS = 5_000


class AgentRunConflict(ValueError):
    """The request ID already identifies an incompatible snapshot."""


class AgentRunStore:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "agent-runs.sqlite"
        self.path.touch(mode=0o600, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    request_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    summary_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS agent_runs_recent
                    ON agent_runs(started_at DESC, request_id DESC);
                CREATE INDEX IF NOT EXISTS agent_runs_session
                    ON agent_runs(session_id, started_at DESC, request_id DESC);
                CREATE INDEX IF NOT EXISTS agent_runs_retention
                    ON agent_runs(received_at DESC, request_id DESC);
            """)
            self._prune_expired(connection, time.time())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA secure_delete = ON")
        return connection

    @staticmethod
    def _prune_expired(connection: sqlite3.Connection, now: float) -> None:
        connection.execute("DELETE FROM agent_runs WHERE received_at < ?", (now - RETENTION_SECONDS,))

    @staticmethod
    def _can_replace(previous: dict, incoming: dict) -> bool:
        if any(previous[field] != incoming[field] for field in ("session_id", "started_at", "request")):
            raise AgentRunConflict
        if previous.get("origin", "clew_chat") != incoming.get("origin", "clew_chat"):
            raise AgentRunConflict
        if previous == incoming:
            return False
        if previous["status"] != "running":
            if incoming["status"] == "running":
                return False
            raise AgentRunConflict
        before, after = previous["steps"], incoming["steps"]
        if incoming["status"] == "running" and len(after) <= len(before):
            if before[:len(after)] == after:
                return False
            raise AgentRunConflict
        if after[:len(before)] != before:
            raise AgentRunConflict
        return True

    def save(self, run: dict) -> None:
        snapshot = json.dumps(run, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":"))
        summary = {key: run[key] for key in (
            "version", "request_id", "session_id", "started_at", "finished_at", "status"
        )}
        if "origin" in run:
            summary["origin"] = run["origin"]
        summary.update(
            response_type=(run["response"] or {}).get("type"),
            step_count=len(run["steps"]),
            kinds=list(dict.fromkeys(step["kind"] for step in run["steps"])),
        )
        now = time.time()
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._prune_expired(connection, now)
            previous = connection.execute(
                "SELECT snapshot_json FROM agent_runs WHERE request_id = ?", (run["request_id"],),
            ).fetchone()
            if previous and not self._can_replace(json.loads(previous["snapshot_json"]), run):
                return
            connection.execute(
                "INSERT INTO agent_runs (request_id, session_id, started_at, received_at, snapshot_json, summary_json) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(request_id) DO UPDATE SET received_at = excluded.received_at, "
                "snapshot_json = excluded.snapshot_json, summary_json = excluded.summary_json",
                (run["request_id"], run["session_id"], run["started_at"], now, snapshot, json.dumps(summary)),
            )
            connection.execute(
                "DELETE FROM agent_runs WHERE request_id IN ("
                "SELECT request_id FROM agent_runs ORDER BY received_at DESC, request_id DESC LIMIT -1 OFFSET ?)",
                (MAX_RUNS,),
            )

    def recent(self, limit: int, session_id: str | None) -> list[dict]:
        query = "SELECT summary_json FROM agent_runs WHERE received_at >= ?"
        values: list[int | float | str] = [time.time() - RETENTION_SECONDS]
        if session_id is not None:
            query += " AND session_id = ?"
            values.append(session_id)
        query += " ORDER BY started_at DESC, request_id DESC LIMIT ?"
        values.append(limit)
        with closing(self._connect()) as connection:
            return [json.loads(row["summary_json"]) for row in connection.execute(query, values)]

    def get(self, request_id: str) -> dict | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM agent_runs WHERE request_id = ? AND received_at >= ?",
                (request_id, time.time() - RETENTION_SECONDS),
            ).fetchone()
        return json.loads(row["snapshot_json"]) if row else None
