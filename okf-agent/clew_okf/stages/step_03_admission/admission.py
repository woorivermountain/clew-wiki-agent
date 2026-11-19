"""Admit canonical or automatically mapped reports through the same strict gate."""

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
import json
from typing import Callable
from uuid import uuid4

from clew_okf.stages.step_03_admission.admission_review import evaluate_report
from clew_okf.stages.step_06_publish.bundle import digest, evidence, file_key
from clew_okf.workflows.compiler import PreparedBuild, _load_sources, prepare_build, publish_build
from clew_okf.stages.step_02_mapping.mapped_reports import dependency_digest, mapped_sources, mapping_settings, scoped_input
from clew_okf.domain.model import Source
from clew_okf.runtime.progress import emit
from clew_okf.domain.report_index import active_entries, read_index, report_hash
from clew_okf.stages.step_01_ingest import report_store
from clew_okf.stages.step_01_ingest import raw_report_store
from clew_okf.domain.reports import ReportRecord, ReportRequest


class _SourceChanged(RuntimeError):
    """A source no longer matches the report and evidence reviewed for this request."""


def _finding(code: str, message: str, evidence_ids: list[str] | None = None) -> dict:
    return {"code": code, "message": message, "evidence_ids": evidence_ids or []}


def _response(report: ReportRecord, status: str, findings: list[dict], *, review: dict | None = None,
              build: dict | None = None, action: str = "none") -> dict:
    return _identity_response(report.report_id, report.version, status, findings,
                              review=review, build=build, action=action)


def _identity_response(report_id: str, version: int, status: str, findings: list[dict], *,
                       review: dict | None = None, build: dict | None = None,
                       action: str = "none", mapping: dict | None = None) -> dict:
    result = {"report_id": report_id, "version": version, "status": status, "action": action,
              "findings": findings, "review": review, "build": build,
              "wiki_path": f"references/{file_key('report:' + report_id)}.md" if build else None}
    if mapping is not None:
        result["mapping"] = mapping
    return result


def _audit(output: Path, response: dict) -> dict:
    folder = output / "admission-decisions"
    folder.mkdir(exist_ok=True)
    identifier = uuid4().hex
    receipt = {"decision_id": identifier, "evaluated_at": datetime.now(UTC).isoformat(), **response}
    temporary = folder / f".{identifier}.tmp"
    try:
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(folder / f"{identifier}.json")
    finally:
        temporary.unlink(missing_ok=True)
    return response


def _lineage_findings(report: ReportRecord, entries: list[dict]) -> list[dict]:
    history = [ReportRecord.model_validate(row["report"]) for row in entries
               if row["report"]["report_id"] == report.report_id]
    if not history:
        if report.supersedes_version is not None:
            return [_finding("revision_conflict", "이전에 편입되지 않은 보고서 버전을 대체할 수 없습니다.")]
        return []
    latest = max(history, key=lambda item: item.version)
    if report.version <= latest.version or report.supersedes_version != latest.version:
        return [_finding("revision_conflict", "새 개정은 현재 편입 버전을 명시적으로 대체해야 합니다.")]
    if report.equipment_key != latest.equipment_key or report.event_id != latest.event_id:
        return [_finding("revision_conflict", "같은 보고서의 개정에서 설비 또는 사건 식별자가 달라졌습니다.")]
    return []


def _settings(context: PreparedBuild) -> dict:
    admission = context.config.get("admission")
    if not isinstance(admission, dict) or admission.keys() - {"reports", "mapping", "max_candidates", "review_model"}:
        raise ValueError("admission에는 reports, mapping과 지원되는 검토 설정을 지정하세요.")
    if context.provider is None:
        raise ValueError("보고서 편입에는 synthesis 모델 설정이 필요합니다.")
    maximum = admission.get("max_candidates", 32)
    if type(maximum) is not int or not 1 <= maximum <= 128:
        raise ValueError("admission.max_candidates는 1~128 사이의 정수여야 합니다.")
    model = admission.get("review_model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError("admission.review_model은 비어 있지 않은 문자열이어야 합니다.")
    return admission


def _prior_build(context: PreparedBuild) -> dict:
    manifest = json.loads((context.previous.parent / "manifest.json").read_text(encoding="utf-8"))
    return {**manifest, "state": "unchanged", "bundle": str(context.output / "current")}


def _admit(context: PreparedBuild, report: ReportRecord, entries: list[dict], *,
           verify_report: Callable[[], None], extra_sources: list[Source] | None = None,
           provenance: dict | None = None) -> dict:
    """Shared admission gate. Callers keep the output lock for the entire operation."""
    from clew_okf.domain.reports import validate_report_links

    admission = _settings(context)
    # An explicit replacement is checked against its own current input/context.
    # Its predecessor remains in candidate/history records, not as a current raw input.
    source_entries = [row for row in entries if row["report"]["report_id"] != report.report_id]
    current_inputs = mapped_sources(context.config, context.base, context.output, source_entries,
                                    sources=context.sources, equipment=context.equipment)
    if any(row["report"]["report_id"] == report.report_id and row["report"]["version"] == report.version
           for row in entries) and provenance is not None:
        current_inputs += mapped_sources(context.config, context.base, context.output,
                                        [row for row in entries if row["report"]["report_id"] == report.report_id],
                                        sources=context.sources, equipment=context.equipment)
    sources = [*context.sources, *current_inputs, *(extra_sources or [])]
    existing = next((row for row in entries if row["report"]["report_id"] == report.report_id
                     and row["report"]["version"] == report.version), None)
    if existing is not None:
        if existing.get("mapping") != provenance:
            return _response(report, "conflict", [_finding("origin_conflict", "동일 보고서 ID·버전의 입력 출처가 다릅니다.")])
        if existing["report_hash"] != report_hash(report):
            return _response(report, "conflict", [
                _finding("identity_content_conflict", "동일 보고서 ID·버전의 내용이 변경되었습니다.")])
        emit("validating_evidence", "설비 연결과 인용·개정·해시를 검사합니다.")
        findings = validate_report_links(report, context.equipment, sources)
        if findings:
            return _response(report, "rejected", findings)
        return _response(report, "unchanged", [], review=existing["review"],
                         build=_prior_build(context), action="no_change")

    emit("validating_evidence", "설비 연결과 인용·개정·해시를 검사합니다.")
    lineage = _lineage_findings(report, entries)
    if lineage:
        return _response(report, "conflict", lineage)
    findings = validate_report_links(report, context.equipment, sources)
    if findings:
        return _response(report, "rejected", findings)
    candidates = [ReportRecord.model_validate(row["report"]) for row in active_entries(entries)
                  if ReportRecord.model_validate(row["report"]).equipment_key == report.equipment_key]
    identical = next((item for item in candidates if item.report_id != report.report_id
                      and item.case_hash == report.case_hash), None)
    if identical is not None:
        review = {"decision": "duplicate", "reason": "같은 사건의 내용과 근거가 이미 편입돼 있습니다.",
                  "candidate_ids": [identical.identity_key],
                  "evidence_ids": [item.evidence_id for item in report.evidence]}
        return _response(report, "duplicate", [], review=review, action="no_change")
    if len(candidates) > admission.get("max_candidates", 32):
        return _response(report, "hold", [_finding("candidate_budget", "비교 후보가 한도를 넘어 전체 비교를 완료할 수 없습니다.")])
    lookup = {identifier: (source, block) for source in sources
              if report.equipment_key in source.equipment_ids for identifier, block in evidence(source)}
    review = evaluate_report(report, candidates, lookup, context.provider, model=admission.get("review_model"),
                             field_evidence=provenance["field_evidence"] if provenance is not None else None)
    if review["decision"] in {"hold", "duplicate"}:
        return _response(report, review["decision"], [], review=review, action="no_change")

    def verify_sources() -> None:
        verify_report()
        reloaded, equipment, _warnings = _load_sources(context.config, context.base, context.output)
        before = digest({"sources": [asdict(item) for item in context.sources],
                         "equipment": [asdict(item) for item in context.equipment]})
        after = digest({"sources": [asdict(item) for item in reloaded],
                        "equipment": [asdict(item) for item in equipment]})
        if before != after:
            raise _SourceChanged("검토 중 설비 또는 원문이 변경되어 다시 검토해야 합니다.")

    entry = {"report": report.model_dump(mode="json"), "report_hash": report_hash(report),
             "review": review, "accepted_at": datetime.now(UTC).isoformat()}
    if provenance is not None:
        entry["mapping"] = provenance
    try:
        verify_sources()
        build = publish_build(context, [*entries, entry], verify_before_publish=verify_sources)
    except _SourceChanged as error:
        return _response(report, "conflict", [_finding("source_changed", str(error))], review=review)
    action = "update" if any(row["report"]["report_id"] == report.report_id for row in entries) else review["decision"]
    return _response(report, "published", [], review=review, build=build, action=action)


def ingest_report(config_path: str | Path, report_id: str, version: int) -> dict:
    """Load one server-owned canonical report and use the common admission gate."""
    ReportRequest(version=version)
    with prepare_build(config_path) as context:
        admission = _settings(context)
        report = report_store.load_report(admission, context.config.get("postgres"), report_id, version)
        def verify() -> None:
            reloaded = report_store.load_report(admission, context.config.get("postgres"), report_id, version)
            if report_hash(reloaded) != report_hash(report):
                raise _SourceChanged("검토 중 보고서의 고정 버전 내용이 변경되었습니다.")
        return _audit(context.output, _admit(context, report, read_index(context.previous), verify_report=verify))


def map_and_ingest_report(config_path: str | Path, report_id: str, version: int, source: str) -> dict:
    """Map a registered original conservatively, then use the existing admission gate."""
    from clew_okf.stages.step_02_mapping.mapping import map_report
    from clew_okf.domain.reports import validate_report_links

    ReportRequest(version=version)
    if source not in {"file", "database", "managed"}:
        raise ValueError("source는 file, database 또는 managed여야 합니다.")
    with prepare_build(config_path) as context:
        _settings(context)
        settings = mapping_settings(context.config)
        try:
            raw = raw_report_store.load_raw_report(
                settings, context.config.get("postgres"), context.base, context.output, report_id, version, source)
        except RuntimeError:
            return _audit(context.output, _identity_response(report_id, version, "hold", [
                _finding("raw_source_unavailable", "보고서 원본·추출 결과·확정 또는 재사용 승인을 확인하지 못했습니다.")]))
        entries = read_index(context.previous)
        existing = next((row for row in entries if row["report"]["report_id"] == report_id
                         and row["report"]["version"] == version), None)
        if existing is not None:
            origin = existing.get("mapping")
            if origin is None or origin["kind"] != source or origin["input_digest"] != raw.fingerprint:
                return _audit(context.output, _identity_response(report_id, version, "conflict", [
                    _finding("identity_content_conflict", "동일 보고서 ID·버전의 원본 또는 출처가 변경되었습니다.")]))
            report = ReportRecord.model_validate(existing["report"])
            inputs = mapped_sources(context.config, context.base, context.output, entries,
                                    sources=context.sources, equipment=context.equipment)
            findings = validate_report_links(report, context.equipment, [*context.sources, *inputs])
            return _audit(context.output, _response(report, "rejected", findings) if findings else
                          _response(report, "unchanged", [], review=existing["review"],
                                    build=_prior_build(context), action="no_change"))
        mapped = map_report(raw, context.equipment, context.provider, model=settings.get("model"))
        if mapped["status"] != "mapped":
            return _audit(context.output, _identity_response(report_id, version, "hold", mapped["findings"], mapping=mapped))
        report = ReportRecord.model_validate(mapped["report"])
        provenance = {**raw.origin, "field_evidence": mapped["field_evidence"],
                      "dependency_digest": dependency_digest(report.equipment_key, context.sources, context.equipment)}
        if set(provenance) != {"kind", "input_digest", "dependency_digest", "field_evidence"}:
            raise ValueError("자동 매핑 원본 연결 형식이 올바르지 않습니다.")
        def verify() -> None:
            try:
                latest = raw_report_store.load_raw_report(
                    settings, context.config.get("postgres"), context.base, context.output, report_id, version, source)
            except (RuntimeError, ValueError):
                raise _SourceChanged("검토 중 보고서 원본 또는 승인을 다시 확인하지 못했습니다.") from None
            if latest.fingerprint != raw.fingerprint:
                raise _SourceChanged("검토 중 보고서 원본 또는 승인 메타데이터가 변경되었습니다.")
        try:
            verify()
            result = _admit(context, report, entries, verify_report=verify,
                            extra_sources=[scoped_input(raw, report)], provenance=provenance)
        except _SourceChanged as error:
            result = _response(report, "conflict", [_finding("source_changed", str(error))])
        result["mapping"] = mapped
        return _audit(context.output, result)
