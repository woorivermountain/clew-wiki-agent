"""Explicit approval of the exact bytes of an OKF snapshot."""

from __future__ import annotations

from datetime import UTC, datetime
import fcntl
from hashlib import file_digest
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile

import yaml

from clew_okf.stages.step_06_publish.bundle import digest, validate_bundle
from clew_okf.workflows.compiler import BuildInProgressError, _current, _object, _path


_APPROVAL_FILE = "approved-release.json"
_SNAPSHOT_ID = re.compile(r"[0-9a-f]{64}(?:-review-[0-9a-f]{32})?")


class ReleaseNotApprovedError(RuntimeError):
    """There is no intact, explicitly approved snapshot available to serve."""


def _output_path(config_path: str | Path) -> Path:
    config_path = Path(config_path).resolve()
    config = _object(yaml.safe_load(config_path.read_text(encoding="utf-8")), "설정")
    return _path(config.get("output", "output"), config_path.parent, "output")


def _snapshot(output: Path, snapshot: str) -> tuple[Path, dict]:
    if not isinstance(snapshot, str) or not _SNAPSHOT_ID.fullmatch(snapshot):
        raise ValueError("위키 스냅샷 ID가 올바르지 않습니다.")
    root = output / "snapshots" / snapshot
    bundle = root / "bundle"
    manifest_path = root / "manifest.json"
    if not all(path.resolve() == path for path in (root, bundle, manifest_path)) or not bundle.is_dir():
        raise ValueError("관리되는 위키 스냅샷을 찾을 수 없습니다.")
    manifest = _object(json.loads(manifest_path.read_text(encoding="utf-8")), "위키 빌드 기록")
    if manifest.get("snapshot") != snapshot:
        raise ValueError("위키 스냅샷이 빌드 기록과 일치하지 않습니다.")
    return bundle, manifest


def _content_hash(bundle: Path) -> str:
    hashes = []
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink():
            raise ValueError("승인할 번들 내부에 심볼릭 링크가 있습니다.")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("승인할 번들 안에는 일반 파일만 둘 수 있습니다.")
        with path.open("rb") as stream:
            hashes.append((path.relative_to(bundle).as_posix(), file_digest(stream, "sha256").hexdigest()))
    return digest(hashes)


def _candidate(output: Path) -> dict:
    current = _current(output)
    if current is None:
        raise ReleaseNotApprovedError("검토할 위키 초안이 없습니다.")
    snapshot = current.parent.name
    bundle, manifest = _snapshot(output, snapshot)
    if bundle != current:
        raise ValueError("현재 초안과 관리 스냅샷이 일치하지 않습니다.")
    validate_bundle(bundle)
    return {
        "status": "draft",
        "snapshot": snapshot,
        "bundle": str(bundle),
        "bundle_sha256": _content_hash(bundle),
        "wiki_reviews": manifest.get("wiki_reviews", []),
        "warnings": manifest.get("warnings", []),
    }


def inspect_release_candidate(config_path: str | Path) -> dict:
    """Return the snapshot identity to use when approving after review."""
    return _candidate(_output_path(config_path))


def approve_release(
    config_path: str | Path, *, snapshot: str, bundle_sha256: str, reviewer: str,
) -> dict:
    """Record an operator's approval of the inspected current snapshot."""
    reviewer = reviewer.strip()
    if not reviewer or not reviewer.isprintable():
        raise ValueError("승인 담당자 이름을 입력하세요.")
    output = _output_path(config_path)
    if not output.is_dir():
        raise ReleaseNotApprovedError("검토할 위키 초안이 없습니다.")
    with (output / ".build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BuildInProgressError("빌드 중에는 위키 초안을 승인할 수 없습니다.") from None
        candidate = _candidate(output)
        if (snapshot, bundle_sha256) != (candidate["snapshot"], candidate["bundle_sha256"]):
            raise ReleaseNotApprovedError("검토 이후 초안이 바뀌었습니다. 최신 초안을 다시 확인하세요.")
        approval = {
            "status": "approved",
            "snapshot": snapshot,
            "bundle_sha256": bundle_sha256,
            "approved_by": reviewer,
            "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        with NamedTemporaryFile(mode="w", dir=output, delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            try:
                json.dump(approval, stream, ensure_ascii=False, indent=2)
                stream.close()
                temporary.replace(output / _APPROVAL_FILE)
            finally:
                temporary.unlink(missing_ok=True)
    return {**approval, "bundle": candidate["bundle"]}


def read_approved_release(config_path: str | Path) -> dict:
    """Expose only the explicitly approved snapshot, never the latest draft."""
    output = _output_path(config_path)
    record = output / _APPROVAL_FILE
    if record.is_symlink() or not record.is_file():
        raise ReleaseNotApprovedError("승인된 위키가 없습니다.")
    try:
        approval = _object(json.loads(record.read_text(encoding="utf-8")), "위키 승인 기록")
        if approval["status"] != "approved":
            raise ValueError
        if any(not isinstance(approval[key], str) or not approval[key].strip()
               for key in ("approved_by", "approved_at")):
            raise ValueError
        bundle, _ = _snapshot(output, approval["snapshot"])
        if _content_hash(bundle) != approval["bundle_sha256"]:
            raise ValueError
    except (ValueError, KeyError, OSError):
        raise ReleaseNotApprovedError("승인 기록 또는 승인된 위키가 변경됐습니다. 다시 검토하세요.") from None
    return {key: approval[key] for key in (
        "status", "snapshot", "bundle_sha256", "approved_by", "approved_at",
    )} | {"bundle": str(bundle)}
