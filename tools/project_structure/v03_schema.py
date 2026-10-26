from __future__ import annotations

import json
from pathlib import Path

from .model import Issue


def validate_snapshot_schema(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    snapshots_root = root / "snapshots"
    if not snapshots_root.is_dir():
        return issues
    for snapshot_dir in snapshots_root.iterdir():
        if not snapshot_dir.is_dir() or snapshot_dir.name.startswith("."):
            continue
        manifest_path = snapshot_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if isinstance(data, dict) and data.get("schema_version") is not None and str(data.get("schema_version")) != "1":
            issues.append(Issue("UNSUPPORTED_SCHEMA_VERSION", f"snapshot schema {data.get('schema_version')} is not supported", manifest_path))
        if isinstance(data, dict) and ("revisions" in data or "revisions_schema" in data):
            from .vcs_service import provenance_errors
            errors = provenance_errors({"schema_version": data.get("revisions_schema"), "scopes": data.get("revisions")})
            issues.extend(Issue("INVALID_SNAPSHOT_REVISIONS", error, manifest_path) for error in errors)
    return issues
