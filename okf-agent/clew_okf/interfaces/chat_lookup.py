"""Read a scoped equipment entry from the approved OKF release for CLEW chat."""

from __future__ import annotations

import hmac
import os
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
import yaml

from clew_okf.interfaces.studio import _FOOTNOTE, _NO_STORE, _local_target, _page_document
from clew_okf.stages.step_06_publish.bundle import LINK, _unfenced, file_key
from clew_okf.workflows.release import ReleaseNotApprovedError, read_approved_release


_bearer = HTTPBearer(auto_error=False)
_Identifier = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class LookupRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    plant_id: _Identifier
    equipment_id: _Identifier


class WikiDocument(BaseModel):
    path: str
    title: str
    revision: str | None = None


class LookupResult(BaseModel):
    result: Literal["hit", "miss", "unapproved"]
    snapshot: str | None = None
    page: WikiDocument | None = None
    sources: list[WikiDocument] = Field(default_factory=list)
    source_count: int = 0
    citation_count: int = 0


def _require_lookup_token(credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)]) -> None:
    expected = os.environ.get("CLEW_WIKI_LOOKUP_TOKEN")
    if not expected:
        raise HTTPException(503, "채팅 위키 조회가 설정되지 않았습니다.", headers=_NO_STORE)
    if credentials is None or not hmac.compare_digest(credentials.credentials.encode(), expected.encode()):
        raise HTTPException(401, "채팅 위키 조회 인증이 필요합니다.",
                            headers={**_NO_STORE, "WWW-Authenticate": "Bearer"})


def lookup_approved_equipment(config_path: Path, plant_id: str, equipment_id: str) -> LookupResult:
    try:
        release = read_approved_release(config_path)
    except ReleaseNotApprovedError:
        return LookupResult(result="unapproved")

    snapshot = release["snapshot"]
    bundle = Path(release["bundle"])
    key = f"{plant_id}/{equipment_id}"
    path = f"equipment/{file_key(key)}.md"
    try:
        metadata, body = _page_document(bundle / path, path)
    except FileNotFoundError:
        return LookupResult(result="miss", snapshot=snapshot)
    if metadata["type"] != "Equipment" or metadata["equipment_key"] != key:
        return LookupResult(result="miss", snapshot=snapshot)

    visible = _unfenced(body)
    targets = dict.fromkeys(_local_target(path, link) for link in LINK.findall(visible))
    sources = []
    for target in targets:
        if target is None or not target.startswith(("references/", "database/")):
            continue
        detail, _ = _page_document(bundle / target, target)
        if key in detail["equipment_ids"]:
            sources.append(WikiDocument(path=target, title=detail["title"], revision=detail["revision"]))
    return LookupResult(
        result="hit", snapshot=snapshot, page=WikiDocument(path=path, title=metadata["title"]),
        sources=sources[:8], source_count=len(sources), citation_count=len(_FOOTNOTE.findall(visible)),
    )


def create_chat_lookup_router(config_path: Path) -> APIRouter:
    router = APIRouter(prefix="/chat", tags=["wiki-chat"], dependencies=[Depends(_require_lookup_token)])

    @router.post("/lookup", response_model=LookupResult)
    def lookup(payload: LookupRequest, response: Response) -> LookupResult:
        try:
            result = lookup_approved_equipment(config_path, payload.plant_id, payload.equipment_id)
        except (OSError, ValueError, yaml.YAMLError):
            raise HTTPException(503, "승인 위키를 열 수 없습니다.", headers=_NO_STORE) from None
        response.headers.update(_NO_STORE)
        return result

    return router
