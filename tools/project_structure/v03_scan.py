from __future__ import annotations

from pathlib import Path

from .model import Issue, ModuleSnapshot, ProjectSnapshot
from .parser import parse_glif, parse_sklt
from .scanner import _structural_files, scan_project
from .v03_manifest import ExplicitModule, ProjectManifest, is_ignored_relative, is_reserved_relative, load_manifest, relative_path


def _parse_module(root: Path, module_id: str, module_path: Path, issues: list[Issue], recursive: bool = True, manifest=None) -> ModuleSnapshot:
    module = ModuleSnapshot(name=module_id, path=module_path)
    sklt_paths, glif_paths = _structural_files(module_path, root)
    if manifest is not None:
        sklt_paths = [path for path in sklt_paths if not is_ignored_relative(manifest, path.relative_to(root).as_posix())]
        glif_paths = [path for path in glif_paths if not is_ignored_relative(manifest, path.relative_to(root).as_posix())]
    # New adoption can explicitly select direct files. Legacy explicit modules
    # retain their recursive ownership, including a sole module at '.'.
    if not recursive:
        sklt_paths = [path for path in sklt_paths if path.parent == module_path]
        glif_paths = [path for path in glif_paths if path.parent == module_path]
    for path in sklt_paths:
        record, parse_issues = parse_sklt(path)
        module.sklts.append(record)
        issues.extend(parse_issues)
    for path in glif_paths:
        record, parse_issues = parse_glif(path)
        module.glifs.append(record)
        issues.extend(parse_issues)
    return module


def _filter_snapshot(snapshot: ProjectSnapshot, manifest: ProjectManifest) -> ProjectSnapshot:
    filtered_modules: list[ModuleSnapshot] = []
    for module in snapshot.modules:
        if module.path == manifest.root:
            module.sklts = [record for record in module.sklts if record.path.parent == manifest.root]
            module.glifs = [record for record in module.glifs if record.path.parent == manifest.root]
        module_relative = relative_path(manifest.root, module.path) or ""
        if module_relative and (is_reserved_relative(manifest, module_relative) or is_ignored_relative(manifest, module_relative)):
            continue
        module.sklts = [record for record in module.sklts if (relative_path(manifest.root, record.path) or "") and not is_ignored_relative(manifest, relative_path(manifest.root, record.path) or "")]
        module.glifs = [record for record in module.glifs if (relative_path(manifest.root, record.path) or "") and not is_ignored_relative(manifest, relative_path(manifest.root, record.path) or "")]
        filtered_modules.append(module)
    snapshot.modules = filtered_modules
    return snapshot


def scan_project_v03(root: Path) -> tuple[ProjectSnapshot, ProjectManifest, list[Issue]]:
    root = root.resolve()
    manifest, manifest_issues = load_manifest(root)
    issues = list(manifest_issues)
    if not manifest.modules:
        def excluded(path):
            relative = path.relative_to(root).as_posix()
            return is_reserved_relative(manifest, relative) or is_ignored_relative(manifest, relative)
        snapshot = _filter_snapshot(scan_project(root, excluded=excluded), manifest)
        snapshot.issues.extend(issues)
        return snapshot, manifest, issues

    snapshot = ProjectSnapshot(root=root)
    for explicit in manifest.modules:
        module_path = (root / explicit.path).resolve()
        relative = relative_path(root, module_path)
        if relative is None:
            issues.append(Issue("INVALID_PROJECT_MANIFEST", f"module path escapes project root: {explicit.path}", manifest.path))
            continue
        if is_reserved_relative(manifest, relative) and not explicit.allow_reserved:
            issues.append(Issue("RESERVED_MODULE_PATH", f"module '{explicit.module_id}' uses reserved path without allow_reserved", manifest.path))
            continue
        if is_ignored_relative(manifest, relative):
            issues.append(Issue("IGNORED_MODULE_PATH", f"module '{explicit.module_id}' is ignored by manifest", manifest.path))
            continue
        if not module_path.exists() or not module_path.is_dir():
            issues.append(Issue("MISSING_MODULE_PATH", f"module path does not exist: {explicit.path}", manifest.path))
            continue
        snapshot.modules.append(_parse_module(root, explicit.module_id, module_path, issues, explicit.recursive, manifest))
    snapshot.issues.extend(issues)
    return snapshot, manifest, issues
