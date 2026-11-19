"""Revalidate and materialize original inputs for accepted mapped reports."""

from dataclasses import asdict, replace
from pathlib import Path

from clew_okf.domain.model import Equipment, Source
from clew_okf.stages.step_06_publish.bundle import digest
from clew_okf.domain.errors import RevalidationRequiredError
from clew_okf.domain.report_index import active_entries
from clew_okf.domain.reports import ReportRecord
from clew_okf.stages.step_01_ingest import raw_report_store


def mapping_settings(config: dict) -> dict:
    admission = config.get("admission", {})
    settings = admission.get("mapping") if isinstance(admission, dict) else None
    if not isinstance(settings, dict) or settings.keys() - {"files", "database", "managed", "model"}:
        raise ValueError("admission.mapping에 files 또는 database 입력을 설정하세요.")
    model = settings.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("admission.mapping.model은 비어 있지 않은 문자열이어야 합니다.")
    return settings


def scoped_input(raw: raw_report_store.RawReport, report: ReportRecord) -> Source:
    return replace(raw.source, equipment_ids=(report.equipment_key,))


def dependency_digest(equipment_key: str, sources: list[Source], equipment: list[Equipment]) -> str:
    """Pin the complete scoped context against which semantic admission was checked."""
    return digest({"sources": [asdict(item) for item in sources if equipment_key in item.equipment_ids],
                   "equipment": [asdict(item) for item in equipment if item.key == equipment_key]})


def mapped_sources(config: dict, base: Path, output: Path, entries: list[dict], *,
                   sources: list[Source], equipment: list[Equipment]) -> list[Source]:
    """Read current original bytes/DB rows again; never remap accepted content."""
    result = []
    for entry in active_entries(entries):
        provenance = entry.get("mapping")
        if provenance is None:
            continue
        report = ReportRecord.model_validate(entry["report"])
        if dependency_digest(report.equipment_key, sources, equipment) != provenance["dependency_digest"]:
            raise RevalidationRequiredError("편입 심사 당시의 설비 또는 현행 근거가 변경되었습니다. 새 개정을 검토하세요.", code="dependency_changed")
        try:
            raw = raw_report_store.load_raw_report(
                mapping_settings(config), config.get("postgres"), base, output,
                report.report_id, report.version, provenance["kind"],
            )
        except RuntimeError:
            raise RevalidationRequiredError(
                "기존 편입 보고서의 원본 또는 확정·재사용 승인을 확인하지 못했습니다. 다시 검토하세요.",
                code="admitted_source_unavailable") from None
        if raw.fingerprint != provenance["input_digest"]:
            raise RevalidationRequiredError("편입된 보고서의 원본 또는 승인 메타데이터가 변경되었습니다. 새 개정을 검토하세요.", code="admitted_source_changed")
        result.append(scoped_input(raw, report))
    return result
