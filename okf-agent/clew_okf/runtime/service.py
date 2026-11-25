"""Persistent service integration for saves, uploads, bootstrap and background jobs."""

from __future__ import annotations

import copy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import yaml

from clew_okf.agent import WikiAgent
from clew_okf.stages.step_06_publish.bundle import digest
from clew_okf.runtime.jobs import JobStore, enqueue_in_transaction
from clew_okf.stages.step_01_ingest.managed_intake import ManagedIntake
from clew_okf.domain.reports import ReportRequest, ReportIdentifier
from pydantic import TypeAdapter


_SERVICE_KEYS = {"state_dir", "max_upload_bytes", "bootstrap_on_startup", "lease_seconds", "poll_seconds"}


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a mapping")
    return value


def _resolve_path(value: object, base: Path, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a path")
    return (base / value).resolve()


def _config_value(value):
    # YAML/SQL filters support native dates. Keep their type in the stable key.
    if isinstance(value, (date, datetime)):
        return {"__yaml_type__": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, dict):
        return {key: _config_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_config_value(item) for item in value]
    return value


class WikiService:
    """Store input and its job atomically; execute expensive work in a separate worker."""

    def __init__(self, config_path: str | Path, *, agent_factory=WikiAgent):
        self.config_path = Path(config_path).resolve()
        self.base = self.config_path.parent
        self.agent_factory = agent_factory
        config = self._read_config()
        settings = _object(config.get("service", {}), "service")
        if settings.keys() - _SERVICE_KEYS:
            raise ValueError("Unsupported service settings")
        self.state_dir = _resolve_path(settings.get("state_dir", "./service-state"), self.base, "service.state_dir")
        output = _resolve_path(config.get("output", "output"), self.base, "output")
        if self.state_dir.is_relative_to(output) or output.is_relative_to(self.state_dir):
            raise ValueError("Service state and published wiki output must not overlap")
        self.output = output
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_db = self.state_dir / "state.sqlite"
        self.jobs = JobStore(self.state_db)
        with self.jobs.transaction() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS service_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        mapping = config.get("admission", {}).get("mapping", {})
        files = mapping.get("files", {})
        self.files_root = _resolve_path(files.get("root", str(self.state_dir / "uploads")), self.base, "mapping.files.root")
        if self.files_root.is_relative_to(output) or output.is_relative_to(self.files_root):
            raise ValueError("Uploaded originals and published output must not overlap")
        documents = config.get("documents", {})
        if documents.get("root"):
            normal_root = _resolve_path(documents["root"], self.base, "documents.root")
            if self.files_root.is_relative_to(normal_root) or normal_root.is_relative_to(self.files_root):
                raise ValueError("Uploads must use a separate tree from directly collected documents")
        self.files_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        limit = settings.get("max_upload_bytes", 64 * 1024 * 1024)
        if type(limit) is not int or limit < 1:
            raise ValueError("service.max_upload_bytes must be a positive integer")
        self.intake = ManagedIntake(self.state_db, self.files_root, max_upload_bytes=limit)
        lease = settings.get("lease_seconds", 60)
        if type(lease) is not int or lease < 5:
            raise ValueError("service.lease_seconds must be an integer of at least 5")
        self.lease_seconds = lease
        poll = settings.get("poll_seconds", 1.0)
        if type(poll) not in (int, float) or not 0.05 <= poll <= 60:
            raise ValueError("service.poll_seconds must be between 0.05 and 60")
        self.poll_seconds = float(poll)
        startup = settings.get("bootstrap_on_startup", True)
        if type(startup) is not bool:
            raise ValueError("service.bootstrap_on_startup must be boolean")
        self.bootstrap_on_startup = startup

    def _read_config(self) -> dict:
        return _object(yaml.safe_load(self.config_path.read_text(encoding="utf-8")), "wiki config")

    def _signature(self, config: dict | None = None) -> str:
        return digest({"config": _config_value(self._read_config() if config is None else config), "service_protocol": 2})

    def _normalize_config(self, config: dict) -> dict:
        value = copy.deepcopy(config)
        value["output"] = str(self.output)
        if "equipment_csv" in value:
            value["equipment_csv"] = str(_resolve_path(value["equipment_csv"], self.base, "equipment_csv"))
        documents = value.get("documents", {})
        if "root" in documents:
            documents["root"] = str(_resolve_path(documents["root"], self.base, "documents.root"))
        for settings in [documents, value.get("admission", {}).get("mapping", {}).get("files", {})]:
            executable = settings.get("kordoc")
            if isinstance(executable, str) and "/" in executable:
                settings["kordoc"] = str((self.base / executable).resolve())
        admission = value.setdefault("admission", {})
        mapping = admission.setdefault("mapping", {})
        files = mapping.setdefault("files", {})
        registry = files.get("registry")
        if registry is not None and _resolve_path(registry, self.base, "mapping.files.registry") != self.state_db:
            raise ValueError("Service uploads must use this service's managed registry")
        files["root"] = str(self.files_root)
        files["registry"] = str(self.state_db)
        # A service-managed upload uses the same configured parser by default.
        files.setdefault("kordoc", documents.get("kordoc", "kordoc"))
        files.setdefault("timeout_seconds", documents.get("timeout_seconds", 120))
        files.setdefault("max_bytes", self.intake.max_upload_bytes)
        mapping["managed"] = {"registry": str(self.state_db)}
        value.setdefault("service", {})["state_dir"] = str(self.state_dir)
        return value

    def write_runtime_config(self, postgres: dict | None = None, *, base_config: dict | None = None) -> Path:
        config = self._normalize_config(self._read_config() if base_config is None else base_config)
        if postgres is not None:
            config["postgres"] = copy.deepcopy(postgres)
        directory = self.state_dir / "configs"
        directory.mkdir(exist_ok=True)
        path = directory / f"{digest(_config_value(config))}.yaml"
        if not path.exists():
            temporary = directory / f".{uuid4().hex}.tmp"
            try:
                temporary.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return path

    def effective_config(self, *, base_config: dict | None = None) -> Path:
        config = self._read_config() if base_config is None else base_config
        with self.jobs.transaction() as connection:
            row = connection.execute("SELECT value FROM service_meta WHERE key='active_config'").fetchone()
        if row:
            active = json.loads(row[0])
            path = Path(active["path"])
            if (active["base_digest"] == self._signature(config) and path.is_file()
                    and not path.is_symlink() and path.resolve().is_relative_to(self.state_dir / "configs")):
                return path
        return self.write_runtime_config(base_config=config)

    def activate_config(self, path: Path, *, base_digest: str | None = None) -> None:
        if not path.resolve().is_relative_to(self.state_dir / "configs"):
            raise ValueError("Runtime configuration is outside service state")
        expected = base_digest or self._signature()
        if self._signature() != expected:
            raise ValueError("Configuration changed during initialization")
        with self.jobs.transaction() as connection:
            connection.execute("INSERT INTO service_meta(key,value) VALUES('active_config',?) "
                               "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (json.dumps({"path": str(path), "base_digest": expected}),))

    def agent(self):
        return self.agent_factory(self.effective_config())

    def start(self) -> dict | None:
        """Queue first-setup scanning once; this method performs no DB/model work."""
        if self.bootstrap_on_startup and self._read_config().get("postgres") and not (self.output / "current").exists():
            return self.enqueue_bootstrap()
        return None

    def _enqueue_current(self, kind: str, payload: dict, *, key: str, retry: bool = False) -> dict:
        config = self._read_config()
        signature = self._signature(config)
        payload = {**payload, "base_digest": signature}
        prefix = f"{kind}:{digest({'key': key, 'config': signature})}:"
        with self.jobs.transaction() as connection:
            if not retry:
                row = connection.execute("SELECT id FROM jobs WHERE dedupe_key LIKE ? "
                    "AND status IN ('queued','running','waiting') ORDER BY created_at DESC LIMIT 1", (prefix + "%",)).fetchone()
                if row:
                    identifier = row[0]
                else:
                    identifier = None
            else:
                identifier = None
            if identifier is None:
                job = enqueue_in_transaction(connection, kind, payload, prefix + uuid4().hex)
                source = payload.get("source")
                managed_source = "text" if source in {"managed", "managed_text"} else "file" if source == "file" else None
                if kind == "report" and managed_source is not None:
                    connection.execute("UPDATE managed_reports SET last_job_id=? WHERE report_id=? AND version=? AND source=?",
                        (job["id"], payload["report_id"], payload["version"], managed_source))
                return job
        return self.jobs.get(identifier)

    def enqueue_bootstrap(self, *, retry: bool = False) -> dict:
        return self._enqueue_current("bootstrap", {}, key="bootstrap", retry=retry)

    def enqueue_build(self, *, retry: bool = False) -> dict:
        return self._enqueue_current("build", {}, key="build", retry=retry)

    def queue_report(self, report_id: str, version: int, *, source: str, retry: bool = False) -> dict:
        TypeAdapter(ReportIdentifier).validate_python(report_id, strict=True)
        ReportRequest(version=version)
        if source not in {"canonical", "database", "file", "managed"}:
            raise ValueError("Invalid report source")
        return self._enqueue_current("report", {"report_id": report_id, "version": version, "source": source},
                                     key=f"saved:{source}:{report_id}@{version}", retry=retry)

    def report_saved(self, report_id: str, version: int, *, source: str = "canonical", retry: bool = False) -> dict:
        """Call after the main service commits a report in its existing DB."""
        if source not in {"canonical", "database"}:
            raise ValueError("Saved external source must be canonical or database")
        return self.queue_report(report_id, version, source=source, retry=retry)

    def save_report(self, report_id: str, version: int, *, body: str, revision: str,
                    metadata: dict | None = None, status: str = "draft", reuse_approved: bool = False) -> dict:
        return self.intake.save_text(report_id, version, body=body, revision=revision,
                                     metadata=metadata or {}, status=status, reuse_approved=reuse_approved)

    def upload_report(self, report_id: str, version: int, *, stream: BinaryIO, filename: str,
                      revision: str, metadata: dict | None = None, status: str = "draft",
                      reuse_approved: bool = False) -> dict:
        return self.intake.save_file(report_id, version, stream=stream, filename=filename,
                                     revision=revision, metadata=metadata or {}, status=status,
                                     reuse_approved=reuse_approved)

    def set_report_approval(self, report_id: str, version: int, *, status: str, reuse_approved: bool) -> dict:
        """Trusted backend hook; intentionally not an anonymous approval HTTP endpoint."""
        return self.intake.set_approval(report_id, version, status=status, reuse_approved=reuse_approved)

    def retry_job(self, job_id: str) -> dict:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job["status"] not in {"held", "failed"}:
            raise ValueError("Only held or failed jobs may be retried")
        payload = {key: value for key, value in job["payload"].items() if key not in {"config_path", "base_digest"}}
        return self._enqueue_current(job["kind"], payload, key=f"retry:{job_id}", retry=True)

    def job_status(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        result = job.get("result") or {}
        reasons = list(result.get("findings") or [])
        review = result.get("review") or {}
        if job["status"] == "held" and review.get("decision") == "hold" and review.get("reason"):
            reasons.append({"code": "admission_hold", "message": review["reason"],
                            "evidence_ids": review.get("evidence_ids", [])})
        if job["status"] == "held" and not reasons and result.get("children", {}).get("held"):
            reasons.append({"code": "child_reports_held", "message": "일부 기존 보고서가 보류됐습니다. 하위 작업의 사유를 확인하세요."})
        job["hold_reasons"] = reasons
        return job

    def child_jobs(self, job_id: str, *, limit: int = 100, offset: int = 0) -> list[dict]:
        with self.jobs.transaction() as connection:
            identifiers = [row[0] for row in connection.execute(
                "SELECT id FROM jobs WHERE parent_id=? ORDER BY created_at,id LIMIT ? OFFSET ?",
                (job_id, limit, offset)).fetchall()]
        return [self.job_status(identifier) for identifier in identifiers]

    def report_status(self, report_id: str, version: int) -> dict | None:
        TypeAdapter(ReportIdentifier).validate_python(report_id, strict=True)
        ReportRequest(version=version)
        with self.jobs.transaction() as connection:
            row = connection.execute("SELECT last_job_id FROM managed_reports WHERE report_id=? AND version=?",
                                     (report_id, version)).fetchone()
        return self.job_status(row[0]) if row and row[0] else None
