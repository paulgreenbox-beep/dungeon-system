"""Bounded, read-only SKLT discovery and declaration navigation.

Declared symbols are navigation identities, not an implicit canonical dialect
adapter. No responsibility, state, source link or dependency is synthesized.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from .model import SkltRecord, VALID_STATES
from .parser import parse_sklt
from .v03_manifest import DEFAULT_IGNORED, DEFAULT_RESERVED, ProjectManifest, is_ignored_relative
from .v03_sources import parse_source_refs


MAX_DIRECTORIES = 10_000
MAX_FILES = 100_000
MAX_STRUCTURAL_FILES = 10_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DECLARATIONS = 10_000
SKIP_DIRS = DEFAULT_IGNORED | DEFAULT_RESERVED | {
    "node_modules", ".venv", "venv", "build", "dist", "assets", "logs", "references",
}
SOURCE_SUFFIXES = {".rs", ".py", ".cs", ".c", ".cpp", ".h", ".hpp", ".js", ".ts", ".tsx", ".java", ".go"}
DECLARATION_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<kind>project|module|capability|command|action)\s+"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_.:-]*)\s*:\s*(?:#.*)?$"
)
FIELD_RE = re.compile(r"^([ \t]*)([A-Za-z_][A-Za-z0-9_.-]*)\s*:\s*(.*)$")


def issue(code: str, message: str, path: str = "", *, severity: str = "ERROR", **details: Any) -> dict:
    return {"code": code, "severity": severity, "message": message, "path": path, **details}


def is_link(path: Path) -> bool:
    """Also reject Windows junctions and other reparse points."""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def checked_root(value: str | os.PathLike) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("project root must be an absolute directory path")
    if ".." in path.parts:
        raise ValueError("project root must not contain traversal")
    for part in (path, *path.parents):
        if is_link(part):
            raise ValueError(f"project root traverses a symlink or reparse point: {part}")
    if not path.is_dir():
        raise ValueError("project root must be an existing directory")
    return path.resolve(strict=True)


def checked_path(root: Path, relative: str, *, output: bool = False) -> Path:
    """Resolve only project-relative paths, rejecting links even inside the root."""
    if not isinstance(relative, str) or not relative:
        raise ValueError("path must be a non-empty project-relative string")
    normal = relative.replace("\\", "/")
    if Path(normal).is_absolute() or PureWindowsPath(normal).drive or ":" in normal or ".." in normal.split("/"):
        raise ValueError(f"path is not project-relative: {relative}")
    path = root / normal
    path.resolve().relative_to(root)
    current = root
    for part in Path(normal).parts:
        current = current / part
        if os.path.lexists(current):
            if is_link(current):
                raise ValueError(f"path traverses a symlink or reparse point: {relative}")
            if output and current == path and current.is_file() and current.stat().st_nlink > 1:
                raise ValueError(f"output is a hard-linked file: {relative}")
    return path


def read_bounded(path: Path) -> bytes:
    with path.open("rb") as handle:
        content = handle.read(MAX_FILE_BYTES + 1)
    if len(content) > MAX_FILE_BYTES:
        raise ValueError(f"structural file exceeds {MAX_FILE_BYTES} bytes")
    return content


def discover(root: Path, manifest: ProjectManifest) -> dict:
    """Honor explicit module selection; otherwise use v0.3 physical groups.

    Files are visited once (including when there are root-level SKLTs). Limits
    report partial discovery instead of claiming a complete count.
    """
    result: dict = {"sklt": [], "glif": [], "source_directories": [], "directories": [], "issues": [], "complete": True}
    sources: set[str] = set()
    directories: set[str] = set()
    seen: set[str] = set()
    visited_dirs = 0
    visited_files = 0
    structural_files = 0

    def walk(base: Path, module_id: str | None, module_path: str | None, recursive: bool = True) -> None:
        nonlocal visited_dirs, visited_files, structural_files
        pending = [base]
        while pending:
            current = pending.pop()
            visited_dirs += 1
            if visited_dirs > MAX_DIRECTORIES:
                raise ValueError("directory limit reached")
            relative_dir = current.relative_to(root).as_posix()
            directories.add(relative_dir)
            with os.scandir(current) as handle:
                entries = sorted(handle, key=lambda entry: (entry.name.casefold(), entry.name))
            for entry in entries:
                visited_files += 1
                if visited_files > MAX_FILES:
                    raise ValueError("directory entry limit reached")
                candidate = Path(entry.path)
                relative = candidate.relative_to(root).as_posix()
                if is_ignored_relative(manifest, relative):
                    continue
                if entry.is_dir(follow_symlinks=False) and (
                    entry.name.startswith(".") or (not manifest.exists and entry.name.lower().startswith("target-"))
                    or (not manifest.exists and entry.name.lower() in SKIP_DIRS)
                    or (current == root and entry.name in DEFAULT_RESERVED)
                ):
                    continue
                if is_link(candidate):
                    result["issues"].append(issue("UNSAFE_PATH", "discovery does not follow symlinks or reparse points", relative))
                    result["complete"] = False
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if recursive:
                        pending.append(candidate)
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
                if candidate.suffix.lower() in SOURCE_SUFFIXES:
                    sources.add(candidate.parent.relative_to(root).as_posix())
                suffix = candidate.suffix.lower()
                if suffix not in {".sklt", ".glif"} or relative in seen:
                    continue
                structural_files += 1
                if structural_files > MAX_STRUCTURAL_FILES:
                    raise ValueError("structural file limit reached")
                seen.add(relative)
                physical_group = relative.split("/", 1)[0] if "/" in relative else "."
                result["sklt" if suffix == ".sklt" else "glif"].append({
                    "path": relative,
                    "module": module_id if module_id is not None else physical_group,
                    "module_path": module_path if module_path is not None else physical_group,
                })

    try:
        if manifest.modules:
            for module in sorted(manifest.modules, key=lambda item: item.module_id):
                walk(checked_path(root, module.path), module.module_id, module.path, module.recursive)
        else:
            walk(root, None, None)
    except (OSError, ValueError) as exc:
        result["complete"] = False
        result["issues"].append(issue("DISCOVERY_INCOMPLETE", str(exc)))
    for key in ("sklt", "glif"):
        result[key].sort(key=lambda entry: entry["path"])
    result["source_directories"] = sorted(sources)
    result["directories"] = sorted(directories)
    return result


def _indent(line: str) -> int:
    return len(line.expandtabs(4)) - len(line.expandtabs(4).lstrip())


def _direct_fields(lines: list[str], start: int, end: int) -> dict[str, list[str]]:
    """Read fields of this declaration only, excluding nested declarations."""
    body = [(index, line) for index, line in enumerate(lines[start + 1:end], start + 1)
            if line.strip() and not line.lstrip().startswith("#")]
    if not body:
        return {}
    level = min(_indent(line) for _, line in body)
    fields: dict[str, list[str]] = {}
    key: str | None = None
    for _, line in body:
        if _indent(line) == level:
            match = FIELD_RE.match(line)
            key = match.group(2) if match and not DECLARATION_RE.match(line) else None
            if key:
                # A repeated identity is kept for explicit conflict diagnosis.
                fields.setdefault(key, []).append(match.group(3).strip())
        elif key:
            fields[key].append(line.strip())
    return fields


def _values(lines: list[str]) -> list[str]:
    return [line.removeprefix("-").strip().strip('"\'') for line in lines if line.strip()]


def parse_declarations(path: Path, text: str, relative: str, physical_module: str) -> tuple[list[dict], list[dict]]:
    lines = text.splitlines()
    records: list[dict] = []
    issues: list[dict] = []
    scopes: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = DECLARATION_RE.match(line)
        if not match:
            continue
        if len(records) >= MAX_DECLARATIONS:
            issues.append(issue("DECLARATION_LIMIT", "too many declarations in one SKLT; split it before adoption", relative))
            break
        indent = _indent(line)
        while scopes and scopes[-1][0] >= indent:
            scopes.pop()
        kind, name = match.group("kind", "name")
        symbol = f"{kind}:{name}"
        namespace = "/".join(scope[2] for scope in scopes)
        declared_id = "declared:" + (namespace + "/" if namespace else "") + symbol
        end = index + 1
        while end < len(lines):
            if lines[end].strip() and not lines[end].lstrip().startswith("#") and _indent(lines[end]) <= indent:
                break
            end += 1
        fields = _direct_fields(lines, index, end)
        explicit = _values(fields.get("object_id", []))
        if len(explicit) > 1:
            issues.append(issue("DUPLICATE_SKLT_FIELD", "declaration has multiple object_id values", relative, line=index + 1, values=explicit))
        object_id = explicit[0] if explicit else declared_id
        module = next((scope[1] for scope in reversed(scopes) if scope[2].startswith("module:")), None)
        if kind == "module":
            module = object_id
        state_values = _values(fields.get("state", []))
        state_value = state_values[0].upper() if len(state_values) == 1 else None
        if state_values and state_value not in VALID_STATES:
            issues.append(issue("INVALID_STATE", "only an explicitly declared SKLT or RARE is a structural state", relative, line=index + 1))
            state_value = None
        source_text = " ".join(fields.get("source", []) or fields.get("source_refs", []))
        source_refs, source_issues = parse_source_refs(SkltRecord(path, {"source": source_text}))
        bare_sources = _values(fields.get("source", []) or fields.get("source_refs", []))
        if source_issues and bare_sources and all(not re.search(r"\s|:\s", value) for value in bare_sources):
            # This dialect also explicitly declares bare paths, e.g. source: src/draw.rs.
            # Keep the paths literally; do not infer a symbol or semantic correspondence.
            source_values = [{"path": value} for value in bare_sources]
            source_issues = []
        else:
            source_values = [ref.as_dict() for ref in source_refs]
        issues.extend(issue(item.code, item.message, relative) for item in source_issues)
        dependencies = []
        for key in ("depends_on", "dependencies", "requires", "use"):
            for value in _values(fields.get(key, [])):
                dependencies.append({"type": "declared", "id": value})
        records.append({
            "object_id": object_id, "identity_origin": "OBJECT_ID" if explicit else "DECLARED_SYMBOL",
            "module": module or physical_module, "sklt": relative, "state": state_value,
            "dependencies": dependencies, "source": source_values, "glif": [],
            "dialect": "DECLARATIONS", "kind": kind, "symbol": name,
            "line_start": index + 1, "line_end": end,
            "span": {"start": index + 1, "end": end},
        })
        if kind in {"project", "module"}:
            scopes.append((indent, object_id, symbol))
    if not records:
        issues.append(issue("UNRECOGNIZED_SKLT_DIALECT", "no canonical object_id or supported named declaration; inspect this SKLT", relative))
    return records, issues


def read_sklt(root: Path, entry: dict) -> tuple[list[dict], SkltRecord | None, list[dict]]:
    relative = entry["path"]
    path = checked_path(root, relative)
    try:
        text = read_bounded(path).decode("utf-8-sig")
    except (OSError, UnicodeError, ValueError) as exc:
        return [], None, [issue("UNREADABLE_SKLT", str(exc), relative)]
    if re.search(r"^object_id\s*:", text, re.MULTILINE):
        canonical, parse_issues = parse_sklt(path)
        issues = [issue(item.code, item.message, relative, **({"line": item.line} if item.line else {})) for item in parse_issues]
        sources, source_issues = parse_source_refs(canonical)
        issues.extend(issue(item.code, item.message, relative) for item in source_issues)
        if not canonical.object_id:
            return [], canonical, issues
        return [{
            "object_id": canonical.object_id, "identity_origin": "OBJECT_ID", "module": entry["module"],
            "sklt": relative, "state": canonical.state, "dependencies": [],
            "source": [ref.as_dict() for ref in sources], "glif": [], "dialect": "CANONICAL",
            "git_scope": canonical.git_scope, "level": canonical.level,
            "line_start": 1, "line_end": max(1, len(text.splitlines())),
        }], canonical, issues
    records, issues = parse_declarations(path, text, relative, entry["module"])
    return records, None, issues
