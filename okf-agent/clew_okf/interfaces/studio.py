"""Read-only workspace for the wiki's draft and approved OKF snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import os
from pathlib import Path
import posixpath
import re
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Query, Response
from fastapi.responses import FileResponse
import yaml

from clew_okf.stages.step_06_publish.bundle import LINK, _parts, _unfenced, inline
from clew_okf.workflows.compiler import _current
from clew_okf.workflows.release import ReleaseNotApprovedError, _output_path, _snapshot, read_approved_release

View = Literal["draft", "approved"]
_PAGE_TYPES = {"index.md": "Index", "log.md": "Changelog"}
_PAGE_FOLDERS = ("equipment", "references", "database")
_PAGE_PATH = re.compile(r"(?:index\.md|log\.md|(?:equipment|references|database)/[a-z0-9][a-z0-9-]*\.md)")
_FOOTNOTE = re.compile(r"^\[\^(src-[a-f0-9]{24})\]: (.+)$", re.MULTILINE)
_FILE_LINK = re.compile(r" · \[원본 열기\]\(file:[^\r\n]+\)$", re.IGNORECASE | re.MULTILINE)
_LOCAL_URI = re.compile(r"\bfile://[^\s)<>]+", re.IGNORECASE)
_ASSETS = Path(__file__).with_name("studio_assets")
_ASSET_TYPES = {
    "studio.css": "text/css",
    "studio.js": "text/javascript",
    "studio_runs.js": "text/javascript",
    "studio_runs.css": "text/css",
    "studio_evidence.css": "text/css",
    "studio_graph.js": "text/javascript",
    "studio_lineage.js": "text/javascript",
    "studio_context.js": "text/javascript",
    "studio_palette.js": "text/javascript",
    "studio_palette.css": "text/css",
}
_NO_STORE = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


@dataclass(frozen=True)
class _Snapshot:
    bundle: Path
    manifest: dict
    approval: dict | None = None


@dataclass(frozen=True)
class _Catalog:
    pages: tuple[dict, ...]
    links: tuple[dict, ...]
    sources: dict[str, tuple[str, str | None]]


def _plain(value: str) -> str:
    return re.sub(r"\\([\\`*_{\[\]<>}|])", r"\1", value)


def _page_document(path: Path, relative: str) -> tuple[dict, str, list]:
    if path.is_symlink() or path.resolve() != path:
        raise FileNotFoundError
    raw = path.read_text(encoding="utf-8")
    fields, body = ({}, raw) if relative == "log.md" else _parts(raw)
    heading = re.search(r"^# (.+)$", body, re.MULTILINE)
    title = fields.get("title") or (_plain(heading[1]) if heading else path.stem)
    metadata = {
        "path": relative,
        "title": title,
        "type": fields.get("type", _PAGE_TYPES.get(relative, "Reference")),
        "status": fields.get("status"),
        "revision": fields.get("revision"),
        "equipment_key": fields.get("equipment_key"),
        "equipment_ids": fields.get("equipment_ids", []),
        "source_key": fields.get("source_key"),
    }
    return metadata, body, fields.get("clew_evidence_groups", [])


def _local_target(path: str, target: str) -> str | None:
    target = urlsplit(target)
    if target.scheme or target.netloc or target.path.startswith("/") or not target.path:
        return None
    relative = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(target.path)))
    return relative if _PAGE_PATH.fullmatch(relative) else None


def _page_files(bundle: Path) -> tuple[tuple[str, int, int, int, int], ...]:
    signatures = []
    paths = [bundle / name for name in _PAGE_TYPES]
    for folder in _PAGE_FOLDERS:
        directory = bundle / folder
        if not directory.is_symlink():
            paths.extend(directory.glob("*.md"))
    for path in sorted(paths):
        relative = path.relative_to(bundle).as_posix()
        if _PAGE_PATH.fullmatch(relative) and not path.is_symlink() and path.is_file():
            stat = path.stat()
            signatures.append((relative, stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size))
    return tuple(signatures)


@lru_cache(maxsize=8)
def _catalog(bundle: Path, files: tuple[tuple[str, int, int, int, int], ...]) -> _Catalog:
    pages = []
    links = []
    sources = {}
    known = {relative for relative, *_ in files}
    for relative, *_ in files:
        page, body, _ = _page_document(bundle / relative, relative)
        pages.append(page)
        visible = _unfenced(body)
        targets = {_local_target(relative, target) for target in LINK.findall(visible)}
        links.extend({"source": relative, "target": target} for target in sorted(known & targets))
        if relative.startswith(("references/", "database/")):
            sources.update({identifier: (relative, page["revision"]) for identifier, _ in _FOOTNOTE.findall(visible)})
    return _Catalog(tuple(pages), tuple(links), sources)


def _evidence(markdown: str, catalog: _Catalog) -> list[dict]:
    evidence = []
    for identifier, footnote in _FOOTNOTE.findall(_unfenced(markdown)):
        label = footnote.split(" · [원본 열기](", 1)[0]
        title_and_revision, _, locator = label.rpartition(" · ")
        path, revision = catalog.sources.get(identifier, (None, None))
        if revision and title_and_revision.endswith(f" · {inline(revision)}"):
            title_and_revision = title_and_revision.removesuffix(f" · {inline(revision)}")
        evidence.append({
            "id": identifier,
            "title": _plain(title_and_revision or label),
            "locator": _plain(locator) if title_and_revision else None,
            "revision": revision,
            "path": path,
        })
    return evidence


def _draft(output: Path) -> _Snapshot | None:
    current = _current(output)
    if current is None:
        return None
    bundle, manifest = _snapshot(output, current.parent.name)
    if bundle != current:
        raise ValueError("현재 초안과 스냅샷 위치가 일치하지 않습니다.")
    return _Snapshot(bundle, manifest)


def _approved(config_path: Path, output: Path) -> _Snapshot | None:
    try:
        approval = read_approved_release(config_path)
    except ReleaseNotApprovedError:
        return None
    bundle, manifest = _snapshot(output, approval["snapshot"])
    return _Snapshot(bundle, manifest, approval)


def _summary(snapshot: _Snapshot | None, catalog: _Catalog | None) -> dict | None:
    if snapshot is None or catalog is None:
        return None
    manifest = snapshot.manifest
    result = {
        "snapshot": manifest["snapshot"],
        "generated_at": manifest.get("generated_at"),
        "counts": {"pages": len(catalog.pages), **{
            key: manifest.get(key, 0) for key in ("equipment", "sources", "selected_quotes", "selected_groups")
        }},
        "links_count": len(catalog.links),
    }
    if snapshot.approval is not None:
        for key in ("approved_by", "approved_at"):
            result[key] = snapshot.approval[key]
    return result


def _get_catalog(snapshot: _Snapshot | None) -> _Catalog | None:
    if snapshot is None:
        return None
    return _catalog(snapshot.bundle, _page_files(snapshot.bundle))


def _load_asset(name: str, content_type: str) -> FileResponse:
    asset = _ASSETS / name
    if not asset.is_file():
        raise HTTPException(404, "위키 스튜디오 파일을 찾을 수 없습니다.", headers=_NO_STORE)
    return FileResponse(asset, media_type=content_type, headers=_NO_STORE)


@dataclass(frozen=True)
class StudioEvaluation:
    """Read-only supplemental records for a known draft from a separate evaluation."""

    snapshot: str
    groups: dict[str, list[dict]]
    records: tuple[dict, ...]


def _claim_groups(raw: list, evidence: list[dict]) -> list[dict]:
    known = {item["id"]: item for item in evidence}
    return [
        {
            **group,
            "relationship": "selected",
            "evidence": [
                {**known[span["evidence_id"]], **span}
                for span in group["evidence"] if span["evidence_id"] in known
            ],
        }
        for group in raw
    ]


def _build_details(snapshot: _Snapshot, equipment_key: str | None, records: tuple[dict, ...]) -> dict:
    manifest = snapshot.manifest
    review = next(
        (entry for entry in manifest.get("wiki_reviews", []) if entry["equipment_key"] == equipment_key),
        None,
    )
    return {
        "snapshot": manifest["snapshot"],
        "generated_at": manifest.get("generated_at"),
        "review": review,
        "executions": [entry for entry in records if entry.get("equipment_key") in (None, equipment_key)],
        "approval": {
            key: snapshot.approval[key] for key in ("approved_by", "approved_at")
        } if snapshot.approval else None,
    }


def create_studio_router(
    config_path: Path, *, jobs_available: bool, evaluation: StudioEvaluation | None = None,
) -> APIRouter:
    """Reuse the configured wiki router's dependencies and output directory."""
    router = APIRouter(prefix="/studio", tags=["wiki-studio"])

    @router.get("", include_in_schema=False)
    def studio():
        return _load_asset("index.html", "text/html")

    @router.get("/assets/{name}", include_in_schema=False)
    def studio_asset(name: str):
        content_type = _ASSET_TYPES.get(name)
        if content_type is None:
            raise HTTPException(404, "위키 스튜디오 파일을 찾을 수 없습니다.", headers=_NO_STORE)
        return _load_asset(name, content_type)

    @router.get("/overview")
    def overview(response: Response, view: View = "draft") -> dict:
        try:
            output = _output_path(config_path)
            draft, approved = _draft(output), _approved(config_path, output)
            draft_catalog, approved_catalog = _get_catalog(draft), _get_catalog(approved)
            selected = draft if view == "draft" else approved
            catalog = draft_catalog if view == "draft" else approved_catalog
        except (OSError, ValueError, yaml.YAMLError):
            raise HTTPException(503, "위키 스냅샷 정보를 불러오지 못했습니다.", headers=_NO_STORE) from None
        context = os.environ.get("CLEW_WIKI_STUDIO_CONTEXT", "configured")
        response.headers.update(_NO_STORE)
        return {
            "view": view,
            "context": context if context in {"evaluation", "production"} else "configured",
            "draft": _summary(draft, draft_catalog),
            "approved": _summary(approved, approved_catalog),
            "pages": catalog.pages if catalog else (),
            "links": catalog.links if catalog else (),
            "reviews": selected.manifest.get("wiki_reviews", []) if selected else [],
            "jobs_available": jobs_available,
            "work_records": evaluation.records if evaluation and view == "draft" and
            selected and selected.manifest["snapshot"] == evaluation.snapshot else (),
            "evaluation_lookup": evaluation is not None,
        }

    @router.get("/page")
    def page(
        response: Response, path: Annotated[str, Query(min_length=1, max_length=200)],
        view: View = "draft", snapshot: Annotated[str | None, Query(max_length=110)] = None,
    ) -> dict:
        if not _PAGE_PATH.fullmatch(path):
            raise HTTPException(404, "위키 문서를 찾을 수 없습니다.", headers=_NO_STORE)
        try:
            output = _output_path(config_path)
            selected = _draft(output) if view == "draft" else _approved(config_path, output)
            if selected is None:
                raise HTTPException(404, "열 수 있는 위키 스냅샷이 없습니다.", headers=_NO_STORE)
            snapshot_id = selected.manifest["snapshot"]
            if snapshot is not None and snapshot != snapshot_id:
                raise HTTPException(409, "위키가 변경되었습니다. 목록을 새로 불러오세요.", headers=_NO_STORE)
            catalog = _get_catalog(selected)
            if catalog is None or not any(meta["path"] == path for meta in catalog.pages):
                raise HTTPException(404, "위키 문서를 찾을 수 없습니다.", headers=_NO_STORE)
            metadata, body, raw_groups = _page_document(selected.bundle / path, path)
            markdown = _LOCAL_URI.sub("원본 보관 위치", _FILE_LINK.sub("", body)).lstrip("\n")
        except FileNotFoundError:
            raise HTTPException(404, "위키 문서를 찾을 수 없습니다.", headers=_NO_STORE) from None
        except (OSError, ValueError, yaml.YAMLError):
            raise HTTPException(503, "위키 문서를 불러오지 못했습니다.", headers=_NO_STORE) from None
        evidence = _evidence(markdown, catalog)
        groups = _claim_groups(raw_groups, evidence)
        records = ()
        if evaluation and view == "draft" and snapshot_id == evaluation.snapshot:
            groups.extend(evaluation.groups.get(metadata["equipment_key"], ()))
            records = evaluation.records
        response.headers.update(_NO_STORE)
        return {
            **metadata,
            "snapshot": snapshot_id,
            "markdown": markdown,
            "links": [link["target"] for link in catalog.links if link["source"] == path],
            "backlinks": [link["source"] for link in catalog.links if link["target"] == path],
            "evidence": evidence,
            "structured_evidence": {"groups": groups},
            "build": _build_details(selected, metadata["equipment_key"], records),
        }

    return router
