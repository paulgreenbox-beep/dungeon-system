from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import Issue
from .v041_paths import atomic_bytes, json_bytes


METADATA_SCHEMA = "dungeon-state/v1"


def metadata_path(root: Path) -> Path:
    return root / ".project" / "state.json"


def load_metadata(root: Path) -> tuple[dict[str, Any], list[Issue]]:
    path = metadata_path(root)
    if not path.exists():
        return {"schema": METADATA_SCHEMA, "artifacts": {}, "docs": {}, "snapshot": {}}, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {}, [Issue("INVALID_METADATA", f"cannot parse .project/state.json: {exc}", path)]
    if not isinstance(data, dict):
        return {}, [Issue("INVALID_METADATA", ".project/state.json must be an object", path)]
    return data, []


def atomic_write_json(path: Path, data: dict[str, Any], *, root: Path | None = None) -> None:
    # All current metadata writers pass an explicit root. The compatibility
    # default permits only a direct file in its already named parent directory.
    atomic_bytes(root or path.parent, path, json_bytes(data))


def metadata_updates(root: Path, updates: dict[str, Any]) -> dict[str, Any]:
    data, _ = load_metadata(root)
    if not isinstance(data, dict):
        data = {"schema": METADATA_SCHEMA}
    data.setdefault("schema", METADATA_SCHEMA)
    data.setdefault("artifacts", {})
    data.setdefault("docs", {})
    data.setdefault("snapshot", {})
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(value)
        else:
            data[key] = value
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return data


def update_metadata(root: Path, updates: dict[str, Any]) -> dict[str, Any]:
    data = metadata_updates(root, updates)
    atomic_write_json(metadata_path(root), data, root=root)
    return data


def mark_artifact(root: Path, name: str, input_hash: str, path: str) -> dict[str, Any]:
    return update_metadata(root, {"artifacts": {name: {"input_hash": input_hash, "path": path, "generated_at": datetime.now(timezone.utc).isoformat()}}})
