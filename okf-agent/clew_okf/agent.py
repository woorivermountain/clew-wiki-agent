"""Callable wiki workflow for services and agent orchestration."""

from dataclasses import dataclass
from pathlib import Path

from clew_okf.workflows.compiler import build_wiki


@dataclass(frozen=True, slots=True)
class WikiAgent:
    """Build a wiki from a server-owned configuration and return its report."""

    config_path: str | Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "config_path", Path(self.config_path).resolve())

    def run(self) -> dict:
        """Read sources, select and review quotes, validate, and publish."""
        return build_wiki(self.config_path)

    def ingest_report(self, report_id: str, version: int) -> dict:
        """Validate and admit one canonical report into the configured wiki."""
        from clew_okf.stages.step_03_admission.admission import ingest_report

        return ingest_report(self.config_path, report_id, version)

    def map_and_ingest_report(self, report_id: str, version: int, source: str) -> dict:
        """Extract supported fields from a registered file or DB report, then admit."""
        from clew_okf.stages.step_03_admission.admission import map_and_ingest_report

        return map_and_ingest_report(self.config_path, report_id, version, source)
