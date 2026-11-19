from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Block:
    locator: str
    text: str


@dataclass(frozen=True, slots=True)
class Source:
    key: str
    title: str
    kind: str
    resource: str
    blocks: tuple[Block, ...]
    equipment_ids: tuple[str, ...] = ()
    revision: str | None = None
    warnings: tuple[str, ...] = ()
    concept_type: str | None = None
    related_sources: tuple[str, ...] = ()
    synthesize: bool = True


@dataclass(frozen=True, slots=True)
class Equipment:
    equipment_id: str
    fields: dict[str, str]
    resource: str
    locator: str

    @property
    def key(self) -> str:
        plant = self.fields.get("plant_id")
        return f"{plant}/{self.equipment_id}" if plant else self.equipment_id
