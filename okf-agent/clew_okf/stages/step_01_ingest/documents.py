"""Read source documents while preserving page, section, or line references."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from clew_okf.domain.model import Block, Source

_TEXT_FORMATS = {".md", ".markdown", ".txt", ".csv"}
_KORDOC_FORMATS = {".hwp", ".hwpx", ".pdf", ".docx", ".pptx", ".xlsx"}
_OCR_FORMATS = {".hwp", ".hwpx", ".pdf"}
_FILE_OPTIONS = {"title", "type", "equipment_ids", "revision", "source_uri", "ocr", "synthesize"}
_IMAGE_LINE = re.compile(r"^\s*!\[[^\]\n]*\]\([^\n]*\)\s*$", re.MULTILINE)

_CACHE_FORMAT = 1
_Extraction = tuple[tuple[Block, ...], tuple[str, ...]]


def load_documents(
    config: dict, base: Path, *, output: Path, cache: Path | None = None
) -> tuple[list[Source], list[str]]:
    """Load supported documents from a source tree and report files that were skipped."""
    if not isinstance(config, dict):
        raise ValueError("documents must be a mapping")
    root = (base / _required_text(config.get("root"), "documents.root")).resolve()
    if not root.is_dir():
        raise ValueError(f"Document root is not a directory: {root}")

    output = (base / output).resolve()
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError("Document root and output directory must not overlap")

    executable = _required_text(config.get("kordoc", "kordoc"), "documents.kordoc")
    if os.sep in executable or (os.altsep and os.altsep in executable):
        executable = str((base / executable).resolve())
    timeout = config.get("timeout_seconds", 120)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("documents.timeout_seconds must be a positive number")
    max_bytes = config.get("max_bytes", 64 * 1024 * 1024)
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("documents.max_bytes must be a positive integer")

    file_options = _validate_file_options(config.get("files", {}))
    include_only_listed = config.get("include_only_listed", bool(file_options))
    if type(include_only_listed) is not bool:
        raise ValueError("documents.include_only_listed must be a boolean")
    discovered, warnings = _discover_files(root)
    missing = sorted(file_options.keys() - discovered.keys())
    if missing:
        raise ValueError(f"Mapped document files are missing or not regular files: {', '.join(missing)}")

    if include_only_listed:
        discovered = {relative: discovered[relative] for relative in file_options}

    parser = None
    if cache is not None and any(path.suffix.lower() in _KORDOC_FORMATS for path in discovered.values()):
        cache = _prepare_cache(base / cache, root, warnings)
        if cache is not None:
            parser = _parser_identity(executable, timeout)

    sources = []
    for relative, path in sorted(discovered.items()):
        suffix = path.suffix.lower()
        if suffix not in _TEXT_FORMATS | _KORDOC_FORMATS:
            warnings.append(f"Unsupported document format: {relative}")
            continue
        if path.stat().st_size > max_bytes:
            raise ValueError(f"Document exceeds documents.max_bytes ({max_bytes}): {relative}")
        options = file_options.get(relative, {})
        if suffix in _TEXT_FORMATS:
            blocks, parse_warnings = _read_text(path, relative)
        else:
            ocr = options.get("ocr", False)
            blocks, parse_warnings = _extract_document(
                path, relative, executable=executable, timeout=timeout,
                ocr=ocr, cache=cache, parser=parser,
            )
        sources.append(Source(
            key=f"docs:{relative}",
            title=options.get("title", path.stem),
            kind="document",
            resource=options.get("source_uri", path.as_uri()),
            blocks=blocks,
            equipment_ids=options.get("equipment_ids", ()),
            revision=options.get("revision"),
            warnings=parse_warnings,
            concept_type=options.get("type"),
            synthesize=options.get("synthesize", True),
        ))
    return sources, warnings


def _discover_files(root: Path) -> tuple[dict[str, Path], list[str]]:
    files = {}
    warnings = []
    for directory, subdirectories, filenames in os.walk(root, onerror=_raise_walk_error):
        folder = Path(directory)
        subfolders = []
        for name in sorted(subdirectories):
            path = folder / name
            if path.is_symlink():
                warnings.append(f"Skipped symbolic link: {path.relative_to(root).as_posix()}")
            else:
                subfolders.append(name)
        subdirectories[:] = subfolders
        for name in sorted(filenames):
            path = folder / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                warnings.append(f"Skipped symbolic link: {relative}")
            elif not path.is_file() or not path.resolve().is_relative_to(root):
                warnings.append(f"Skipped input outside the regular document files: {relative}")
            else:
                files[relative] = path
    return files, warnings


def _raise_walk_error(error: OSError) -> None:
    raise error


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _validate_file_options(mapping: object) -> dict[str, dict]:
    if not isinstance(mapping, dict):
        raise ValueError("documents.files must be a mapping of relative paths to options")
    result = {}
    for key, options in mapping.items():
        _required_text(key, "documents.files path")
        relative = PurePosixPath(key)
        if relative.is_absolute() or ".." in relative.parts or relative.as_posix() != key or key == ".":
            raise ValueError(f"Expected a relative POSIX document path: {key}")
        if not isinstance(options, dict) or options.keys() - _FILE_OPTIONS:
            raise ValueError(f"Invalid documents.files options for {key}")
        checked = {}
        for field in ("title", "type", "revision", "source_uri"):
            if field in options:
                checked[field] = _required_text(options[field], f"documents.files[{key}].{field}")
        if "source_uri" in checked and not urlsplit(checked["source_uri"]).scheme:
            raise ValueError(f"Expected an absolute source URI for {key}")
        if "equipment_ids" in options:
            if not isinstance(options["equipment_ids"], list):
                raise ValueError(f"Expected a list of equipment IDs for {key}")
            checked["equipment_ids"] = tuple(dict.fromkeys(
                _required_text(value, f"equipment ID for {key}") for value in options["equipment_ids"]
            ))
        if "ocr" in options:
            if type(options["ocr"]) is not bool:
                raise ValueError(f"Expected a boolean ocr option for {key}")
            if options["ocr"] and relative.suffix.lower() not in _OCR_FORMATS:
                raise ValueError(f"OCR is only supported for HWP, HWPX, or PDF: {key}")
            checked["ocr"] = options["ocr"]
        if "synthesize" in options:
            if type(options["synthesize"]) is not bool:
                raise ValueError(f"Expected a boolean synthesize option for {key}")
            checked["synthesize"] = options["synthesize"]
        result[key] = checked
    return result


def _read_text(path: Path, relative: str) -> tuple[tuple[Block, ...], tuple[str, ...]]:
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError(f"Document must be UTF-8: {relative}") from error
    if not content.strip() or "\x00" in content:
        raise ValueError(f"Document is empty or not text: {relative}")
    line_count = len(content.splitlines())
    locator = "line 1" if line_count == 1 else f"lines 1-{line_count}"
    return (Block(locator, content),), ()


def _prepare_cache(path: Path, root: Path, warnings: list[str]) -> Path | None:
    path = Path(os.path.abspath(path))
    try:
        if path.resolve() != path or path.is_relative_to(root) or root.is_relative_to(path):
            warnings.append(f"Skipped unsafe document extraction cache: {path}")
            return None
        path.mkdir(parents=True, exist_ok=True)
    except (OSError, RuntimeError) as error:
        warnings.append(f"Could not open document extraction cache {path}: {error}")
        return None
    return path


def _file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _parser_identity(executable: str, timeout: int | float) -> tuple[str, str, str] | None:
    found = shutil.which(executable)
    if not found:
        return None
    try:
        path = Path(found).resolve()
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, encoding="utf-8",
            timeout=min(timeout, 10), check=False,
        )
        version = result.stdout.strip()
        if result.returncode == 0 and version:
            return str(path), version, _file_hash(path)
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
        return None
    return None


def _extract_document(
    path: Path, relative: str, *, executable: str, timeout: int | float, ocr: bool,
    cache: Path | None, parser: tuple[str, str, str] | None,
) -> _Extraction:
    if cache is None or parser is None:
        return _read_kordoc(path, relative, executable=executable, timeout=timeout, ocr=ocr)

    digest = _file_hash(path)
    identity = (_CACHE_FORMAT, relative, digest, path.suffix.lower(), ocr, parser)
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()
    entry = cache / f"{key}.json"
    cached = _read_cache(entry, key)
    if cached is not None:
        return cached

    extraction = _read_kordoc(path, relative, executable=parser[0], timeout=timeout, ocr=ocr)
    if digest != _file_hash(path):
        raise ValueError(f"Document changed during extraction; retry the build: {relative}")
    _write_cache(entry, key, extraction)
    return extraction


def _read_cache(path: Path, key: str) -> _Extraction | None:
    try:
        if path.is_symlink() or not path.is_file() or path.parent.resolve() != path.parent:
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return None
            payload = json.load(handle)
    except (OSError, RuntimeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("key") != key:
        return None
    records = payload.get("blocks")
    warnings = payload.get("warnings")
    if (
        not isinstance(records, list) or not records
        or not isinstance(warnings, list) or not all(isinstance(warning, str) for warning in warnings)
    ):
        return None
    blocks = []
    for record in records:
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("locator"), str)
            or not record["locator"].strip()
            or not _has_text(record.get("text"))
        ):
            return None
        blocks.append(Block(record["locator"], record["text"]))
    return tuple(blocks), tuple(warnings)


def _write_cache(path: Path, key: str, extraction: _Extraction) -> None:
    blocks, warnings = extraction
    payload = {
        "key": key,
        "blocks": [{"locator": block.locator, "text": block.text} for block in blocks],
        "warnings": warnings,
    }
    temporary = None
    try:
        if path.parent.resolve() != path.parent:
            return
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".extraction-", delete=False
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=True)
        os.replace(temporary, path)
        temporary = None
    except (OSError, RuntimeError):
        return
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _read_kordoc(
    path: Path, relative: str, *, executable: str, timeout: int | float, ocr: bool
) -> tuple[tuple[Block, ...], tuple[str, ...]]:
    command = [executable, str(path), "--format", "json", "--no-images"]
    if ocr:
        command.append("--ocr")
    try:
        completed = subprocess.run(
            command, capture_output=True, encoding="utf-8", timeout=timeout, check=False
        )
    except FileNotFoundError as error:
        raise ValueError(f"kordoc executable not found ({executable}) while reading {relative}") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"kordoc timed out after {timeout} seconds reading {relative}") from error
    except UnicodeDecodeError as error:
        raise ValueError(f"kordoc produced non-UTF-8 output for {relative}") from error

    try:
        parsed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        detail = completed.stderr.strip() or "expected JSON on stdout"
        raise ValueError(f"kordoc failed for {relative}: {detail}") from error
    if not isinstance(parsed, dict):
        raise ValueError(f"kordoc returned an invalid result for {relative}")
    if completed.returncode or parsed.get("success") is not True:
        code = parsed.get("code", "PARSE_ERROR")
        detail = parsed.get("error") or completed.stderr.strip() or f"exit {completed.returncode}"
        if code == "IMAGE_BASED_PDF" and not ocr:
            detail = f"{detail}. Set ocr: true for this file."
        raise ValueError(f"kordoc failed for {relative} ({code}): {detail}")

    blocks, section_fallback = _kordoc_blocks(parsed, path.suffix.lower(), relative)
    if not blocks:
        hint = " Set ocr: true for this file." if path.suffix.lower() in _OCR_FORMATS and not ocr else ""
        raise ValueError(f"kordoc extracted no text from {relative}.{hint}")
    warnings = []
    for warning in parsed.get("warnings") or []:
        if isinstance(warning, str):
            warnings.append(warning)
        elif isinstance(warning, dict):
            warnings.append(": ".join(str(warning[key]) for key in ("code", "message") if warning.get(key)) or str(warning))
    if section_fallback:
        warnings.append("Kordoc provided approximate section boundaries rather than physical page numbers.")
    return blocks, tuple(warnings)


def _kordoc_blocks(parsed: dict, suffix: str, relative: str) -> tuple[tuple[Block, ...], bool]:
    metadata = parsed.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError(f"kordoc returned invalid metadata for {relative}")
    mode = metadata.get("pageMode")
    section_fallback = mode == "section" or (suffix in {".hwp", ".hwpx"} and mode != "layout")
    if suffix == ".xlsx":
        locator = "sheet"
    elif suffix == ".pptx":
        locator = "slide"
    elif section_fallback:
        locator = "section"
    else:
        locator = "page"

    blocks = []
    pages = parsed.get("pages") or []
    if not isinstance(pages, list):
        raise ValueError(f"kordoc returned invalid pages for {relative}")
    for page in pages:
        if (
            not isinstance(page, dict)
            or type(page.get("pageNumber")) is not int
            or page["pageNumber"] < 1
            or not isinstance(page.get("markdown"), str)
        ):
            raise ValueError(f"kordoc returned an invalid page record for {relative}")
        if _has_text(page["markdown"]):
            blocks.append(Block(f"{locator} {page['pageNumber']}", page["markdown"]))
    if blocks:
        return tuple(blocks), section_fallback

    # Formats without page boundaries still expose the complete Markdown text.
    if _has_text(parsed.get("markdown")):
        return (Block("document body", parsed["markdown"]),), False
    return (), False


def _has_text(value: object) -> bool:
    return isinstance(value, str) and bool(_IMAGE_LINE.sub("", value).strip())
