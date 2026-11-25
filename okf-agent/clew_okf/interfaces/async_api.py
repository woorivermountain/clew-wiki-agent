"""FastAPI adapters that store/queue quickly and expose durable job progress."""

from __future__ import annotations

import inspect
import hashlib
import json
from typing import Annotated, Any, Callable, Literal

from fastapi import APIRouter, Body, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from clew_okf.stages.step_01_ingest.managed_intake import InputConflictError, InputNotFoundError, InputValidationError, submission_descriptor
from clew_okf.interfaces.agent_runs import create_agent_runs_router
from clew_okf.interfaces.studio import create_studio_router
from clew_okf.interfaces.chat_lookup import create_chat_lookup_router
from clew_okf.domain.reports import Identifier, PositiveVersion, ReportIdentifier
from clew_okf.interfaces.api import WikiReleaseResponse, _get_approved_release
from clew_okf.runtime.service import WikiService


class StrictRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class IntakeMetadata(StrictRequest):
    plant_id: Identifier | None = None
    equipment_id: Identifier | None = None
    event_id: Identifier | None = None
    supersedes_version: PositiveVersion | None = None


class UploadDetails(StrictRequest):
    version: PositiveVersion
    revision: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    metadata: IntakeMetadata = Field(default_factory=IntakeMetadata)


class SaveReportRequest(UploadDetails):
    body: Annotated[str, StringConstraints(min_length=1)]


class JobRequest(StrictRequest):
    retry: bool = False


class SavedEvent(StrictRequest):
    version: PositiveVersion
    source: Literal["canonical", "database"] = "canonical"
    retry: bool = False


class RawQueuedEvent(StrictRequest):
    version: PositiveVersion
    source: Literal["file", "database", "managed"]
    retry: bool = False


class CanonicalEvent(StrictRequest):
    version: PositiveVersion
    retry: bool = False


class ApprovalDecision(StrictRequest):
    status: Literal["draft", "finalized"] = "draft"
    reuse_approved: bool = False
    approved_fingerprint: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None

    @model_validator(mode="after")
    def valid_approval(self):
        if self.reuse_approved and self.status != "finalized":
            raise ValueError("Approved reports must be finalized")
        if self.reuse_approved and self.approved_fingerprint is None:
            raise ValueError("Approval must identify the exact immutable input")
        return self


class JobAccepted(BaseModel):
    job_id: str
    status: str
    status_url: str
    report_id: str | None = None
    version: int | None = None


def _accepted(job: dict, prefix: str) -> dict:
    return {"job_id": job["id"], "status": job["status"], "status_url": f"{prefix}/jobs/{job['id']}",
            "report_id": job["payload"].get("report_id"), "version": job["payload"].get("version")}


class WikiBodyLimitMiddleware:
    """Bound request bytes before multipart spooling consumes unbounded disk."""
    def __init__(self, app, *, max_bytes: int, prefix: str = "/wiki"):
        self.app, self.max_bytes, self.prefix = app, max_bytes, prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefix + "/"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers[b"content-length"]) if b"content-length" in headers else None
        except ValueError:
            return await JSONResponse({"detail": "잘못된 Content-Length입니다."}, status_code=400)(scope, receive, send)
        if length is not None and (length < 0 or length > self.max_bytes):
            return await JSONResponse({"detail": "요청 크기가 업로드 한도를 넘었습니다."}, status_code=413)(scope, receive, send)
        consumed = 0
        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > self.max_bytes:
                    raise HTTPException(413, "요청 크기가 업로드 한도를 넘었습니다.")
            return message
        return await self.app(scope, bounded_receive, send)


def create_service_router(service: WikiService, *, prefix: str = "/wiki", approval_resolver: Callable | None = None,
                          dependencies: list | None = None) -> APIRouter:
    """Approval resolver belongs to the trusted main service, never request JSON."""
    router = APIRouter(prefix=prefix, tags=["wiki-jobs"], dependencies=dependencies or [])

    router.include_router(create_studio_router(service.config_path, jobs_available=True))
    router.include_router(create_chat_lookup_router(service.config_path))
    router.include_router(create_agent_runs_router(service.state_dir))

    @router.get("/release", response_model=WikiReleaseResponse)
    def release(response: Response) -> dict:
        return _get_approved_release(service.config_path, response)

    async def approval(report_id: str, version: int, submission: dict) -> dict:
        if approval_resolver is None:
            return {"status": "draft", "reuse_approved": False}
        try:
            value = await run_in_threadpool(approval_resolver, report_id, version, submission)
            if inspect.isawaitable(value):
                value = await value
            decision = ApprovalDecision.model_validate(value)
        except Exception:
            raise HTTPException(503, "서버의 보고서 확정·승인 상태를 확인하지 못했습니다.") from None
        if decision.reuse_approved and decision.approved_fingerprint != submission["fingerprint"]:
            raise HTTPException(409, "제출한 원본이 서버에서 승인한 원본과 다릅니다.")
        return {"status": decision.status, "reuse_approved": decision.reuse_approved}

    def text_descriptor(report_id: str, request: SaveReportRequest) -> dict:
        try:
            data = request.body.encode("utf-8")
        except UnicodeError:
            raise InputValidationError("Report text is invalid") from None
        if len(data) > service.intake.max_upload_bytes:
            raise InputValidationError("Report text is too large")
        return submission_descriptor(report_id, request.version, source="text", content_sha=hashlib.sha256(data).hexdigest(),
            revision=request.revision, metadata=request.metadata.model_dump(exclude_none=True))

    def file_descriptor(report_id: str, details: UploadDetails, stream) -> dict:
        digest = hashlib.sha256()
        length = 0
        try:
            stream.seek(0)
            while True:
                data = stream.read(min(64 * 1024, service.intake.max_upload_bytes - length + 1))
                if not data:
                    break
                length += len(data)
                if length > service.intake.max_upload_bytes:
                    raise InputValidationError("Upload is too large")
                digest.update(data)
        finally:
            stream.seek(0)
        return submission_descriptor(report_id, details.version, source="file", content_sha=digest.hexdigest(),
            revision=details.revision, metadata=details.metadata.model_dump(exclude_none=True))

    @router.post("/setup", status_code=202, response_model=JobAccepted)
    def setup(request: JobRequest = Body(default=JobRequest())):
        return _accepted(service.enqueue_bootstrap(retry=request.retry), prefix)

    @router.post("/build", status_code=202, response_model=JobAccepted)
    def build(request: JobRequest = Body(default=JobRequest())):
        return _accepted(service.enqueue_build(retry=request.retry), prefix)

    @router.post("/reports/{report_id}", status_code=202, response_model=JobAccepted)
    async def save_report(report_id: ReportIdentifier, request: SaveReportRequest):
        try:
            descriptor = await run_in_threadpool(text_descriptor, report_id, request)
            approved = await approval(report_id, request.version, descriptor)
            result = await run_in_threadpool(service.save_report, report_id, request.version, body=request.body,
                revision=request.revision, metadata=request.metadata.model_dump(exclude_none=True), **approved)
        except InputConflictError:
            raise HTTPException(409, "같은 보고서 ID·버전의 원본이 다릅니다. 새 버전을 사용하세요.") from None
        except InputValidationError:
            raise HTTPException(422, "보고서 원문과 메타데이터를 확인하세요.") from None
        return _accepted(result["job"], prefix)

    @router.post("/reports/{report_id}/upload", status_code=202, response_model=JobAccepted)
    async def upload(report_id: ReportIdentifier, request: Request, file: UploadFile = File(...), details: str = Form(...)):
        try:
            form = await request.form()
            keys = [key for key, _ in form.multi_items()]
            if sorted(keys) != ["details", "file"] or len(details) > 16_000:
                raise ValueError
            parsed = UploadDetails.model_validate_json(details, strict=True)
        except (ValueError, TypeError):
            await file.close()
            raise HTTPException(422, "file과 유효한 details JSON만 전송하세요.") from None
        try:
            descriptor = await run_in_threadpool(file_descriptor, report_id, parsed, file.file)
            approved = await approval(report_id, parsed.version, descriptor)
            result = await run_in_threadpool(service.upload_report, report_id, parsed.version,
                stream=file.file, filename=file.filename or "", revision=parsed.revision,
                metadata=parsed.metadata.model_dump(exclude_none=True), **approved)
        except InputConflictError:
            raise HTTPException(409, "같은 보고서 ID·버전의 파일이 다릅니다. 새 버전을 사용하세요.") from None
        except InputValidationError:
            raise HTTPException(422, "HWP·HWPX·PDF 파일과 크기·메타데이터를 확인하세요.") from None
        finally:
            await file.close()
        return _accepted(result["job"], prefix)

    @router.post("/reports/{report_id}/saved", status_code=202, response_model=JobAccepted)
    def saved(report_id: ReportIdentifier, request: SavedEvent):
        return _accepted(service.report_saved(report_id, request.version, source=request.source, retry=request.retry), prefix)

    @router.post("/reports/{report_id}/ingest", status_code=202, response_model=JobAccepted)
    def canonical(report_id: ReportIdentifier, request: CanonicalEvent):
        return _accepted(service.report_saved(report_id, request.version, retry=request.retry), prefix)

    @router.post("/reports/{report_id}/map-and-ingest", status_code=202, response_model=JobAccepted)
    def mapped(report_id: ReportIdentifier, request: RawQueuedEvent):
        return _accepted(service.queue_report(report_id, request.version, source=request.source, retry=request.retry), prefix)

    @router.get("/reports/{report_id}/status")
    def report_status(report_id: ReportIdentifier, version: int = Query(gt=0)):
        job = service.report_status(report_id, version)
        if job is None:
            raise HTTPException(404, "등록된 보고서 또는 작업을 찾을 수 없습니다.")
        return job

    @router.get("/jobs")
    def jobs(limit: int = Query(default=50, ge=1, le=200)):
        return {"jobs": [service.job_status(job["id"]) for job in service.jobs.list_jobs(limit)]}

    @router.get("/jobs/{job_id}")
    def status(job_id: str):
        job = service.job_status(job_id)
        if job is None:
            raise HTTPException(404, "작업을 찾을 수 없습니다.")
        return job

    @router.get("/jobs/{job_id}/events")
    def events(job_id: str, after: int = Query(default=0, ge=0)):
        if service.jobs.get(job_id) is None:
            raise HTTPException(404, "작업을 찾을 수 없습니다.")
        return {"events": service.jobs.get_events(job_id, after=after)}

    @router.get("/jobs/{job_id}/children")
    def children(job_id: str, limit: int = Query(default=100, ge=1, le=200), offset: int = Query(default=0, ge=0)):
        if service.jobs.get(job_id) is None:
            raise HTTPException(404, "작업을 찾을 수 없습니다.")
        return {"jobs": service.child_jobs(job_id, limit=limit, offset=offset)}

    @router.post("/jobs/{job_id}/retry", status_code=202, response_model=JobAccepted)
    def retry(job_id: str):
        try:
            job = service.retry_job(job_id)
        except KeyError:
            raise HTTPException(404, "작업을 찾을 수 없습니다.") from None
        except ValueError:
            raise HTTPException(409, "보류 또는 실패한 작업만 재시도할 수 있습니다.") from None
        return _accepted(job, prefix)

    return router


def install_wiki_service(app: FastAPI, service: WikiService, *, prefix: str = "/wiki",
                         approval_resolver: Callable | None = None, dependencies: list | None = None) -> None:
    """Install asynchronous routes, bounded uploads and first-setup scheduling."""
    app.add_middleware(WikiBodyLimitMiddleware, max_bytes=service.intake.max_upload_bytes + 64 * 1024, prefix=prefix)
    app.include_router(create_service_router(service, prefix=prefix, approval_resolver=approval_resolver, dependencies=dependencies))
    service.start()
