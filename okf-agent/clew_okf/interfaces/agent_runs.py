"""Internal ingestion and operator-only reads of CLEW execution traces."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import os
from pathlib import Path
from secrets import compare_digest
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import (
    AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints, ValidationError,
    field_serializer, model_validator,
)
from starlette.concurrency import run_in_threadpool

from clew_okf.runtime.agent_runs import AgentRunConflict, AgentRunStore


_MAX_BYTES = 128 * 1024
_PRIVATE = {"Cache-Control": "no-store", "Vary": "Authorization"}
_RunId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")]
_Message = Annotated[str, StringConstraints(max_length=6000)]
_Label = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class _ChatRequest(_StrictModel):
    message: _Message | None = None


class _ChatResponse(_StrictModel):
    message: _Message | None = None
    type: _Label | None = None
    error: _Label | None = None


class AgentRunStep(_StrictModel):
    kind: Literal["chat", "code", "rule", "agent", "tool", "wiki", "llm"]
    name: _Label
    source: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    at: AwareDatetime
    status: Literal["completed", "skipped", "failed"]
    details: dict[str, JsonValue]
    duration_ms: Annotated[int, Field(ge=0, le=86_400_000)] | None = None

    @field_serializer("at", when_used="json")
    def serialize_at(self, value: datetime) -> str:
        return _timestamp(value)


class AgentRunSnapshot(_StrictModel):
    version: Literal[1]
    origin: Literal["clew_chat", "wiki_evaluation"] = "clew_chat"
    request_id: _RunId
    session_id: _RunId
    started_at: AwareDatetime
    finished_at: AwareDatetime | None
    status: Literal["running", "completed", "failed"]
    request: _ChatRequest
    response: _ChatResponse | None
    steps: Annotated[list[AgentRunStep], Field(max_length=64)]

    @model_validator(mode="after")
    def consistent_timeline(self):
        is_terminal = self.status != "running"
        if (self.finished_at is not None) != is_terminal or (self.response is not None) != is_terminal:
            raise ValueError("Terminal status requires finish time and response")
        previous = self.started_at
        for step in self.steps:
            if step.at < previous:
                raise ValueError("Steps must be chronological")
            previous = step.at
        if self.finished_at is not None and self.finished_at < previous:
            raise ValueError("Finish time precedes an observed step")
        return self

    @field_serializer("started_at", "finished_at", when_used="json")
    def serialize_time(self, value: datetime | None) -> str | None:
        return _timestamp(value) if value is not None else None


def _authorize(env_name: str, credential: HTTPAuthorizationCredentials | None) -> None:
    expected = os.environ.get(env_name)
    if not expected:
        raise HTTPException(503, "에이전트 실행 추적이 설정되지 않았습니다.", headers=_PRIVATE)
    if credential is None or not compare_digest(credential.credentials.encode(), expected.encode()):
        raise HTTPException(
            401, "에이전트 실행 추적 토큰을 확인하세요.",
            headers={**_PRIVATE, "WWW-Authenticate": "Bearer"},
        )


_ingest_bearer = HTTPBearer(auto_error=False, scheme_name="AgentRunIngestBearer")
_admin_bearer = HTTPBearer(auto_error=False, scheme_name="AgentRunAdminBearer")


def _require_ingest(credential: Annotated[HTTPAuthorizationCredentials | None, Depends(_ingest_bearer)]) -> None:
    _authorize("CLEW_MONITOR_INGEST_TOKEN", credential)


def require_agent_run_admin(credential: Annotated[HTTPAuthorizationCredentials | None, Depends(_admin_bearer)]) -> None:
    _authorize("CLEW_MONITOR_ADMIN_TOKEN", credential)


async def _read_snapshot(request: Request) -> AgentRunSnapshot:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BYTES:
            raise HTTPException(413, "실행 기록이 요청 크기 한도를 넘었습니다.", headers=_PRIVATE)
        body.extend(chunk)
    try:
        run = AgentRunSnapshot.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, "실행 기록 형식이 올바르지 않습니다.", headers=_PRIVATE) from None
    if run.origin != "clew_chat":
        raise HTTPException(422, "수집 API는 서비스 질의 기록을 받습니다.", headers=_PRIVATE)
    return run


def create_agent_runs_router(
    state_dir: Path, *, read_dependency: Callable[..., None] = require_agent_run_admin,
) -> APIRouter:
    store = AgentRunStore(state_dir)
    router = APIRouter(prefix="/agent-runs", tags=["agent-runs"])

    @router.post("", status_code=204, dependencies=[Depends(_require_ingest)])
    async def ingest(request: Request) -> Response:
        run = await _read_snapshot(request)
        try:
            await run_in_threadpool(store.save, run.model_dump(mode="json", exclude_unset=True))
        except AgentRunConflict:
            raise HTTPException(409, "이미 기록된 실행과 일치하지 않습니다.", headers=_PRIVATE) from None
        except (ValueError, OverflowError, RecursionError):
            raise HTTPException(422, "실행 기록 형식이 올바르지 않습니다.", headers=_PRIVATE) from None
        return Response(status_code=204, headers=_PRIVATE)

    @router.get("", dependencies=[Depends(read_dependency)])
    def recent(limit: Annotated[int, Query(ge=1, le=200)] = 50, session_id: _RunId | None = None):
        return JSONResponse({"runs": store.recent(limit, session_id)}, headers=_PRIVATE)

    @router.get("/{request_id}", dependencies=[Depends(read_dependency)])
    def detail(request_id: _RunId):
        run = store.get(request_id)
        if run is None:
            raise HTTPException(404, "실행 기록을 찾을 수 없습니다.", headers=_PRIVATE)
        return JSONResponse(run, headers=_PRIVATE)

    return router
