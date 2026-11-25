"""Resolve explicitly linked excerpts without losing the original citations."""

from dataclasses import dataclass

from clew_okf.domain.model import Block, Source

# Groups must fit intact in an author evidence block and each span must fit the
# existing verbatim quotation format.
_MAX_GROUP_CHARS = 4_000
_MAX_SPAN_CHARS = 600


@dataclass(frozen=True, slots=True)
class EvidenceGroup:
    identifier: str
    label: str
    equipment_ids: tuple[str, ...]
    spans: tuple[tuple[str, str], ...]
    block: Block
    required: bool = False


def _object(value: object, keys: set[str], location: str, *, optional: tuple[str, ...] = ()) -> dict:
    if not isinstance(value, dict) or not keys <= value.keys() <= keys.union(optional):
        supported = ", ".join(sorted(keys.union(optional)))
        raise ValueError(f"{location}의 필드를 확인하세요. 지원 항목: {supported}")
    return value


def _list(value: object, location: str, *, allow_empty: bool = False) -> list:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise ValueError(f"{location}에 목록을 지정하세요.")
    return value


def _text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{location}에 비어 있지 않은 문자열을 지정하세요.")
    return value


def _unique_strings(value: object, location: str) -> tuple[str, ...]:
    strings = tuple(_text(entry, location) for entry in _list(value, location))
    if len(strings) != len(set(strings)):
        raise ValueError(f"{location}에 중복 항목이 있습니다.")
    return strings


def _resolve_span(
    spec: object, location: str, protected: dict[str, list[tuple[str, Block]]],
) -> tuple[str, str]:
    span = _object(spec, {"locator", "quote"}, location)
    locator = _text(span["locator"], f"{location}.locator")
    quote = _text(span["quote"], f"{location}.quote")
    if locator not in protected:
        raise ValueError(f"{location}의 위치 {locator!r}는 보호 목록에 있어야 합니다.")
    if len(quote) > _MAX_SPAN_CHARS:
        raise ValueError(f"{location}의 인용문은 {_MAX_SPAN_CHARS}자 이하여야 합니다.")

    matches = []
    for identifier, block in protected[locator]:
        offset = block.text.find(quote)
        if offset < 0:
            continue
        if block.text.find(quote, offset + 1) >= 0:
            raise ValueError(f"{location}의 인용문이 원문의 같은 위치에 반복되어 범위를 확인할 수 없습니다.")
        matches.append(identifier)
    if len(matches) != 1:
        raise ValueError(f"{location}의 인용문은 원문의 {locator!r}에서 정확히 한 번 확인돼야 합니다.")
    return matches[0], quote


def _resolve_group(
    spec: object,
    location: str,
    source: Source,
    equipment_keys: set[str],
    protected: dict[str, list[tuple[str, Block]]],
) -> EvidenceGroup:
    entry = _object(spec, {"id", "label", "equipment_ids", "spans"}, location, optional=("required",))
    required = entry.get("required", False)
    if type(required) is not bool:
        raise ValueError(f"{location}.required는 true 또는 false여야 합니다.")
    group_id = _text(entry["id"], f"{location}.id")
    label = _text(entry["label"], f"{location}.label")
    if not group_id.isprintable() or not label.isprintable():
        raise ValueError(f"{location}의 id와 label은 한 줄에 표시 가능한 문자열이어야 합니다.")
    scope = _unique_strings(entry["equipment_ids"], f"{location}.equipment_ids")
    invalid_scope = set(scope) - (equipment_keys & set(source.equipment_ids))
    if invalid_scope:
        raise ValueError(f"{location}의 설비 범위가 원문의 적용 범위를 벗어났습니다: {', '.join(sorted(invalid_scope))}")

    spans = tuple(
        _resolve_span(span, f"{location}.spans[{index}]", protected)
        for index, span in enumerate(_list(entry["spans"], f"{location}.spans"), 1)
    )
    if len(spans) != len(set(spans)):
        raise ValueError(f"{location}에 같은 원문 인용이 중복되었습니다.")
    text = "\n\n".join([label, *(quote for _, quote in spans)])
    if len(text) > _MAX_GROUP_CHARS:
        raise ValueError(f"{location}의 근거 묶음은 {_MAX_GROUP_CHARS}자 이하여야 합니다.")
    identifier = f"group-{group_id}"
    return EvidenceGroup(identifier, label, scope, spans, Block(identifier, text), required=required)


def resolve_structured_evidence(
    synthesis: dict | None,
    lookup: dict[str, tuple[Source, Block]],
    equipment_keys: set[str],
) -> tuple[frozenset[str], dict[str, EvidenceGroup]]:
    """Resolve protected locations and groups from the unchanged source blocks."""
    location = "synthesis.structured_evidence"
    if synthesis is None or "structured_evidence" not in synthesis:
        return frozenset(), {}
    specs = _list(synthesis["structured_evidence"], location)

    sources: dict[str, tuple[Source, dict[str, list[tuple[str, Block]]]]] = {}
    for identifier, (source, block) in lookup.items():
        _, locations = sources.setdefault(source.key, (source, {}))
        locations.setdefault(block.locator, []).append((identifier, block))

    protected_ids: set[str] = set()
    groups: dict[str, EvidenceGroup] = {}
    configured_sources: set[str] = set()
    for index, spec in enumerate(specs, 1):
        entry_location = f"{location}[{index}]"
        entry = _object(spec, {"source_key", "protected_locators", "groups"}, entry_location)
        source_key = _text(entry["source_key"], f"{entry_location}.source_key")
        if source_key in configured_sources:
            raise ValueError(f"{location}에 원문 {source_key!r}를 중복 지정했습니다.")
        configured_sources.add(source_key)
        if source_key not in sources or not sources[source_key][0].synthesize:
            raise ValueError(f"{entry_location}에서 합성할 수 있는 원문을 찾지 못했습니다: {source_key}")
        source, locations = sources[source_key]
        locators = _unique_strings(entry["protected_locators"], f"{entry_location}.protected_locators")
        missing_locators = set(locators) - locations.keys()
        if missing_locators:
            raise ValueError(f"{entry_location}의 원문 위치를 찾지 못했습니다: {', '.join(sorted(missing_locators))}")

        protected = {locator: locations[locator] for locator in locators}
        protected_ids.update(identifier for records in protected.values() for identifier, _ in records)
        for group_index, group_spec in enumerate(_list(entry["groups"], f"{entry_location}.groups", allow_empty=True), 1):
            group_location = f"{entry_location}.groups[{group_index}]"
            group = _resolve_group(group_spec, group_location, source, equipment_keys, protected)
            if group.identifier in groups or group.identifier in lookup:
                raise ValueError(f"{group_location}의 근거 ID {group.identifier!r}가 중복되었습니다.")
            groups[group.identifier] = group

    return frozenset(protected_ids), groups
