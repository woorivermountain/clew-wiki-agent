"""Command-line entry point for the wiki agent."""

import argparse
import csv
import json
import sys
from pathlib import Path

import yaml

from clew_okf.domain.errors import RequiredEvidenceMissingError
from clew_okf.stages.step_06_publish.bundle import validate_bundle
from clew_okf.workflows.compiler import _load_sources, build_wiki
from clew_okf.workflows.release import approve_release, inspect_release_candidate, read_approved_release

# Keep the previous private callable available for existing local integrations.
_build = build_wiki


def main() -> int:
    parser = argparse.ArgumentParser(prog="clew-okf")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="원본을 읽어 새 위키 스냅샷을 검증·게시")
    build.add_argument("--config", type=Path, required=True)
    check = commands.add_parser("check", help="생성된 번들의 OKF 구조와 출처 링크 확인")
    check.add_argument("bundle", type=Path)
    worker = commands.add_parser("worker", help="영속 작업 큐를 처리하는 별도 워커")
    worker.add_argument("--config", type=Path, required=True)
    worker.add_argument("--once", action="store_true", help="준비된 작업 한 건만 처리하고 종료")
    candidate = commands.add_parser("release-candidate", help="담당자가 검토할 위키의 경로·해시·검토 결과")
    candidate.add_argument("--config", type=Path, required=True)
    approve = commands.add_parser("release-approve", help="담당자가 검토한 스냅샷을 서비스용으로 승인")
    approve.add_argument("--config", type=Path, required=True)
    approve.add_argument("--snapshot", required=True)
    approve.add_argument("--sha256", required=True)
    approve.add_argument("--reviewer", required=True)
    approved = commands.add_parser("release-show", help="현재 서비스용으로 승인된 위키 조회")
    approved.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "worker":
            from clew_okf.runtime.worker import run_worker
            run_worker(args.config, once=args.once)
            return 0
        if args.command == "build":
            result = _build(args.config)
        elif args.command == "release-candidate":
            result = inspect_release_candidate(args.config)
        elif args.command == "release-approve":
            result = approve_release(
                args.config, snapshot=args.snapshot, bundle_sha256=args.sha256, reviewer=args.reviewer,
            )
        elif args.command == "release-show":
            result = read_approved_release(args.config)
        else:
            result = {"state": "valid", "pages": validate_bundle(args.bundle)}
    except RequiredEvidenceMissingError as exc:
        print(json.dumps({"status": "hold", "findings": exc.findings}, ensure_ascii=False, indent=2))
        return 1
    except (ValueError, OSError, RuntimeError, csv.Error, yaml.YAMLError) as exc:
        print(f"clew-okf: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
