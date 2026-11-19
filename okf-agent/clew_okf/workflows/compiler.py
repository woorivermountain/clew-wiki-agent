"""Read configured sources and atomically publish a verified OKF wiki."""

import csv
import errno
import fcntl
import json
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import uuid4

import yaml

from clew_okf.stages.step_06_publish.bundle import digest, validate_bundle, write_bundle
from clew_okf.stages.step_01_ingest.documents import load_documents
from clew_okf.domain.model import Equipment, Source
from clew_okf.stages.step_01_ingest.postgres import load_postgres, postgres_resource
from clew_okf.stages.step_04_compose.synthesis import configure_synthesis
from clew_okf.providers.llm import JsonModel
from clew_okf.domain.report_index import active_entries, read_index, report_sources
from clew_okf.domain.reports import ReportRecord, validate_report_links
from clew_okf.stages.step_01_ingest import report_store
from typing import Callable
from clew_okf.runtime.progress import emit
from clew_okf.domain.errors import RevalidationRequiredError


class BuildInProgressError(RuntimeError):
    """The configured output is already being built by another invocation."""


CONFIG_KEYS = {"title", "output", "equipment_csv", "documents", "postgres", "database_links", "synthesis", "admission", "service", "bootstrap"}
EQUIPMENT_FIELDS = ("equipment_id", "name", "plant_id", "process_id", "model", "aliases")


def _object(value: object, name: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{name}은 YAML 객체여야 합니다.")
    return value


def _path(value: object, base: Path, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}에 경로를 지정하세요.")
    return (base / value).resolve()


def _record(fields: dict, resource: str, locator: str) -> Equipment:
    values = {}
    for key in EQUIPMENT_FIELDS:
        value = fields.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError(f"설비 마스터 필드 {key!r}는 한 줄의 문자열이어야 합니다: {locator}")
        if value := value.strip():
            values[key] = value
    equipment_id = values.get("equipment_id")
    if not equipment_id:
        raise ValueError(f"설비 마스터에 equipment_id가 필요합니다: {locator}")
    if "/" in equipment_id or "/" in values.get("plant_id", ""):
        raise ValueError(f"plant_id와 equipment_id에는 슬래시를 쓸 수 없습니다: {locator}")
    return Equipment(equipment_id, values, resource, locator)


def _equipment_csv(path: Path) -> list[Equipment]:
    if not path.is_file():
        raise ValueError(f"설비 마스터 CSV를 찾을 수 없습니다: {path}")
    records = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        columns = reader.fieldnames or []
        if len(columns) != len(set(columns)) or not {"equipment_id", "name"} <= set(columns):
            raise ValueError("CSV에는 고유한 헤더와 equipment_id, name 컬럼이 필요합니다.")
        for row in reader:
            if None in row:
                raise ValueError(f"CSV 열 수가 헤더와 일치하지 않습니다: {reader.line_num}행")
            if any(value for value in row.values()):
                locator = f"CSV {reader.line_num}행"
                if not (row.get("name") or "").strip():
                    raise ValueError(f"설비 마스터 CSV에 name이 필요합니다: {locator}")
                records.append(_record(row, path.as_uri(), locator))
    if not records:
        raise ValueError("설비 마스터 CSV에 데이터가 없습니다.")
    return records


def _load_sources(config: dict, base: Path, output: Path) -> tuple[list[Source], list[Equipment], list[str]]:
    emit("loading_sources", "설비 마스터와 설정된 원본을 읽고 있습니다.")
    sources: list[Source] = []
    equipment: list[Equipment] = []
    warnings: list[str] = []
    if "equipment_csv" in config:
        path = _path(config["equipment_csv"], base, "equipment_csv")
        if path.is_relative_to(output):
            raise ValueError("설비 마스터 CSV는 출력 경로 밖에 둬야 합니다.")
        equipment.extend(_equipment_csv(path))
    if "postgres" in config:
        database = _object(config["postgres"], "postgres")
        db_sources, rows = load_postgres(database)
        registry = database.get("equipment")
        if rows and registry:
            schema, table = registry["table"].split(".")
            resource = postgres_resource(database.get("name", "postgres"), registry["table"])
            for fields in rows:
                locator = f"{schema}.{table} equipment_id={fields.get('equipment_id', '')}"
                if fields.get("plant_id"):
                    locator += f", plant_id={fields['plant_id']}"
                equipment.append(_record(fields, resource, locator))
        sources.extend(db_sources)
    if "database_links" in config:
        links = _object(config["database_links"], "database_links")
        known = {source.key for source in sources if source.kind == "postgres"}
        if set(links) - known:
            raise ValueError("database_links에는 수집 대상인 postgres:… 원본 키를 지정해야 합니다.")
        for index, source in enumerate(sources):
            if source.key not in links:
                continue
            keys = links[source.key]
            if not isinstance(keys, list) or not keys or any(
                not isinstance(key, str) or not key.strip() for key in keys
            ):
                raise ValueError(f"database_links의 설비 키 목록이 유효하지 않습니다: {source.key}")
            sources[index] = replace(source, equipment_ids=tuple(dict.fromkeys(keys)))
    if "documents" in config:
        settings = _object(config["documents"], "documents")
        scope_from_path = settings.get("scope_from_path", False)
        if type(scope_from_path) is not bool:
            raise ValueError("documents.scope_from_path는 true 또는 false여야 합니다.")
        documents, notices = load_documents(settings, base, output=output, cache=output / "extraction-cache")
        if scope_from_path:
            scope_keys = {item.key for item in equipment}
            file_options = settings.get("files", {})
            for index, source in enumerate(documents):
                relative = source.key.removeprefix("docs:")
                if "equipment_ids" in file_options.get(relative, {}):
                    continue
                folders = PurePosixPath(relative).parts[:-1]
                candidates = ("/".join(folders[:size]) for size in range(min(2, len(folders)), 0, -1))
                scope = next((key for key in candidates if key in scope_keys), None)
                if scope:
                    documents[index] = replace(source, equipment_ids=(scope,))
        sources.extend(documents)
        warnings.extend(notices)
    if not sources and not equipment:
        raise ValueError("수집된 문서, DB 테이블 또는 설비가 없습니다.")
    if len({source.key for source in sources}) != len(sources):
        raise ValueError("수집 원본의 키가 중복되었습니다.")
    if len({item.key for item in equipment}) != len(equipment):
        raise ValueError("설비 키가 중복되었습니다. plant_id를 채우거나 중복 행을 정리하세요.")
    if any(not source.blocks for source in sources):
        raise ValueError("일부 원본의 추출 근거가 비어 있습니다.")
    warnings.extend(f"{source.key}: {note}" for source in sources for note in source.warnings)
    return sorted(sources, key=lambda source: source.key), sorted(equipment, key=lambda item: item.key), warnings


def _current(output: Path) -> Path | None:
    current = output / "current"
    if current.is_symlink():
        bundle = current.resolve()
        if (
            not bundle.is_relative_to(output / "snapshots")
            or bundle.name != "bundle"
            or not (bundle.parent / "manifest.json").is_file()
        ):
            raise ValueError("current가 관리되는 스냅샷을 가리키지 않습니다.")
        return bundle
    if current.exists():
        raise ValueError("current 경로가 이미 사용 중입니다. 전용 출력 경로를 설정하세요.")
    return None


def _publish(output: Path, bundle: Path) -> None:
    emit("publishing_wiki", "검증된 위키 스냅샷을 발행합니다.")
    temporary = output / f".current-{uuid4().hex}"
    try:
        temporary.symlink_to(bundle.relative_to(output), target_is_directory=True)
        os.replace(temporary, output / "current")
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class PreparedBuild:
    config: dict
    output: Path
    previous: Path | None
    sources: list[Source]
    equipment: list[Equipment]
    warnings: list[str]
    synthesis: dict | None
    provider: JsonModel | None
    base: Path


@contextmanager
def prepare_build(config_path: str | Path):
    """Hold the output lock while checking, staging and publishing one build."""
    config_path = Path(config_path).resolve()
    config = _object(yaml.safe_load(config_path.read_text(encoding="utf-8")), "설정")
    if config.keys() - CONFIG_KEYS:
        raise ValueError("알 수 없는 최상위 설정: " + ", ".join(sorted(map(str, config.keys() - CONFIG_KEYS))))
    title = config.get("title", "CLEW 제조 위키")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title은 비어 있지 않은 문자열이어야 합니다.")
    base = config_path.parent
    output = _path(config.get("output", "output"), base, "output")
    if config_path.is_relative_to(output):
        raise ValueError("설정 파일은 출력 경로 밖에 둬야 합니다.")
    admission = config.get("admission")
    if admission is not None:
        admission = _object(admission, "admission")
        settings = _object(admission.get("reports", {}), "admission.reports")
        record_tables = {entry.get("table") for entry in config.get("postgres", {}).get("records", [])}
        if settings.get("table") in record_tables:
            raise ValueError("편입 대상 보고서 테이블은 postgres.records로 직접 수집할 수 없습니다.")
        mapping = admission.get("mapping", {})
        if not isinstance(mapping, dict):
            raise ValueError("admission.mapping은 설정 객체여야 합니다.")
        file_inputs = mapping.get("files", {})
        if isinstance(file_inputs, dict) and file_inputs.get("registry") and config.get("documents", {}).get("root"):
            root = (base / file_inputs["root"]).resolve()
            normal = (base / config["documents"]["root"]).resolve()
            if root.is_relative_to(normal) or normal.is_relative_to(root):
                raise ValueError("등록형 업로드 원본은 직접 수집 문서와 별도 폴더에 보관해야 합니다.")
        raw_database = mapping.get("database", {})
        if isinstance(raw_database, dict) and raw_database.get("table") in record_tables:
            raise ValueError("자동 매핑 대상 DB 보고서는 postgres.records로 직접 수집할 수 없습니다.")
    synthesis = None
    provider = None
    if "synthesis" in config:
        synthesis = _object(config["synthesis"], "synthesis")
        provider = configure_synthesis(synthesis)

    output.mkdir(parents=True, exist_ok=True)
    with (output / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise BuildInProgressError("같은 출력 경로에 다른 빌드가 실행 중입니다.") from None
            raise
        previous = _current(output)
        sources, equipment, warnings = _load_sources(config, base, output)
        files = (config.get("admission") or {}).get("mapping", {}).get("files", {})
        if isinstance(files, dict) and files.get("root"):
            raw_root = (base / files["root"]).resolve()
            document_root = config.get("documents", {}).get("root")
            if document_root:
                collected = {(base / document_root / source.key.removeprefix("docs:")).resolve()
                             for source in sources if source.kind == "document"}
                for entry in files.get("entries", []):
                    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                        if (raw_root / entry["path"]).resolve() in collected:
                            raise ValueError("자동 매핑 대상 파일은 documents로 직접 수집할 수 없습니다.")
        yield PreparedBuild(config, output, previous, sources, equipment, warnings, synthesis, provider, base)


def _verify_source_snapshot(context: PreparedBuild) -> None:
    sources, equipment, _warnings = _load_sources(context.config, context.base, context.output)
    old = digest({"sources": [asdict(source) for source in context.sources],
                  "equipment": [asdict(asset) for asset in context.equipment]})
    new = digest({"sources": [asdict(source) for source in sources],
                  "equipment": [asdict(asset) for asset in equipment]})
    if old != new:
        raise ValueError("위키 생성 중 원문 또는 설비가 변경되었습니다. 다시 실행하세요.")


def _verify_admitted_reports(context: PreparedBuild, entries: list[dict]) -> None:
    active = active_entries(entries)
    if not active:
        return
    admission = context.config.get("admission")
    if not isinstance(admission, dict):
        raise ValueError("이미 편입된 보고서를 확인할 admission 설정이 필요합니다.")
    from clew_okf.stages.step_02_mapping.mapped_reports import mapped_sources
    inputs = mapped_sources(context.config, context.base, context.output, entries,
                            sources=context.sources, equipment=context.equipment)
    for entry in active:
        approved = ReportRecord.model_validate(entry["report"])
        if "mapping" not in entry:
            canonical = report_store.load_report(
                admission, context.config.get("postgres"), approved.report_id, approved.version
            )
            if canonical.content_hash != approved.content_hash:
                raise RevalidationRequiredError("이미 편입된 보고서의 고정 버전 내용이 변경되었습니다.", code="admitted_report_changed")
        if validate_report_links(approved, context.equipment, [*context.sources, *inputs]):
            raise RevalidationRequiredError("기존 편입 보고서의 근거가 변경되었습니다. 해당 보고서의 새 개정을 검토하세요.", code="admitted_evidence_changed")


def publish_build(
    context: PreparedBuild, entries: list[dict], *, verify_before_publish: Callable[[], None] | None = None
) -> dict:
    """Publish sources and the admitted-report ledger through one current pointer."""
    emit("checking_admitted_reports", "기존 편입 보고서의 원본과 근거를 다시 확인합니다.")
    _verify_admitted_reports(context, entries)
    output, previous = context.output, context.previous
    synthesis, provider = context.synthesis, context.provider
    warnings, equipment = context.warnings, context.equipment
    title = context.config.get("title", "CLEW 제조 위키")
    admission = context.config.get("admission", {})
    reports = admission.get("reports", {}) if isinstance(admission, dict) else {}
    resource = "postgresql://reports/" + reports.get("table", "approved_reports").replace(".", "/")
    from clew_okf.stages.step_02_mapping.mapped_reports import mapped_sources
    sources = sorted(
        [*context.sources, *mapped_sources(context.config, context.base, context.output, entries,
                            sources=context.sources, equipment=context.equipment),
         *report_sources(entries, resource)],
        key=lambda source: source.key,
    )
    fingerprint = digest({
        "builder": "0.1.0",
        "admitted_reports": entries,
        "title": title,
        "sources": [asdict(source) for source in sources],
        "equipment": [asdict(item) for item in equipment],
        "warnings": warnings,
        "synthesis": {"settings": synthesis, "protocol": "quote-annotations-v3"} if synthesis is not None else None,
    })
    snapshots = output / "snapshots"
    snapshots.mkdir(exist_ok=True)
    if snapshots.is_symlink():
        raise ValueError("snapshots는 실제 디렉터리여야 합니다.")
    destination = snapshots / fingerprint
    bundle = destination / "bundle"
    if destination.is_symlink():
        raise ValueError("스냅샷 경로에 심볼릭 링크가 있습니다.")
    if destination.exists():
        validate_bundle(bundle)
        report = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        if report.get("snapshot") != fingerprint:
            raise ValueError("스냅샷 manifest가 출력 경로와 일치하지 않습니다.")
        if verify_before_publish is not None:
            verify_before_publish()
        _verify_admitted_reports(context, entries)
        _verify_source_snapshot(context)
        if previous != bundle:
            _publish(output, bundle)
        return {**report, "state": "unchanged" if previous == bundle else "reused", "bundle": str(output / "current")}

    with TemporaryDirectory(prefix=".build-", dir=snapshots) as temporary:
        stage = Path(temporary)
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        emit("generating_wiki", "원문 인용과 설비별 위키를 작성·검토합니다.")
        result = write_bundle(
            stage / "bundle", title, sources, equipment, timestamp, synthesis, previous, provider=provider
        )
        if any(review["status"] == "unavailable" for review in result.wiki_reviews):
            # Publish the draft without caching an interrupted review for this input.
            destination = snapshots / f"{fingerprint}-review-{uuid4().hex}"
        report = {
            "snapshot": destination.name,
            "generated_at": timestamp,
            "equipment": len(equipment),
            "sources": len(sources),
            "unmapped_documents": sum(source.kind == "document" and not source.equipment_ids for source in sources),
            "selected_quotes": result.selected_quotes,
            "warnings": warnings,
        }
        if synthesis is not None and synthesis.get("review", True):
            report["wiki_reviews"] = result.wiki_reviews
        inventory = [
            {
                "key": source.key,
                "resource": source.resource,
                "revision": source.revision,
                "concept_type": source.concept_type,
                "equipment_ids": source.equipment_ids,
                "evidence_blocks": len(source.blocks),
                "evidence_sha256": digest([asdict(block) for block in source.blocks]),
            }
            for source in sources
        ]
        (stage / "manifest.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "sources.json").write_text(
            json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (stage / "admitted_reports.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        stage.rename(destination)
    if verify_before_publish is not None:
        verify_before_publish()
    _verify_admitted_reports(context, entries)
    _verify_source_snapshot(context)
    _publish(output, destination / "bundle")
    return {**report, "state": "published", "bundle": str(output / "current")}



def build_wiki(config_path: str | Path) -> dict:
    """Compile one configured wiki and return its publication and review report."""
    with prepare_build(config_path) as context:
        return publish_build(context, read_index(context.previous))
