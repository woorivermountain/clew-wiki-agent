"""Inspect a scoped OKF entry and record the lookup decisions actually taken."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import hmac
import os
from pathlib import Path
from time import perf_counter_ns
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints
import yaml

from clew_okf.interfaces.agent_runs import AgentRunSnapshot, AgentRunStep, require_agent_run_admin
from clew_okf.interfaces.studio import _FOOTNOTE, _NO_STORE, _draft, _local_target, _output_path, _page_document
from clew_okf.runtime.agent_runs import AgentRunStore
from clew_okf.stages.step_06_publish.bundle import LINK, _unfenced, file_key
from clew_okf.workflows.release import ReleaseNotApprovedError, read_approved_release


_bearer = HTTPBearer(auto_error=False)
_Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class LookupTarget(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    plant_id: _Identifier
    equipment_id: _Identifier


class LookupRequest(LookupTarget):
    include_trace: bool = False


class WikiDocument(BaseModel):
    path: str
    title: str
    revision: str | None = None


class LookupTrace(BaseModel):
    steps: list[AgentRunStep] = Field(default_factory=list)

    def record(
        self, kind: Literal["rule", "wiki"], name: str, source: str,
        started_ns: int, details: dict[str, JsonValue], *,
        status: Literal["completed", "failed"] = "completed",
    ) -> None:
        self.steps.append(AgentRunStep(
            kind=kind, name=name, source=source, at=datetime.now(UTC), status=status,
            duration_ms=(perf_counter_ns() - started_ns) // 1_000_000, details=details,
        ))


class LookupResult(BaseModel):
    result: Literal["hit", "miss", "unapproved"]
    snapshot: str | None = None
    page: WikiDocument | None = None
    sources: list[WikiDocument] = Field(default_factory=list)
    source_count: int = 0
    citation_count: int = 0
    trace: LookupTrace | None = None


class EvaluationLookupResult(LookupResult):
    request_id: str


def _require_lookup_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> None:
    expected = os.environ.get("CLEW_WIKI_LOOKUP_TOKEN")
    if not expected:
        raise HTTPException(503, "채팅 위키 조회가 설정되지 않았습니다.", headers=_NO_STORE)
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        raise HTTPException(401, "채팅 위키 조회 인증이 필요합니다.",
                            headers={**_NO_STORE, "WWW-Authenticate": "Bearer"})


def _trace_document(document: WikiDocument) -> dict[str, JsonValue]:
    data: dict[str, JsonValue] = {"path": document.path, "title": document.title[:200]}
    if document.revision:
        data["revision"] = document.revision[:200]
    return data


def lookup_wiki_bundle(
    bundle: Path, snapshot: str, plant_id: str, equipment_id: str, trace: LookupTrace | None = None,
) -> LookupResult:
    """Walk the equipment links in an explicitly selected wiki bundle."""
    started = perf_counter_ns()
    key = f"{plant_id}/{equipment_id}"
    path = f"equipment/{file_key(key)}.md"
    scope_result = "matched"
    try:
        metadata, body, _ = _page_document(bundle / path, path)
    except FileNotFoundError:
        scope_result = "page_missing"
    else:
        if metadata["type"] != "Equipment" or metadata["equipment_key"] != key:
            scope_result = "scope_mismatch"
    if trace is not None:
        trace.record("rule", "wiki.equipment.scope", "clew_okf/interfaces/chat_lookup.py::lookup_wiki_bundle",
                     started, {
                         "rule_id": "equipment_exact_in_plant",
                         "decision": "passed" if scope_result == "matched" else "blocked",
                         "result": scope_result, "snapshot": snapshot,
                         "plant_id": plant_id, "equipment_id": equipment_id,
                     })
    if scope_result != "matched":
        return LookupResult(result="miss", snapshot=snapshot, trace=trace)

    started = perf_counter_ns()
    visible = _unfenced(body)
    targets = dict.fromkeys(_local_target(path, link) for link in LINK.findall(visible))
    sources = []
    excluded_sources = 0
    for target in targets:
        if target is None or not target.startswith(("references/", "database/")):
            continue
        detail, _, _ = _page_document(bundle / target, target)
        if key in detail["equipment_ids"]:
            sources.append(WikiDocument(path=target, title=detail["title"], revision=detail["revision"]))
        else:
            excluded_sources += 1
    page = WikiDocument(path=path, title=metadata["title"])
    citation_count = len(_FOOTNOTE.findall(visible))
    if trace is not None:
        trace.record("wiki", "wiki.sources.linked", "clew_okf/interfaces/chat_lookup.py::lookup_wiki_bundle",
                     started, {
                         "rule_id": "source_scoped_to_equipment",
                         "operation": "collect_linked_sources",
                         "result": "hit", "snapshot": snapshot, "page": _trace_document(page),
                         "sources": [_trace_document(source) for source in sources[:8]],
                         "source_count": len(sources), "citation_count": citation_count,
                         "excluded_sources": excluded_sources,
                     })
    return LookupResult(
        result="hit", snapshot=snapshot, page=page, sources=sources[:8],
        source_count=len(sources), citation_count=citation_count, trace=trace,
    )


def lookup_approved_equipment(
    config_path: Path, plant_id: str, equipment_id: str, trace: LookupTrace | None = None,
) -> LookupResult:
    started = perf_counter_ns()
    try:
        release = read_approved_release(config_path)
    except ReleaseNotApprovedError:
        if trace is not None:
            trace.record("rule", "wiki.release.approval", "clew_okf/workflows/release.py::read_approved_release",
                         started, {
                             "rule_id": "approved_release_required",
                             "decision": "blocked", "result": "unapproved",
                         })
        return LookupResult(result="unapproved", trace=trace)

    if trace is not None:
        trace.record("rule", "wiki.release.approval", "clew_okf/workflows/release.py::read_approved_release",
                     started, {
                         "rule_id": "approved_release_required",
                         "decision": "passed", "snapshot": release["snapshot"], "view": "approved",
                     })
    return lookup_wiki_bundle(Path(release["bundle"]), release["snapshot"], plant_id, equipment_id, trace)


def create_chat_lookup_router(config_path: Path) -> APIRouter:
    router = APIRouter(prefix="/chat", tags=["wiki-chat"], dependencies=[Depends(_require_lookup_token)])

    @router.post("/lookup", response_model=LookupResult)
    def lookup(payload: LookupRequest) -> JSONResponse:
        trace = LookupTrace() if payload.include_trace else None
        try:
            result = lookup_approved_equipment(config_path, payload.plant_id, payload.equipment_id, trace)
        except (OSError, ValueError, yaml.YAMLError):
            raise HTTPException(503, "승인 위키를 열 수 없습니다.", headers=_NO_STORE) from None
        return JSONResponse(result.model_dump(mode="json", exclude=set() if trace else {"trace"}), headers=_NO_STORE)

    return router


def local_evaluation_access(port: int) -> Callable[[Request], None]:
    """Authorize a separate loopback preview without a production operator token."""
    authority = f"127.0.0.1:{port}"
    origin = f"http://{authority}"

    def authorize(request: Request) -> None:
        peer = request.client
        if (peer is None or peer.host not in {"127.0.0.1", "::1"}
                or request.headers.get("host") != authority
                or request.headers.get("origin") not in {None, origin}):
            raise HTTPException(403, "이 평가는 로컬 작업실에서만 볼 수 있습니다.", headers=_NO_STORE)
        content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
        if request.method == "POST" and content_type != "application/json":
            raise HTTPException(415, "평가 요청에는 JSON이 필요합니다.", headers=_NO_STORE)

    return authorize


def create_evaluation_lookup_router(
    config_path: Path, state_dir: Path, *, authorize: Callable[..., None] = require_agent_run_admin,
) -> APIRouter:
    """Mount on the loopback evaluation server to inspect its draft, in isolation."""
    store = AgentRunStore(state_dir)
    router = APIRouter(prefix="/studio", tags=["wiki-evaluation"], dependencies=[Depends(authorize)])

    @router.post("/evaluation-lookup", response_model=EvaluationLookupResult)
    def lookup(payload: LookupTarget, response: Response) -> EvaluationLookupResult:
        started_at = datetime.now(UTC)
        started = perf_counter_ns()
        request_id = f"wiki-eval-{uuid4().hex}"
        trace = LookupTrace()
        scope = {"plant_id": payload.plant_id, "equipment_id": payload.equipment_id, "view": "draft"}
        try:
            selected = _draft(_output_path(config_path))
            if selected is None:
                raise FileNotFoundError
            snapshot = selected.manifest["snapshot"]
            trace.record("wiki", "wiki.evaluation.draft", "clew_okf/interfaces/studio.py::_draft", started,
                         {**scope, "snapshot": snapshot, "operation": "open_evaluation_draft"})
            result = lookup_wiki_bundle(selected.bundle, snapshot, payload.plant_id, payload.equipment_id, trace)
        except (OSError, ValueError, yaml.YAMLError):
            trace.record("wiki", "wiki.evaluation.unavailable", "clew_okf/interfaces/chat_lookup.py::lookup_wiki_bundle",
                         started, {**scope, "result": "unavailable"}, status="failed")
            store.save(AgentRunSnapshot(
                version=1, origin="wiki_evaluation", request_id=request_id, session_id="wiki-studio-evaluation",
                started_at=started_at, finished_at=datetime.now(UTC), status="failed", request={},
                response={"type": "wiki_lookup", "error": "unavailable"}, steps=trace.steps,
            ).model_dump(mode="json", exclude_unset=True))
            raise HTTPException(503, "평가 위키를 열 수 없습니다.", headers=_NO_STORE) from None

        store.save(AgentRunSnapshot(
            version=1, origin="wiki_evaluation", request_id=request_id, session_id="wiki-studio-evaluation",
            started_at=started_at, finished_at=datetime.now(UTC), status="completed", request={},
            response={"type": result.result}, steps=trace.steps,
        ).model_dump(mode="json", exclude_unset=True))
        response.headers.update(_NO_STORE)
        return EvaluationLookupResult(request_id=request_id, **result.model_dump())

    return router
