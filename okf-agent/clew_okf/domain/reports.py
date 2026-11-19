"""Strict report input and deterministic checks against scoped source blocks."""

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from clew_okf.stages.step_06_publish.bundle import digest, evidence
from clew_okf.domain.model import Block, Equipment, Source


PositiveVersion = Annotated[int, Field(strict=True, gt=0)]
Identifier = Annotated[str, StringConstraints(min_length=1, pattern=r"^[^\s/]+$")]
ReportIdentifier = Annotated[
    str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
]
ReportText = Annotated[str, StringConstraints(min_length=1, max_length=4000)]


class _ReportModel(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")


class ReportRequest(_ReportModel):
    version: PositiveVersion


class ReportEvidence(_ReportModel):
    evidence_id: Annotated[str, StringConstraints(pattern=r"^src-[0-9a-f]{24}$")]
    revision: Annotated[str, StringConstraints(min_length=1)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    quote: Annotated[str, StringConstraints(min_length=1, max_length=600)]

    @field_validator("revision", "quote")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace characters")
        return value


class ReportRecord(_ReportModel):
    report_id: ReportIdentifier
    version: PositiveVersion
    plant_id: Identifier
    equipment_id: Identifier
    event_id: Identifier
    title: ReportText
    condition: ReportText
    observation: ReportText
    action: ReportText
    result: ReportText
    status: Literal["finalized"]
    reuse_approved: Literal[True]
    supersedes_version: PositiveVersion | None = None
    evidence: Annotated[list[ReportEvidence], Field(min_length=1, max_length=16)]

    @field_validator("reuse_approved", mode="before")
    @classmethod
    def strict_approval(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("reuse_approved must be the boolean true")
        return value

    @field_validator("plant_id", "equipment_id", "event_id")
    @classmethod
    def identifier_without_whitespace(cls, value: str) -> str:
        if "/" in value or re.search(r"\s", value):
            raise ValueError("identifiers cannot contain slash or whitespace")
        return value

    @field_validator("title", "condition", "observation", "action", "result")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must contain non-whitespace characters")
        return value

    @field_validator("evidence")
    @classmethod
    def unique_evidence(cls, value: list[ReportEvidence]) -> list[ReportEvidence]:
        identifiers = [item.evidence_id for item in value]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("evidence_id must be unique within a report")
        return value

    @property
    def equipment_key(self) -> str:
        return f"{self.plant_id}/{self.equipment_id}"

    @property
    def identity_key(self) -> str:
        return f"{self.report_id}@{self.version}"

    @property
    def content_hash(self) -> str:
        return digest(self.model_dump(mode="json"))

    def _knowledge_payload(self) -> dict:
        normalized_evidence = sorted(
            json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
            for item in self.evidence
        )
        return {
            "equipment_key": self.equipment_key,
            "condition": self.condition,
            "observation": self.observation,
            "action": self.action,
            "result": self.result,
            "evidence": normalized_evidence,
        }

    @property
    def case_hash(self) -> str:
        return digest({**self._knowledge_payload(), "event_id": self.event_id})

    @property
    def knowledge_hash(self) -> str:
        return digest(self._knowledge_payload())


def validate_report_links(
    report: ReportRecord, equipment: list[Equipment], sources: list[Source]
) -> list[dict]:
    """Return hold findings; never infer links or repair report evidence."""
    findings: list[dict] = []

    def hold(code: str, message: str, identifiers: list[str]) -> None:
        findings.append({"code": code, "message": message, "evidence_ids": identifiers})

    if report.equipment_key not in {asset.key for asset in equipment}:
        hold(
            "asset_not_registered",
            f"설비 {report.equipment_key}가 현재 설비 목록에 없습니다.",
            [],
        )

    # Retain every candidate so out-of-scope entries cannot hide scoped ones.
    blocks: dict[str, list[tuple[Source, Block]]] = {}
    for source in sources:
        for identifier, block in evidence(source):
            blocks.setdefault(identifier, []).append((source, block))

    for reference in report.evidence:
        identifiers = [reference.evidence_id]
        candidates = blocks.get(reference.evidence_id, [])
        if not candidates:
            hold("evidence_unknown", "현재 원본에서 근거 블록을 찾을 수 없습니다.", identifiers)
            continue
        scoped = [
            pair for pair in candidates if report.equipment_key in pair[0].equipment_ids
        ]
        if not scoped:
            hold(
                "evidence_wrong_equipment",
                f"근거가 설비 {report.equipment_key} 범위에 포함되지 않습니다.",
                identifiers,
            )
            continue
        # The compiler rejects conflicting source identities. Direct callers
        # also receive a hold rather than an arbitrary choice of source block.
        if len(scoped) != 1:
            hold("evidence_ambiguous", "동일한 근거 식별자가 여러 원본에 있습니다.", identifiers)
            continue
        source, block = scoped[0]
        if not source.revision or not source.revision.strip():
            hold("revision_missing", "현재 원본의 리비전이 없어 근거를 확인할 수 없습니다.", identifiers)
        elif source.revision != reference.revision:
            hold("revision_mismatch", "보고서 근거의 리비전이 현재 원본과 다릅니다.", identifiers)
        actual_hash = hashlib.sha256(block.text.encode("utf-8")).hexdigest()
        if reference.sha256 != actual_hash:
            hold("evidence_hash_mismatch", "근거 블록의 SHA-256이 현재 원본과 다릅니다.", identifiers)
        if reference.quote not in block.text:
            hold("quote_not_found", "인용문이 현재 근거 블록의 정확한 부분 문자열이 아닙니다.", identifiers)

    return findings
