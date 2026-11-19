"""Optional FastAPI adapter for a configured WikiAgent."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel, Field

from clew_okf import WikiAgent
from clew_okf.workflows.compiler import BuildInProgressError
from clew_okf.domain.reports import ReportIdentifier, ReportRequest


class WikiBuildResponse(BaseModel):
    snapshot: str
    generated_at: str
    equipment: int
    sources: int
    unmapped_documents: int
    selected_quotes: int
    warnings: list[str]
    wiki_reviews: list[dict[str, Any]] = Field(default_factory=list)
    state: Literal["published", "unchanged", "reused"]
    bundle: str


class RawReportRequest(ReportRequest):
    source: Literal["file", "database"]


class ReportAdmissionResponse(BaseModel):
    report_id: str
    version: int
    status: Literal["published", "unchanged", "duplicate", "hold", "conflict", "rejected"]
    action: str
    findings: list[dict[str, Any]]
    review: dict[str, Any] | None
    build: WikiBuildResponse | None
    wiki_path: str | None
    mapping: dict[str, Any] | None = None


def create_router(agent: WikiAgent, *, prefix: str = "/wiki") -> APIRouter:
    """Use a server-configured agent and wait for its build to complete."""
    router = APIRouter(prefix=prefix, tags=["wiki"])

    @router.post("/build", response_model=WikiBuildResponse)
    def build_wiki() -> WikiBuildResponse:
        try:
            report = agent.run()
        except BuildInProgressError:
            raise HTTPException(409, "같은 출력 경로의 위키 빌드가 진행 중입니다.") from None
        except (ValueError, yaml.YAMLError, csv.Error):
            raise HTTPException(422, "위키 입력 자료 또는 설정을 확인하세요.") from None
        except RuntimeError:
            raise HTTPException(502, "위키 빌드의 데이터 또는 모델 처리에 실패했습니다.") from None
        except OSError:
            raise HTTPException(503, "위키 빌드에 필요한 파일 또는 저장소에 접근하지 못했습니다.") from None
        return WikiBuildResponse(**report)

    @router.post("/reports/{report_id}/ingest", response_model=ReportAdmissionResponse)
    def ingest_report(report_id: ReportIdentifier, request: ReportRequest) -> ReportAdmissionResponse:
        try:
            result = agent.ingest_report(report_id, request.version)
        except BuildInProgressError:
            raise HTTPException(409, "같은 출력 경로의 위키 빌드가 진행 중입니다.") from None
        except (ValueError, yaml.YAMLError, csv.Error):
            raise HTTPException(422, "보고서 편입 입력 또는 서버 설정을 확인하세요.") from None
        except RuntimeError:
            raise HTTPException(502, "보고서 원본 조회 또는 위키 생성에 실패했습니다.") from None
        except OSError:
            raise HTTPException(503, "보고서 위키 저장소에 접근하지 못했습니다.") from None
        return ReportAdmissionResponse(**result)

    @router.post("/reports/{report_id}/map-and-ingest", response_model=ReportAdmissionResponse)
    def map_and_ingest_report(report_id: ReportIdentifier, request: RawReportRequest) -> ReportAdmissionResponse:
        try:
            result = agent.map_and_ingest_report(report_id, request.version, request.source)
        except BuildInProgressError:
            raise HTTPException(409, "같은 출력 경로의 위키 빌드가 진행 중입니다.") from None
        except (ValueError, yaml.YAMLError, csv.Error):
            raise HTTPException(422, "보고서 매핑 입력 또는 서버 설정을 확인하세요.") from None
        except RuntimeError:
            raise HTTPException(502, "보고서 원본 확인 또는 위키 생성에 실패했습니다.") from None
        except OSError:
            raise HTTPException(503, "보고서 원본 또는 위키 저장소에 접근하지 못했습니다.") from None
        return ReportAdmissionResponse(**result)

    return router


def create_app() -> FastAPI:
    """Configure the app factory using the server's CLEW_WIKI_CONFIG path."""
    config = os.environ.get("CLEW_WIKI_CONFIG")
    if not config or not config.strip():
        raise ValueError("CLEW_WIKI_CONFIG에 서버의 위키 설정 파일 경로를 지정하세요.")
    app = FastAPI(title="CLEW Wiki Builder")
    settings = yaml.safe_load(Path(config).read_text(encoding="utf-8"))
    if isinstance(settings, dict) and "service" in settings:
        from clew_okf.runtime.service import WikiService
        from clew_okf.interfaces.async_api import install_wiki_service
        service = WikiService(config)
        app.state.wiki_service = service
        install_wiki_service(app, service)
    else:
        app.include_router(create_router(WikiAgent(Path(config))))
    return app
