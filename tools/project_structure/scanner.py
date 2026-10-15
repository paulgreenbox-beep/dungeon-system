from __future__ import annotations

from pathlib import Path

from .model import Issue, ModuleSnapshot, ProjectSnapshot
from .parser import parse_glif, parse_sklt


RESERVED_TOP_LEVEL = frozenset({
    ".git",
    ".codex",
    ".agents",
    "docs",
    "evidence",
    "libs",
    "snapshots",
    "tools",
    "tests",
    "__pycache__",
    "target",
})


def _contains_structural_files(path: Path) -> bool:
    return any(
        candidate.is_file() and candidate.suffix.lower() in {".sklt", ".glif"}
        for candidate in path.rglob("*")
    )


def _module_paths(root: Path, excluded=None) -> list[Path]:
    direct_files = [candidate for candidate in root.iterdir() if candidate.is_file() and candidate.suffix.lower() in {".sklt", ".glif"}]
    paths = [root] if direct_files else []
    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if excluded is not None and excluded(child):
            continue
        if not child.is_dir() or child.name in RESERVED_TOP_LEVEL or child.name.startswith("."):
            continue
        if _contains_structural_files(child):
            paths.append(child)
    return paths


def _structural_files(module_path: Path, project_root: Path) -> tuple[list[Path], list[Path]]:
    sklts: list[Path] = []
    glifs: list[Path] = []
    for candidate in sorted(module_path.rglob("*"), key=lambda item: str(item).lower()):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(module_path)
        if module_path == project_root and relative.parts and relative.parts[0] in RESERVED_TOP_LEVEL:
            continue
        if any(part.startswith(".") for part in relative.parts[:-1]):
            continue
        if candidate.suffix.lower() == ".sklt":
            sklts.append(candidate)
        elif candidate.suffix.lower() == ".glif":
            glifs.append(candidate)
    return sklts, glifs


def scan_project(root: Path, *, excluded=None) -> ProjectSnapshot:
    root = root.resolve()
    snapshot = ProjectSnapshot(root=root)
    if not root.exists() or not root.is_dir():
        snapshot.issues.append(Issue("INVALID_PROJECT_ROOT", "project root must be an existing directory", root))
        return snapshot

    for module_path in _module_paths(root, excluded):
        name = "." if module_path == root else module_path.relative_to(root).as_posix()
        module = ModuleSnapshot(name=name, path=module_path)
        sklt_paths, glif_paths = _structural_files(module_path, root)
        for path in sklt_paths:
            if excluded is not None and excluded(path):
                continue
            record, issues = parse_sklt(path)
            module.sklts.append(record)
            snapshot.issues.extend(issues)
        for path in glif_paths:
            if excluded is not None and excluded(path):
                continue
            record, issues = parse_glif(path)
            module.glifs.append(record)
            snapshot.issues.extend(issues)
        snapshot.modules.append(module)
    return snapshot
