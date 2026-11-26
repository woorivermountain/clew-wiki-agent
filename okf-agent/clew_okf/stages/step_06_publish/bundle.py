import hashlib
import json
import posixpath
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import yaml

from clew_okf._version import __version__
from clew_okf.providers.llm import JsonModel
from clew_okf.runtime.progress import emit
from clew_okf.domain.errors import RequiredEvidenceMissingError
from clew_okf.domain.model import Block, Equipment, Source
from clew_okf.stages.step_05_review.review import review_quotes
from clew_okf.stages.step_04_compose.structured_evidence import EvidenceGroup, resolve_structured_evidence
from clew_okf.stages.step_04_compose.synthesis import _max_parallel_equipment, curate

CITATION = re.compile(r"\[\^(src-[0-9a-f]{24})\]")
DEFINITION = re.compile(r"^\[\^(src-[0-9a-f]{24})\]:", re.MULTILINE)
LINK = re.compile(r"(?<!\\)\]\(([^\s)]+)\)")


def digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def file_key(value: str) -> str:
    readable = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:45].rstrip("-")
    return f"{readable or 'item'}-{digest(value)[:12]}"


def inline(value: str) -> str:
    value = " ".join(value.split())
    return re.sub(r"([\\`*_{\[\]<>}|])", r"\\\1", value)


def document(fields: dict, lines: list[str]) -> str:
    header = yaml.safe_dump(fields, allow_unicode=True, sort_keys=False, width=120)
    return f"---\n{header}---\n\n" + "\n".join(lines).rstrip() + "\n"


def evidence_id(key: str, locator: str, occurrence: int = 0) -> str:
    return "src-" + digest([key, locator, occurrence])[:24]


def evidence(source: Source) -> list[tuple[str, Block]]:
    items = []
    counts: dict[str, int] = {}
    for block in source.blocks:
        occurrence = counts.get(block.locator, 0)
        counts[block.locator] = occurrence + 1
        items.append((evidence_id(source.key, block.locator, occurrence), block))
    return items


def _source_entry(identifier: str, resource: str, title: str) -> dict[str, str]:
    return {"id": identifier, "resource": resource, "title": title}


def _citation(
    identifier: str, title: str, locator: str, revision: str | None, resource: str
) -> str:
    label = " · ".join(inline(value) for value in (title, revision, locator) if value)
    if urlsplit(resource).scheme in {"file", "http", "https"}:
        target = quote(resource, safe=":/?#[]@!$&'*+,;=%")
        label += f" · [원본 열기]({target})"
    return f"[^{identifier}]: {label}"


def _literal(text: str) -> list[str]:
    # Original document content is displayed as evidence, including Markdown in the input.
    runs = re.findall(r"~+", text)
    fence = "~" * max(3, max(map(len, runs), default=0) + 1)
    return [f"{fence}text", text.rstrip(), fence, ""]


def _source_path(source: Source) -> str:
    prefix = "database" if source.kind in {"postgres", "postgres_record"} else "references"
    return f"{prefix}/{file_key(source.key)}.md"


def _relative(source: str, target: str) -> str:
    return posixpath.relpath(target, posixpath.dirname(source) or ".")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _common(title: str, kind: str, timestamp: str) -> dict:
    return {
        "type": kind,
        "title": title,
        "status": "draft",
        "generated": {"by": f"clew-okf/{__version__}", "at": timestamp},
    }


@dataclass(frozen=True, slots=True)
class BundleResult:
    selected_quotes: int
    wiki_reviews: list[dict]
    selected_groups: int = 0


@dataclass(frozen=True, slots=True)
class _RenderedClaim:
    spans: tuple[tuple[str, str], ...]
    label: str | None = None


def _missing_group_finding(
    equipment_key: str, group: EvidenceGroup, lookup: dict[str, tuple[Source, Block]]
) -> dict:
    originals = []
    for identifier, excerpt in group.spans:
        source, block = lookup[identifier]
        originals.append({
            "evidence_id": identifier,
            "source_key": source.key,
            "source_title": source.title,
            "revision": source.revision,
            "locator": block.locator,
            "quote": excerpt,
        })
    return {
        "code": "required_evidence_missing",
        "message": "작성 결과에 필수 근거 묶음이 없습니다.",
        "equipment_key": equipment_key,
        "group_id": group.identifier.removeprefix("group-"),
        "label": group.label,
        "evidence_ids": list(dict.fromkeys(original["evidence_id"] for original in originals)),
        "evidence": originals,
    }


def write_bundle(
    directory: Path,
    title: str,
    sources: list[Source],
    equipment: list[Equipment],
    timestamp: str,
    synthesis: dict | None,
    previous: Path | None,
    *,
    provider: JsonModel | None = None,
) -> BundleResult:
    directory.mkdir()
    pages: dict[str, tuple[str, str]] = {}
    scoped: dict[str, list[Source]] = {item.key: [] for item in equipment}
    for source in sources:
        for key in source.equipment_ids:
            if key not in scoped:
                raise ValueError(f"등록되지 않은 설비 키 {key!r}: {source.key}")
            scoped[key].append(source)

    source_evidence = {source.key: evidence(source) for source in sources}
    protected, groups = resolve_structured_evidence(
        synthesis,
        {identifier: (source, block) for source in sources if source.synthesize
         for identifier, block in source_evidence[source.key]},
        set(scoped),
    )

    for source in sources:
        path = _source_path(source)
        default_kind = {"postgres": "Database Table", "postgres_record": "Database Record"}
        kind = source.concept_type or default_kind.get(source.kind, "Reference")
        fields = _common(source.title, kind, timestamp)
        fields.update(resource=source.resource, source_key=source.key)
        if source.revision:
            fields["revision"] = source.revision
        if source.equipment_ids:
            fields["equipment_ids"] = list(source.equipment_ids)
        lines = [f"# {inline(source.title)}", ""]
        for key in source.equipment_ids if source.kind == "report" else ():
            target = f"equipment/{file_key(key)}.md"
            lines.extend([f"- 적용 설비 [{inline(key)}]({_relative(path, target)})", ""])
        known_sources = {item.key: item for item in sources}
        for key in source.related_sources:
            if key not in known_sources:
                raise ValueError(f"관련 사례 원본을 찾을 수 없습니다: {key}")
            target = known_sources[key]
            lines.extend([f"- 관련 사례 [{inline(target.title)}]({_relative(path, _source_path(target))})", ""])
        refs = []
        definitions = []
        for identifier, block in source_evidence[source.key]:
            refs.append(_source_entry(identifier, source.resource, source.title))
            definitions.append(_citation(identifier, source.title, block.locator, source.revision, source.resource))
            lines.extend([f"## {inline(block.locator)} [^{identifier}]", "", *_literal(block.text)])
        if source.warnings:
            lines.extend(["## 추출 확인 사항", ""])
            lines.extend(f"- {inline(warning)}" for warning in source.warnings)
            lines.append("")
        fields["sources"] = refs
        pages[path] = (source.title, document(fields, lines + definitions))

    review_enabled = synthesis is not None and synthesis.get("review", True)

    def build_equipment(
        item: Equipment,
    ) -> tuple[str, tuple[str, str], int, int, dict | None] | RequiredEvidenceMissingError:
        label = item.fields.get("name") or item.equipment_id
        path = f"equipment/{file_key(item.key)}.md"
        fields = _common(label, "Equipment", timestamp)
        fields.update(equipment_key=item.key, **item.fields)
        base_id = evidence_id(f"equipment:{item.key}", item.locator)
        refs = [_source_entry(base_id, item.resource, "설비 마스터")]
        definitions = [_citation(base_id, "설비 마스터", item.locator, None, item.resource)]
        lines = [f"# {inline(label)}", "", "## 설비 정보", ""]
        lines.extend(
            f"- **{inline(key)}** · {inline(value)} [^{base_id}]"
            for key, value in item.fields.items()
            if value
        )
        lines.extend(["", "## 연결 문서와 데이터", ""])
        lookup: dict[str, tuple[Source, Block]] = {}
        for source in scoped[item.key]:
            target = _source_path(source)
            revision = f" · {inline(source.revision)}" if source.revision else ""
            lines.append(f"- [{inline(source.title)}]({_relative(path, target)}){revision}")
            if source.synthesize:
                lookup.update({identifier: (source, block) for identifier, block in source_evidence[source.key]})
        if not scoped[item.key]:
            lines.append("- 확인할 자료가 아직 연결되지 않았습니다.")
        lines.append("")

        sections: dict[str, list[_RenderedClaim]] = {}
        equipment_groups = {identifier: group for identifier, group in groups.items() if item.key in group.equipment_ids}
        author_evidence = [(identifier, block) for identifier, (_, block) in lookup.items()
                           if identifier not in protected]
        author_evidence.extend((identifier, group.block) for identifier, group in equipment_groups.items())
        used_groups: set[str] = set()
        if synthesis is not None and author_evidence:
            for identifier, section, quote in curate(synthesis, item.key, author_evidence, provider=provider):
                if group := equipment_groups.get(identifier):
                    if identifier in used_groups:
                        continue
                    used_groups.add(identifier)
                    claim = _RenderedClaim(group.spans, group.label)
                else:
                    claim = _RenderedClaim(((identifier, quote),))
                sections.setdefault(section, []).append(claim)

        missing = [
            _missing_group_finding(item.key, group, lookup)
            for identifier, group in equipment_groups.items()
            if group.required and identifier not in used_groups
        ]
        if missing:
            return RequiredEvidenceMissingError(missing)

        ordered_claims: list[tuple[str, str, str]] = []
        draft_groups = []
        for section, statements in sections.items():
            for statement in statements:
                start = len(ordered_claims) + 1
                ordered_claims.extend((identifier, section, quote) for identifier, quote in statement.spans)
                if statement.label is not None:
                    draft_groups.append({
                        "label": statement.label,
                        "claim_ids": list(range(start, len(ordered_claims) + 1)),
                    })

        review_data = None
        by_claim: dict[int, list[dict]] = {}
        if synthesis is not None and review_enabled:
            review = review_quotes(
                synthesis, item.key, item.fields, lookup, ordered_claims,
                provider=provider, draft_groups=draft_groups,
            )
            review_data = asdict(review)
            review_data["writer_model"] = synthesis["model"]
            review_data["review_model"] = synthesis.get("review_model") or synthesis["model"]
            fields["clew_review"] = {
                key: value for key, value in review_data.items() if key != "issues"
            }
            for issue in review.issues:
                by_claim.setdefault(issue["claim_id"], []).append(issue)
            if review.status == "unavailable":
                lines.extend(["## 검토 상태", "", "- 독립 검토를 완료하지 못했습니다.", ""])
            elif review.status == "no_claims":
                lines.extend(["## 검토 상태", "", "- 검토할 인용문이 없습니다.", ""])
            elif not review.coverage_complete:
                lines.extend([
                    "## 검토 범위", "",
                    f"- 검토 입력에 포함된 원문 블록 {review.reviewed_blocks}/{review.total_blocks}개. "
                    "일부 근거는 원문 대조가 필요합니다.", "",
                ])

        cited: set[str] = set()
        claim_id = 0
        for section, statements in sections.items():
            lines.extend([f"## {section}", ""])
            for statement in statements:
                indent = ""
                if statement.label is not None:
                    lines.append(f"- **{inline(statement.label)}**")
                    indent = "  "
                for identifier, quote in statement.spans:
                    claim_id += 1
                    cited.add(identifier)
                    lines.append(f"{indent}- {inline(quote)} [^{identifier}]")
                    for issue in by_claim.get(claim_id, []):
                        issue_ids = issue["evidence_ids"]
                        citations = " ".join(f"[^{source_id}]" for source_id in issue_ids)
                        cited.update(issue_ids)
                        lines.append(f"{indent}  - **검토 필요** · {inline(issue['message'])} {citations}")
            lines.append("")
        for identifier in sorted(cited):
            source, block = lookup[identifier]
            refs.append(_source_entry(identifier, source.resource, source.title))
            definitions.append(_citation(identifier, source.title, block.locator, source.revision, source.resource))
        fields["sources"] = refs
        if used_groups:
            fields["clew_evidence_groups"] = [
                {
                    "id": group.identifier.removeprefix("group-"),
                    "label": group.label,
                    "required": group.required,
                    "evidence": [
                        {"evidence_id": identifier, "quote": quote}
                        for identifier, quote in group.spans
                    ],
                }
                for identifier, group in equipment_groups.items() if identifier in used_groups
            ]
        review_entry = {"equipment_key": item.key, **review_data} if review_data is not None else None
        return path, (label, document(fields, lines + definitions)), len(ordered_claims), len(draft_groups), review_entry

    workers = _max_parallel_equipment(synthesis) if synthesis is not None else 1
    if workers > 1 and len(equipment) > 1:
        with ThreadPoolExecutor(max_workers=min(workers, len(equipment))) as executor:
            equipment_pages = list(executor.map(build_equipment, equipment))
    else:
        equipment_pages = list(map(build_equipment, equipment))

    selected = 0
    selected_groups = 0
    wiki_reviews: list[dict] = []
    missing_findings = []
    for equipment_page in equipment_pages:
        if isinstance(equipment_page, RequiredEvidenceMissingError):
            missing_findings.extend(equipment_page.findings)
            continue
        path, page, quote_count, group_count, review_entry = equipment_page
        pages[path] = page
        selected += quote_count
        selected_groups += group_count
        if review_entry is not None:
            wiki_reviews.append(review_entry)

    if missing_findings:
        raise RequiredEvidenceMissingError(missing_findings)

    index = [f"# {inline(title)}", ""]
    for prefix, heading in (("equipment", "설비"), ("references", "원본 문서"), ("database", "DB")):
        entries = sorted((path, label) for path, (label, _) in pages.items() if path.startswith(prefix + "/"))
        if entries:
            index.extend([f"## {heading}", ""])
            index.extend(f"- [{inline(label)}]({path})" for path, label in entries)
            index.append("")
    previous_pages = (
        {path.relative_to(previous).as_posix() for path in previous.rglob("*.md")}
        - {"index.md", "log.md"}
        if previous else set()
    )
    timestamp_date = datetime.fromisoformat(timestamp).date().isoformat()
    action = "Update" if previous else "Creation"
    lines = [
        "# 변경 이력", "", f"## {timestamp_date}", "",
        f"- **{action}**: 설비 {len(equipment)}건, 원본 {len(sources)}건, OKF 페이지 {len(pages)}건.",
    ]
    if previous_pages - pages.keys():
        lines.append(f"- **Deletion**: 현재 스냅샷에서 사용 종료된 페이지 {len(previous_pages - pages.keys())}건.")
    _write(directory / "index.md", document({"okf_version": "0.2"}, index))
    _write(directory / "log.md", "\n".join(lines) + "\n")
    for path, (_, content) in pages.items():
        _write(directory / path, content)
    validate_bundle(directory)
    return BundleResult(selected, wiki_reviews, selected_groups)


def _parts(text: str) -> tuple[dict, str]:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise ValueError("YAML frontmatter가 없습니다.")
    end = lines.index("---", 1)
    fields = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(fields, dict):
        raise ValueError("YAML frontmatter는 객체여야 합니다.")
    return fields, "\n".join(lines[end + 1:])


def _unfenced(body: str) -> str:
    visible = []
    fence = ""
    for line in body.splitlines():
        if fence:
            if line.strip() == fence:
                fence = ""
            continue
        match = re.match(r"^(~{3,})text$", line)
        if match:
            fence = match.group(1)
        else:
            visible.append(line)
    if fence:
        raise ValueError("원문 블록이 닫히지 않았습니다.")
    return "\n".join(visible)


def validate_bundle(directory: Path) -> int:
    emit("validating_wiki", "OKF 구조와 출처·내부 링크를 확인합니다.")
    """Check generated bundle structure and the CLEW link/citation invariants."""
    directory = directory.resolve()
    if not (directory / "index.md").is_file() or not (directory / "log.md").is_file():
        raise ValueError("번들 루트에 index.md와 log.md가 필요합니다.")
    total = 0
    for path in sorted(directory.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
            if path.name == "index.md":
                if path.parent == directory:
                    fields, body = _parts(text)
                    if fields != {"okf_version": "0.2"}:
                        raise ValueError("루트 index.md의 okf_version은 0.2여야 합니다.")
                else:
                    body = text
                if not body.lstrip().startswith("# "):
                    raise ValueError("index.md에는 제목이 필요합니다.")
            elif path.name == "log.md":
                if not re.search(r"^## \d{4}-\d{2}-\d{2}$", text, re.MULTILINE):
                    raise ValueError("log.md에는 날짜가 있는 변경 기록이 필요합니다.")
                continue
            else:
                total += 1
                fields, body = _parts(text)
                if not isinstance(fields.get("type"), str) or not fields["type"].strip():
                    raise ValueError("비어 있지 않은 OKF type이 필요합니다.")
                sources = fields.get("sources")
                if not isinstance(sources, list) or not sources:
                    raise ValueError("CLEW 페이지에는 하나 이상의 sources가 필요합니다.")
                known = set()
                for source in sources:
                    if not isinstance(source, dict) or not isinstance(source.get("resource"), str):
                        raise ValueError("sources 항목에 resource가 필요합니다.")
                    identifier = source.get("id")
                    if not isinstance(identifier, str) or not re.fullmatch(r"src-[0-9a-f]{24}", identifier):
                        raise ValueError("sources 항목의 ID 형식이 잘못되었습니다.")
                    if identifier in known or not source["resource"].strip():
                        raise ValueError("sources에 중복 ID 또는 비어 있는 resource가 있습니다.")
                    known.add(identifier)
                body = _unfenced(body)
                definitions = DEFINITION.findall(body)
                uses = set(CITATION.findall(DEFINITION.sub("", body)))
                if set(definitions) != known or len(definitions) != len(known) or uses != known:
                    raise ValueError("본문 각주·각주 정의·sources ID가 일치해야 합니다.")
            for target in LINK.findall(body):
                if urlsplit(target).scheme in {"file", "http", "https"}:
                    continue
                resolved = (path.parent / target).resolve()
                if not resolved.is_relative_to(directory) or not resolved.is_file():
                    raise ValueError(f"번들 링크를 찾을 수 없습니다: {target}")
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"{path.relative_to(directory)}: {exc}") from None
    if not total:
        raise ValueError("생성된 OKF 페이지가 없습니다.")
    return total
