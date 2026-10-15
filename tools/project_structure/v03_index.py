from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .libraries import LibraryRegistry
from .model import Issue, ProjectSnapshot
from .v03_dependencies import library_refs, normalize_dependencies
from .v03_hash import structural_hash
from .v03_metadata import atomic_write_json, load_metadata, mark_artifact
from .v03_sources import parse_source_refs
from .v03_toolkit import parse_toolkit_refs
from .v03_manifest import ProjectManifest
from .v041_paths import publish_files, json_bytes
from .v03_metadata import metadata_updates, metadata_path


INDEX_SCHEMA = "dungeon-object-index/v1"


def index_path(root: Path) -> Path:
    return root / ".project" / "objects.json"


def build_object_index(snapshot: ProjectSnapshot, manifest: ProjectManifest, libraries: LibraryRegistry) -> tuple[dict[str, Any], list[Issue]]:
    input_hash = structural_hash(snapshot, manifest)
    dependencies, dependency_issues = normalize_dependencies(snapshot, set(libraries.by_id))
    objects: dict[str, Any] = {}
    issues = list(dependency_issues)
    module_for_record = {id(record): module for module in snapshot.modules for record in module.sklts}
    for record in snapshot.sklts:
        if not record.object_id:
            continue
        module = module_for_record[id(record)]
        sources, source_issues = parse_source_refs(record)
        issues.extend(source_issues)
        execution, execution_issues = parse_toolkit_refs(record)
        issues.extend(execution_issues)
        glif_paths = [glif.path.relative_to(snapshot.root).as_posix() for glif in module.glifs if glif.object_id == record.object_id]
        objects[record.object_id] = {
            "module": module.name,
            "sklt": record.path.relative_to(snapshot.root).as_posix(),
            "state": record.state,
            "level": record.level,
            "storage_state": record.storage_state,
            "retention": record.retention,
            "flags": record.flags,
            "git_scope": record.git_scope,
            "isolation": record.isolation,
            "glif": sorted(glif_paths),
            "source": [source.as_dict() for source in sources],
            "libraries": sorted(library_refs(record)),
            "dependencies": [dependency.as_dict() for dependency in dependencies.get(record.object_id, [])],
            "execution": [ref.as_dict() for ref in execution],
        }
    return {
        "schema": INDEX_SCHEMA,
        "schema_version": 1,
        "input_hash": input_hash,
        "objects": {key: objects[key] for key in sorted(objects)},
    }, issues


def write_object_index(root: Path, data: dict[str, Any]) -> None:
    atomic_write_json(index_path(root), data, root=root)


def load_object_index(root: Path) -> tuple[dict[str, Any] | None, list[Issue]]:
    path = index_path(root)
    if not path.exists():
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [Issue("INVALID_OBJECT_INDEX", f"cannot parse object index: {exc}", path)]
    if not isinstance(data, dict) or data.get("schema") != INDEX_SCHEMA or not isinstance(data.get("objects"), dict):
        return None, [Issue("INVALID_OBJECT_INDEX", "object index schema is invalid", path)]
    return data, []


def rebuild_object_index(root: Path, snapshot: ProjectSnapshot, manifest: ProjectManifest, libraries: LibraryRegistry) -> tuple[dict[str, Any] | None, list[Issue]]:
    data, issues = build_object_index(snapshot, manifest, libraries)
    blocking = {"INVALID_SOURCE_REF", "UNKNOWN_OBJECT_DEPENDENCY", "SELF_DEPENDENCY", "INVALID_TOOLKIT_REF", "UNKNOWN_TOOLKIT_CONTAINER"}
    if issues and any(issue.code in blocking for issue in issues):
        return None, issues
    metadata = metadata_updates(root, {"artifacts": {"objects_index": {"input_hash": data["input_hash"], "path": ".project/objects.json"}}})
    publish_files(root, {index_path(root): json_bytes(data), metadata_path(root): json_bytes(metadata)})
    return data, issues


def object_index_state(root: Path, current_hash: str) -> str:
    path = index_path(root)
    if not path.exists():
        return "MISSING"
    data, issues = load_object_index(root)
    if issues or data is None:
        return "INVALID"
    if data.get("input_hash") != current_hash:
        return "STALE"
    metadata, metadata_issues = load_metadata(root)
    artifact = metadata.get("artifacts", {}).get("objects_index", {}) if isinstance(metadata, dict) else {}
    if metadata_issues or artifact.get("input_hash") != current_hash:
        return "STALE"
    return "CURRENT"
