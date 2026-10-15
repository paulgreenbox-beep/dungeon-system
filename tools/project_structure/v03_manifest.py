from __future__ import annotations

import json
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .model import DEFAULT_GIT_SCOPE, GIT_SCOPE_MODES, Issue


SUPPORTED_MANIFEST_SCHEMA = 1
DEFAULT_RESERVED = frozenset({"docs", "evidence", "libs", "snapshots", "tools", "tests", ".project"})
DEFAULT_IGNORED = frozenset({".git", "target", ".codex", ".agents", "__pycache__"})


@dataclass(frozen=True)
class ExplicitModule:
    module_id: str
    path: str
    allow_reserved: bool = False
    recursive: bool = True


@dataclass(frozen=True)
class GitScope:
    scope_id: str
    mode: str
    repository: str
    root: str = "."
    default_branch: str | None = None
    remote: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "id": self.scope_id,
            "mode": self.mode,
            "repository": self.repository,
            "root": self.root,
            "default_branch": self.default_branch,
            "remote": self.remote,
        }


def default_git_scope() -> GitScope:
    return GitScope(scope_id=DEFAULT_GIT_SCOPE, mode="MONOREPO", repository=".")


@dataclass
class ProjectManifest:
    root: Path
    schema_version: int = SUPPORTED_MANIFEST_SCHEMA
    project_id: str = "dungeon.project"
    modules: list[ExplicitModule] = field(default_factory=list)
    reserved_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_RESERVED))
    ignore: list[str] = field(default_factory=list)
    docs: dict[str, str] = field(default_factory=dict)
    evidence_store: dict[str, str] | None = None
    git_scopes: list[GitScope] = field(default_factory=list)
    exists: bool = False

    @property
    def path(self) -> Path:
        return self.root / ".project" / "project.json"

    def known_git_scopes(self) -> set[str]:
        return {scope.scope_id for scope in self.git_scopes} or {DEFAULT_GIT_SCOPE}


def load_manifest(root: Path) -> tuple[ProjectManifest, list[Issue]]:
    root = root.resolve()
    manifest = ProjectManifest(root=root, ignore=sorted(DEFAULT_IGNORED))
    path = manifest.path
    if not path.exists():
        return manifest, []
    manifest.exists = True
    issues: list[Issue] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return manifest, [Issue("INVALID_PROJECT_MANIFEST", f"cannot parse manifest: {exc}", path)]
    if not isinstance(data, dict):
        return manifest, [Issue("INVALID_PROJECT_MANIFEST", "manifest must be a JSON object", path)]

    schema_version = data.get("schema_version", 1)
    if not isinstance(schema_version, int) or schema_version < 1:
        issues.append(Issue("INVALID_PROJECT_MANIFEST", "schema_version must be a positive integer", path))
    elif schema_version > SUPPORTED_MANIFEST_SCHEMA:
        issues.append(Issue("UNSUPPORTED_SCHEMA_VERSION", f"project manifest schema {schema_version} is not supported", path))
    else:
        manifest.schema_version = schema_version

    project_id = data.get("project_id", manifest.project_id)
    if not isinstance(project_id, str) or not project_id.strip():
        issues.append(Issue("INVALID_PROJECT_MANIFEST", "project_id must be a non-empty string", path))
    else:
        manifest.project_id = project_id.strip()

    reserved = data.get("reserved_dirs", sorted(DEFAULT_RESERVED))
    if not isinstance(reserved, list) or any(not isinstance(item, str) or not item for item in reserved):
        issues.append(Issue("INVALID_PROJECT_MANIFEST", "reserved_dirs must be an array of strings", path))
    else:
        manifest.reserved_dirs = set(reserved)

    ignored = data.get("ignore", [])
    if not isinstance(ignored, list) or any(not isinstance(item, str) or not item for item in ignored):
        issues.append(Issue("INVALID_PROJECT_MANIFEST", "ignore must be an array of strings", path))
    else:
        manifest.ignore = sorted(set(DEFAULT_IGNORED) | set(ignored))

    docs = data.get("docs", {})
    if not isinstance(docs, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in docs.items()):
        issues.append(Issue("INVALID_PROJECT_MANIFEST", "docs must be an object of string paths", path))
    else:
        manifest.docs = dict(docs)

    evidence_store = data.get("evidence_store")
    if evidence_store is not None:
        if (
            not isinstance(evidence_store, dict)
            or evidence_store.get("type") != "local-cas"
            or not isinstance(evidence_store.get("root"), str)
            or not evidence_store.get("root", "").strip()
        ):
            issues.append(Issue("INVALID_PROJECT_MANIFEST", "evidence_store must contain type 'local-cas' and a non-empty root path", path))
        else:
            manifest.evidence_store = {"type": "local-cas", "root": evidence_store["root"].strip()}

    git_scopes = data.get("git_scopes", [])
    if not isinstance(git_scopes, list):
        issues.append(Issue("INVALID_PROJECT_MANIFEST", "git_scopes must be an array", path))
    else:
        seen_scopes: set[str] = set()
        for item in git_scopes:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("mode"), str) or not isinstance(item.get("repository"), str):
                issues.append(Issue("INVALID_PROJECT_MANIFEST", "each git scope needs string id, mode and repository", path))
                continue
            scope_id = item["id"].strip()
            mode = item["mode"].strip().upper()
            repository = item["repository"].replace("\\", "/").strip()
            if not scope_id or not repository:
                issues.append(Issue("INVALID_PROJECT_MANIFEST", "git scope id and repository must not be empty", path))
                continue
            if mode not in GIT_SCOPE_MODES:
                issues.append(Issue("INVALID_GIT_SCOPE_MODE", f"git scope '{scope_id}' mode '{mode}' is not one of {', '.join(sorted(GIT_SCOPE_MODES))}", path))
                continue
            if scope_id in seen_scopes:
                issues.append(Issue("DUPLICATE_GIT_SCOPE", f"git scope id is duplicated: {scope_id}", path))
                continue
            seen_scopes.add(scope_id)
            root = item.get("root", ".")
            manifest.git_scopes.append(GitScope(
                scope_id=scope_id,
                mode=mode,
                repository=repository,
                root=str(root) if isinstance(root, str) and root.strip() else ".",
                default_branch=item.get("default_branch") if isinstance(item.get("default_branch"), str) else None,
                remote=item.get("remote") if isinstance(item.get("remote"), str) else None,
            ))

    modules = data.get("modules", [])
    if not isinstance(modules, list):
        issues.append(Issue("INVALID_PROJECT_MANIFEST", "modules must be an array", path))
    else:
        seen_ids: set[str] = set()
        seen_paths: set[str] = set()
        for item in modules:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("path"), str):
                issues.append(Issue("INVALID_PROJECT_MANIFEST", "each module needs string id and path", path))
                continue
            module_id = item["id"].strip()
            module_path = item["path"].replace("\\", "/").strip("/")
            if not module_id or not module_path:
                issues.append(Issue("INVALID_PROJECT_MANIFEST", "module id and path must not be empty", path))
                continue
            if module_id in seen_ids or module_path in seen_paths:
                issues.append(Issue("DUPLICATE_MODULE", f"module id or path is duplicated: {module_id}", path))
                continue
            seen_ids.add(module_id)
            seen_paths.add(module_path)
            recursive = item.get("recursive", True)
            if not isinstance(recursive, bool):
                issues.append(Issue("INVALID_PROJECT_MANIFEST", "module recursive must be a boolean", path))
                recursive = True
            manifest.modules.append(ExplicitModule(module_id, module_path, bool(item.get("allow_reserved", False)), recursive))
    return manifest, issues


def relative_path(root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return None


def is_reserved_relative(manifest: ProjectManifest, relative: str) -> bool:
    first = relative.replace("\\", "/").split("/", 1)[0]
    return first in manifest.reserved_dirs


def is_ignored_relative(manifest: ProjectManifest, relative: str) -> bool:
    normal = relative.replace("\\", "/").strip("/")
    parts = normal.split("/") if normal else []
    for pattern in manifest.ignore:
        pattern = pattern.replace("\\", "/").strip("/")
        if not pattern:
            continue
        if normal == pattern or normal.startswith(pattern + "/") or any(part == pattern for part in parts):
            return True
        if fnmatch(normal, pattern) or any(fnmatch(part, pattern) for part in parts):
            return True
    return False


def write_default_manifest(root: Path, project_id: str = "dungeon.project") -> Path:
    manifest = ProjectManifest(root=root.resolve(), project_id=project_id, ignore=sorted(DEFAULT_IGNORED))
    manifest.path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "schema_version": 1,
        "project_id": project_id,
        "modules": [],
        "git_scopes": [{"id": DEFAULT_GIT_SCOPE, "mode": "MONOREPO", "repository": ".", "root": "."}],
        "reserved_dirs": sorted(DEFAULT_RESERVED),
        "ignore": [],
        "docs": {
            "project": "docs/project.md",
            "map": "docs/map.md",
            "architecture": "docs/architecture.md",
            "cronica": "docs/cronica",
        },
        "evidence_store": {"type": "local-cas", "root": "../DungeonStore"},
    }
    manifest.path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return manifest.path
