from __future__ import annotations

from pathlib import Path

from .libraries import discover_libraries, validate_library_refs
from .model import Issue, ProjectSnapshot
from .snapshots import validate_snapshots
from .validator import validate_snapshot


REQUIRED_DOCS = (
    "project.md",
    "map.md",
    "modules.md",
    "architecture.md",
    "agent-guides.md",
)


def validate_project(snapshot: ProjectSnapshot, include_projections: bool = True) -> list[Issue]:
    issues = validate_snapshot(snapshot, include_projections=include_projections)
    docs_root = snapshot.root / "docs"
    for filename in REQUIRED_DOCS:
        path = docs_root / filename
        if not path.exists():
            issues.append(Issue("MISSING_DOCUMENTATION", f"required documentation is missing: docs/{filename}", path))
    cronica_root = docs_root / "cronica"
    if not cronica_root.is_dir():
        issues.append(Issue("MISSING_CRONICA", "docs/cronica directory is missing", cronica_root))
    elif not (cronica_root / "README.md").exists():
        issues.append(Issue("MISSING_CRONICA", "docs/cronica/README.md is missing", cronica_root / "README.md"))

    libraries = discover_libraries(snapshot.root)
    issues.extend(libraries.issues)
    issues.extend(validate_library_refs(snapshot, libraries))
    issues.extend(validate_snapshots(snapshot.root))
    return issues
