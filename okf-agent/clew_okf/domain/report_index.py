"""Approved report versions stored atomically alongside each wiki snapshot."""

from pathlib import Path
import json

from clew_okf.domain.model import Block, Source
from clew_okf.domain.reports import ReportRecord


def read_index(previous: Path | None) -> list[dict]:
    if previous is None:
        return []
    path = previous.parent / "admitted_reports.json"
    if not path.exists():
        return []
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list):
        raise ValueError("편입 보고서 목록의 형식이 올바르지 않습니다.")
    seen = set()
    for entry in value:
        expected = {"report", "report_hash", "review", "accepted_at"}
        if not isinstance(entry, dict) or set(entry) not in (expected, expected | {"mapping"}):
            raise ValueError("편입 보고서 기록의 형식이 올바르지 않습니다.")
        if "mapping" in entry:
            mapping = entry["mapping"]
            if (not isinstance(mapping, dict) or set(mapping) != {"kind", "input_digest", "dependency_digest", "field_evidence"}
                    or mapping["kind"] not in {"file", "database", "managed"}
                    or not isinstance(mapping["input_digest"], str)
                    or len(mapping["input_digest"]) != 64
                    or any(char not in "0123456789abcdef" for char in mapping["input_digest"])
                    or not isinstance(mapping["dependency_digest"], str)
                    or len(mapping["dependency_digest"]) != 64
                    or any(char not in "0123456789abcdef" for char in mapping["dependency_digest"])
                    or not isinstance(mapping["field_evidence"], dict)):
                raise ValueError("보고서 자동 매핑의 원본 연결 기록이 올바르지 않습니다.")
        report = ReportRecord.model_validate(entry["report"])
        if report.identity_key in seen or report_hash(report) != entry["report_hash"]:
            raise ValueError("편입 보고서의 고유 키 또는 내용 해시가 일치하지 않습니다.")
        review = entry["review"]
        if not isinstance(review, dict) or set(review) != {"decision", "reason", "candidate_ids", "evidence_ids"}:
            raise ValueError("편입 검토 기록의 형식이 올바르지 않습니다.")
        if review["decision"] not in {"create", "new_case", "attach"}:
            raise ValueError("허용되지 않은 편입 결정이 저장돼 있습니다.")
        if not isinstance(review["reason"], str) or not review["reason"].strip():
            raise ValueError("편입 검토 사유가 없습니다.")
        for field in ("candidate_ids", "evidence_ids"):
            if not isinstance(review[field], list) or any(not isinstance(item, str) for item in review[field]):
                raise ValueError("편입 검토의 연결 목록이 올바르지 않습니다.")
        if not {item.evidence_id for item in report.evidence}.intersection(review["evidence_ids"]):
            raise ValueError("편입 검토에 보고서 원문 근거가 없습니다.")
        seen.add(report.identity_key)
    return value


def report_hash(report: ReportRecord) -> str:
    return report.content_hash


def active_entries(entries: list[dict]) -> list[dict]:
    latest = {}
    for entry in entries:
        report = ReportRecord.model_validate(entry["report"])
        current = latest.get(report.report_id)
        if current is None or report.version > current["report"]["version"]:
            latest[report.report_id] = entry
    return [latest[key] for key in sorted(latest)]


def report_sources(entries: list[dict], resource: str) -> list[Source]:
    active = active_entries(entries)
    known = {entry["report"]["report_id"] for entry in active}
    sources = []
    for entry in active:
        report = ReportRecord.model_validate(entry["report"])
        blocks = [
            Block("보고서 식별", f"보고서 {report.report_id} / 버전 {report.version} / 사건 {report.event_id}"),
            Block("적용 조건", report.condition),
            Block("관측 사실", report.observation),
            Block("수행 조치", report.action),
            Block("조치 결과", report.result),
        ]
        for evidence in report.evidence:
            blocks.append(Block(
                f"검증된 근거 {evidence.evidence_id}",
                f"개정 {evidence.revision}\nSHA-256 {evidence.sha256}\n{evidence.quote}",
            ))
        if entry["review"]["candidate_ids"]:
            blocks.append(Block("검토 당시 관련 보고서 버전", "\n".join(entry["review"]["candidate_ids"])))
        mapping = entry.get("mapping")
        related = []
        if mapping is not None:
            blocks.append(Block("공통 필드의 원문 연결", json.dumps(
                mapping["field_evidence"], ensure_ascii=False, indent=2)))
            blocks.append(Block("원본 내용 해시", mapping["input_digest"]))
            blocks.append(Block("편입 검토의 설비·현행 근거 해시", mapping["dependency_digest"]))
            related.append(f"raw-report:{mapping['kind']}:{report.report_id}@{report.version}")
        for key in entry["review"]["candidate_ids"]:
            report_id, _, _version = key.rpartition("@")
            if report_id in known and report_id != report.report_id:
                related.append(f"report:{report_id}")
        sources.append(Source(
            key=f"report:{report.report_id}",
            title=f"{report.title} · {report.report_id}",
            kind="report",
            resource=(f"clew-report://{mapping['kind']}/{report.report_id}/{report.version}"
                      if mapping is not None else resource),
            blocks=tuple(blocks),
            equipment_ids=(report.equipment_key,),
            revision=f"v{report.version}",
            concept_type="Maintenance Case",
            related_sources=tuple(dict.fromkeys(related)),
            synthesize=False,
        ))
    return sources
