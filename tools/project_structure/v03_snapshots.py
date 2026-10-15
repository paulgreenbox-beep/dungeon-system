from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .libraries import LibraryRegistry
from .model import Issue, ProjectSnapshot
from .snapshots import create_snapshot
from .v03_hash import structural_hash
from .v03_index import build_object_index


def create_snapshot_v03(snapshot: ProjectSnapshot, snapshot_id: str, manifest, libraries: LibraryRegistry, evidence_ids: list[str] | None = None) -> list[Issue]:
    index_data, index_issues = build_object_index(snapshot, manifest, libraries)
    blocking = {"INVALID_SOURCE_REF", "UNKNOWN_OBJECT_DEPENDENCY", "SELF_DEPENDENCY", "INVALID_TOOLKIT_REF", "UNKNOWN_TOOLKIT_CONTAINER"}
    if any(issue.code in blocking for issue in index_issues):
        return index_issues
    extra = {"snapshot_id": snapshot_id, "name": snapshot_id,
             "structural_hash": structural_hash(snapshot, manifest),
             "objects": index_data.get("objects", {}), "schema_version": 1,
             "evidence_ids": sorted(set(evidence_ids or []))}
    return create_snapshot(snapshot, snapshot_id, manifest_extra=extra)


def _read_manifest(root: Path, name: str) -> tuple[dict[str, Any] | None, list[Issue]]:
    path = root / "snapshots" / name / "manifest.json"
    if not path.exists():
        return None, [Issue("INVALID_SNAPSHOT_MANIFEST", f"snapshot does not exist: {name}", path)]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [Issue("INVALID_SNAPSHOT_MANIFEST", f"cannot parse snapshot: {exc}", path)]
    if not isinstance(data, dict):
        return None, [Issue("INVALID_SNAPSHOT_MANIFEST", "snapshot manifest must be an object", path)]
    return data, []


def snapshot_diff(root: Path, old_name: str, new_name: str) -> tuple[dict[str, Any], list[Issue]]:
    old, old_issues = _read_manifest(root, old_name)
    new, new_issues = _read_manifest(root, new_name)
    issues = old_issues + new_issues
    if old is None or new is None:
        return {}, issues
    old_objects = old.get("objects", {}) if isinstance(old.get("objects"), dict) else {}
    new_objects = new.get("objects", {}) if isinstance(new.get("objects"), dict) else {}
    added = sorted(set(new_objects) - set(old_objects))
    removed = sorted(set(old_objects) - set(new_objects))
    changed: list[dict[str, Any]] = []
    for object_id in sorted(set(old_objects) & set(new_objects)):
        before = old_objects[object_id]
        after = new_objects[object_id]
        fields = {}
        for field in ("module", "state", "glif", "source", "libraries", "dependencies", "execution"):
            if before.get(field) != after.get(field):
                fields[field] = {"before": before.get(field), "after": after.get(field)}
        if fields:
            changed.append({"object_id": object_id, "changes": fields})
    old_grade = old.get("grade", {})
    new_grade = new.get("grade", {})
    result = {
        "old": old_name,
        "new": new_name,
        "added": added,
        "removed": removed,
        "changed": changed,
        "grade": {
            "sklt": {"before": old_grade.get("sklt"), "after": new_grade.get("sklt"), "delta": (new_grade.get("sklt", 0) - old_grade.get("sklt", 0))},
            "rare": {"before": old_grade.get("rare"), "after": new_grade.get("rare"), "delta": (new_grade.get("rare", 0) - old_grade.get("rare", 0))},
        },
    }
    return result, issues


def latest_snapshot(root: Path) -> tuple[str | None, dict[str, Any] | None]:
    candidates: list[tuple[datetime, str, dict[str, Any]]] = []
    snapshots_root = root / "snapshots"
    if not snapshots_root.is_dir():
        return None, None
    for path in snapshots_root.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        data, issues = _read_manifest(root, path.name)
        if issues or data is None:
            continue
        try:
            created = datetime.fromisoformat(str(data.get("created_at")))
        except ValueError:
            continue
        candidates.append((created, path.name, data))
    if not candidates:
        return None, None
    _, name, data = max(candidates, key=lambda item: item[0])
    return name, data
