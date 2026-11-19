"""Restartable worker for persisted CLEW jobs; no model work runs in the API."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import signal
import threading
import time
from uuid import uuid4

import yaml

from clew_okf.stages.step_01_ingest.bootstrap import scan_database
from clew_okf.workflows.compiler import BuildInProgressError
from clew_okf.domain.errors import RevalidationRequiredError
from clew_okf.runtime.progress import emit, reporting
from clew_okf.runtime.service import WikiService
from clew_okf.stages.step_01_ingest.report_store import CanonicalReportHoldError


class LeaseLostError(RuntimeError):
    pass


class ConfigurationChangedError(ValueError):
    pass


class WikiWorker:
    def __init__(self, service: WikiService):
        self.service = service
        self.owner = f"{os.getpid()}-{uuid4().hex}"
        self.stopping = threading.Event()

    def stop(self) -> None:
        self.stopping.set()

    @contextmanager
    def _lease(self, job: dict, expected_config: str):
        lost = threading.Event()
        changed = threading.Event()
        finished = threading.Event()
        store = self.service.jobs
        token = job["lease_token"]
        def keep_alive():
            while not finished.wait(max(1, self.service.lease_seconds / 3)):
                try:
                    valid = store.heartbeat(job["id"], token, lease_seconds=self.service.lease_seconds)
                except Exception:
                    valid = False
                if not valid:
                    lost.set()
                    return
        thread = threading.Thread(target=keep_alive, name="clew-job-heartbeat", daemon=True)
        thread.start()
        def progress(stage, message, details):
            if self.service._signature() != expected_config:
                changed.set()
                raise ConfigurationChangedError("Service configuration changed")
            if lost.is_set() or not store.progress(job["id"], token, stage, message, details):
                lost.set()
                raise LeaseLostError("The job execution lease is no longer valid")
        try:
            with reporting(progress):
                yield lost, changed
        finally:
            finished.set()
            thread.join(timeout=2)

    def run_once(self) -> dict | None:
        store = self.service.jobs
        store.reconcile_parents()
        job = store.claim(self.owner, lease_seconds=self.service.lease_seconds)
        if job is None:
            return None
        token = job["lease_token"]
        try:
            config = self.service._read_config()
            signature = self.service._signature(config)
            if job["payload"].get("base_digest", signature) != signature:
                raise ConfigurationChangedError("Queued source configuration changed")
            with self._lease(job, signature) as (lost, changed):
                emit("starting", "영속 작업 큐에서 작업을 시작합니다.", kind=job["kind"],
                     report_id=job["payload"].get("report_id"), version=job["payload"].get("version"),
                     source=job["payload"].get("source"))
                if job["kind"] == "bootstrap":
                    result = self._bootstrap(job, config, signature)
                    if result is None:
                        return store.get(job["id"])
                elif job["kind"] == "build":
                    result = self._agent_for(job, config).run()
                elif job["kind"] == "report":
                    payload = job["payload"]
                    agent = self._agent_for(job, config)
                    source = payload["source"]
                    if source in {"managed_text", "file", "managed"}:
                        from clew_okf.stages.step_01_ingest.managed_intake import lookup_managed
                        managed = lookup_managed(self.service.state_db, payload["report_id"], payload["version"])
                        if managed is not None and (managed["status"] != "finalized" or not managed["reuse_approved"]):
                            result = {"report_id": payload["report_id"], "version": payload["version"], "status": "hold",
                                      "findings": [{"code": "awaiting_approval", "message": "보고서 확정과 재사용 승인이 필요합니다."}]}
                            store.finish(job["id"], token, status="held", result=result)
                            return store.get(job["id"])
                    if source == "canonical":
                        result = agent.ingest_report(payload["report_id"], payload["version"])
                    else:
                        kind = "managed" if source == "managed_text" else source
                        result = agent.map_and_ingest_report(payload["report_id"], payload["version"], kind)
                else:
                    raise ValueError("Unknown job kind")
                if changed.is_set():
                    raise ConfigurationChangedError("Service configuration changed")
                if lost.is_set():
                    raise LeaseLostError("The job execution lease is no longer valid")
                status = "held" if result.get("status") in {"hold", "conflict", "rejected"} else "succeeded"
                store.finish(job["id"], token, status=status, result=result)
        except CanonicalReportHoldError as error:
            store.finish(job["id"], token, status="held", result={"status": "hold", "findings": [{
                "code": error.code, "message": error.public_message}]})
        except RevalidationRequiredError as error:
            store.finish(job["id"], token, status="held", result={"status": "hold", "findings": [{
                "code": error.code, "message": str(error)}]})
        except ConfigurationChangedError:
            store.finish(job["id"], token, status="held", result={"status": "hold", "findings": [{
                "code": "configuration_changed", "message": "대기 또는 실행 중 서버 설정이 바뀌었습니다. 현재 설정으로 다시 요청하세요."}]})
        except LeaseLostError:
            # A recovered execution owns this job now. Never overwrite its result.
            pass
        except BuildInProgressError:
            store.defer(job["id"], token, delay_seconds=5, message="다른 위키 발행 작업이 끝나기를 기다립니다.", consume_attempt=False)
        except RuntimeError:
            store.defer(job["id"], token, delay_seconds=3, message="원본 또는 모델 실행을 완료하지 못해 재시도를 기다립니다.")
        except (ValueError, yaml.YAMLError, OSError, KeyError, TypeError):
            store.finish(job["id"], token, status="failed", error={
                "code": "invalid_input_or_storage", "message": "입력·설정 또는 저장소를 확인하세요. 기존 위키는 유지됩니다."})
        except Exception:
            store.finish(job["id"], token, status="failed", error={
                "code": "worker_failed", "message": "작업을 완료하지 못했습니다. 서버 기록을 확인하세요."})
        finally:
            store.reconcile_parents()
        return store.get(job["id"])

    def _agent_for(self, job: dict, config: dict):
        path_value = job["payload"].get("config_path")
        path = Path(path_value) if path_value else self.service.effective_config(base_config=config)
        if path.is_symlink() or not path.resolve().is_relative_to(self.service.state_dir / "configs") or not path.is_file():
            raise ValueError("Job runtime configuration is invalid")
        return self.service.agent_factory(path)

    def _bootstrap(self, job: dict, config: dict, signature: str) -> dict | None:
        emit("scanning_database", "허용된 DB 스키마·컬럼·키와 기존 승인 보고서를 확인합니다.")
        plan = scan_database(config)
        if plan["status"] != "ready":
            return {**plan, "status": "hold"}
        emit("building_initial_wiki", "설비와 DB 구조·설정된 현행 근거로 첫 위키를 구성합니다.", **plan["counts"])
        runtime = self.service.write_runtime_config(plan["proposed_postgres"], base_config=config)
        built = self.service.agent_factory(runtime).run()
        self.service.activate_config(runtime, base_digest=signature)
        children = [
            {"kind": "report", "payload": {**report, "config_path": str(runtime), "base_digest": signature},
             "dedupe_key": f"bootstrap:{job['id']}:{report['source']}:{report['report_id']}@{report['version']}"}
            for report in plan["report_jobs"]
        ]
        result = {"status": "initialized", "scan": plan, "build": built,
                  "report_jobs": len(children), "findings": plan["findings"]}
        if not children:
            return result
        emit("queueing_existing_reports", "초기 보고서를 개별 편입 검증 작업으로 등록합니다.", total=len(children))
        self.service.jobs.set_waiting(job["id"], job["lease_token"], result=result, children=children)
        return None

    def run(self, *, once: bool = False) -> None:
        self.service.start()
        while not self.stopping.is_set():
            result = self.run_once()
            if once:
                return
            if result is None:
                self.stopping.wait(self.service.poll_seconds)


def run_worker(config_path: Path, *, once: bool = False) -> None:
    worker = WikiWorker(WikiService(config_path))
    def stop(_signum, _frame):
        worker.stop()
    for name in (signal.SIGTERM, signal.SIGINT):
        signal.signal(name, stop)
    worker.run(once=once)
