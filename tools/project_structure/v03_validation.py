from __future__ import annotations

from pathlib import Path

from .libraries import LibraryRegistry, discover_libraries
from .model import Issue, ProjectSnapshot
from .parser import parse_projection
from .snapshots import validate_snapshots
from .v03_dependencies import normalize_dependencies
from .v03_evidence import validate_cronica_evidence_refs, validate_snapshot_evidence_refs
from .v03_hash import structural_hash
from .v03_index import index_path, load_object_index
from .v03_issues import severity_for
from .v03_manifest import ProjectManifest
from .v03_metadata import load_metadata
from .v03_schema import validate_snapshot_schema
from .v03_snapshots import latest_snapshot
from .v03_sources import validate_source_refs
from .v03_toolkit import flatten_toolkit_refs, validate_toolkit_refs
from .validator import module_counts, project_counts, validate_snapshot


REQUIRED_DOCS = ("project.md", "map.md", "modules.md", "architecture.md", "agent-guides.md")


def _schema_issues(snapshot: ProjectSnapshot, libraries: LibraryRegistry, manifest: ProjectManifest) -> list[Issue]:
    issues: list[Issue] = []
    if manifest.schema_version > 1:
        issues.append(Issue("UNSUPPORTED_SCHEMA_VERSION", f"manifest schema {manifest.schema_version} is not supported", manifest.path))
    for record in snapshot.sklts:
        version = record.fields.get("schema_version")
        if version is None or str(version).strip() == "":
            continue
        if str(version).strip() != "1":
            issues.append(Issue("UNSUPPORTED_SCHEMA_VERSION", f"SKLT schema {version} is not supported", record.path))
    for glif in snapshot.glifs:
        version = glif.data.get("schema_version")
        if version is not None and str(version).strip() != "1":
            issues.append(Issue("UNSUPPORTED_SCHEMA_VERSION", f"GLIF schema {version} is not supported", glif.path))
    for descriptor in libraries.descriptors:
        version = descriptor.data.get("schema_version")
        if version is not None and str(version).strip() != "1":
            issues.append(Issue("UNSUPPORTED_SCHEMA_VERSION", f"library schema {version} is not supported", descriptor.path))
    return issues


def _projection_status(path: Path, expected: dict[str, int], artifact_meta: dict, stale_code: str) -> tuple[str, list[Issue]]:
    if not path.exists():
        return "MISSING", [Issue(stale_code, f"derived projection is missing: {path}", path)]
    actual, parse_issues = parse_projection(path)
    if parse_issues:
        return "INVALID", parse_issues
    if artifact_meta.get("input_hash") is None:
        return "STALE", [Issue("MISSING_PROJECTION_METADATA", f"projection metadata is missing for {path}", path)]
    if actual != expected or artifact_meta.get("input_hash") != artifact_meta.get("current_hash"):
        return "STALE", [Issue(stale_code, f"derived projection is stale: {path}", path)]
    return "CURRENT", []


def projection_statuses(snapshot: ProjectSnapshot, manifest: ProjectManifest) -> tuple[dict[str, str], list[Issue]]:
    metadata, metadata_issues = load_metadata(snapshot.root)
    issues = list(metadata_issues)
    current_hash = structural_hash(snapshot, manifest)
    artifacts = metadata.get("artifacts", {}) if isinstance(metadata, dict) else {}
    exp_states: list[str] = []
    for module in snapshot.modules:
        meta = dict(artifacts.get("exp", {}))
        meta["current_hash"] = current_hash
        state, state_issues = _projection_status(module.exp_path, module_counts(module), meta, "STALE_EXP")
        exp_states.append(state)
        issues.extend(state_issues)
    grade_meta = dict(artifacts.get("grade", {}))
    grade_meta["current_hash"] = current_hash
    grade_state, grade_issues = _projection_status(snapshot.global_grade_path, project_counts(snapshot), grade_meta, "STALE_GRADE")
    issues.extend(grade_issues)

    index_state = "MISSING"
    index_data, index_issues = load_object_index(snapshot.root)
    issues.extend(index_issues)
    if index_issues:
        index_state = "INVALID"
    elif index_data is not None:
        index_state = "CURRENT" if index_data.get("input_hash") == current_hash and artifacts.get("objects_index", {}).get("input_hash") == current_hash else "STALE"
        if index_state == "STALE":
            issues.append(Issue("STALE_OBJECT_INDEX", "object index does not match current structural inputs", index_path(snapshot.root)))
    statuses = {
        "exp": "INVALID" if "INVALID" in exp_states else ("STALE" if "STALE" in exp_states else ("MISSING" if "MISSING" in exp_states else "CURRENT")),
        "grade": grade_state,
        "objects_index": index_state,
    }
    return statuses, issues


def _git_scope_issues(snapshot: ProjectSnapshot, manifest: ProjectManifest) -> list[Issue]:
    issues: list[Issue] = []
    known = manifest.known_git_scopes()
    for record in snapshot.sklts:
        scope = record.git_scope
        if scope and scope not in known:
            issues.append(Issue("UNKNOWN_GIT_SCOPE", f"git_scope '{scope}' is not declared in git_scopes", record.path))
    return issues


def validate_v03(snapshot: ProjectSnapshot, manifest: ProjectManifest, libraries: LibraryRegistry | None = None) -> list[Issue]:
    libraries = libraries or discover_libraries(snapshot.root)
    issues = list(snapshot.issues)
    issues.extend(validate_snapshot(snapshot, include_projections=False))
    issues.extend(_schema_issues(snapshot, libraries, manifest))
    issues.extend(_git_scope_issues(snapshot, manifest))
    issues.extend(libraries.issues)
    issues.extend(validate_source_refs(snapshot)[1])
    issues.extend(normalize_dependencies(snapshot, set(libraries.by_id))[1])

    docs_root = snapshot.root / "docs"
    for filename in REQUIRED_DOCS:
        path = docs_root / filename
        if not path.exists():
            issues.append(Issue("MISSING_DOCUMENTATION", f"required documentation is missing: docs/{filename}", path))
    cronica = docs_root / "cronica"
    if not cronica.is_dir() or not (cronica / "README.md").exists():
        issues.append(Issue("MISSING_CRONICA", "docs/cronica/README.md is missing", cronica / "README.md"))
    issues.extend(validate_snapshots(snapshot.root))
    issues.extend(validate_snapshot_schema(snapshot.root))
    issues.extend(validate_snapshot_evidence_refs(snapshot.root))
    issues.extend(validate_cronica_evidence_refs(snapshot.root))

    _, projection_issues = projection_statuses(snapshot, manifest)
    issues.extend(projection_issues)

    latest_name, latest = latest_snapshot(snapshot.root)
    if latest is None:
        issues.append(Issue("NO_SNAPSHOTS", "no valid snapshot exists"))
    elif latest.get("structural_hash") != structural_hash(snapshot, manifest):
        issues.append(Issue("PROJECT_CHANGED_SINCE_SNAPSHOT", f"project has structural changes since snapshot '{latest_name}'", snapshot.root / "snapshots" / latest_name))
    return issues


def status_data(snapshot: ProjectSnapshot, manifest: ProjectManifest, libraries: LibraryRegistry | None = None) -> tuple[dict[str, object], list[Issue]]:
    libraries = libraries or discover_libraries(snapshot.root)
    issues = validate_v03(snapshot, manifest, libraries)
    statuses, _ = projection_statuses(snapshot, manifest)
    latest_name, latest = latest_snapshot(snapshot.root)
    current_hash = structural_hash(snapshot, manifest)
    objects = len({record.object_id for record in snapshot.sklts if record.object_id})
    refs_by_object, _ = validate_toolkit_refs(snapshot)
    refs = flatten_toolkit_refs(refs_by_object)
    storage_states: dict[str, int] = {}
    isolations: dict[str, int] = {}
    for record in snapshot.sklts:
        storage_states[record.storage_state] = storage_states.get(record.storage_state, 0) + 1
        if record.isolation:
            isolations[record.isolation] = isolations.get(record.isolation, 0) + 1
    data = {
        "project_id": manifest.project_id,
        "validation": "PASS" if not any(severity_for(issue.code) == "ERROR" for issue in issues) else "FAIL",
        "modules": len(snapshot.modules),
        "objects": objects,
        "sklt": len(snapshot.sklts),
        "rare": sum(record.state == "RARE" for record in snapshot.sklts),
        "projections": statuses,
        "structural_hash": current_hash,
        "latest_snapshot": {"id": latest_name, "state": "CURRENT" if latest and latest.get("structural_hash") == current_hash else ("PROJECT_CHANGED" if latest else "MISSING")},
        "libraries": len(libraries.descriptors),
        "git_scopes": {
            "declared": [scope.as_dict() for scope in manifest.git_scopes],
            "effective": sorted(manifest.known_git_scopes()),
        },
        "storage_axes": {
            "structural": {"SKLT": sum(record.state == "SKLT" for record in snapshot.sklts), "RARE": sum(record.state == "RARE" for record in snapshot.sklts)},
            "storage_state": dict(sorted(storage_states.items())),
            "isolation": dict(sorted(isolations.items())),
            "retention": sorted({record.retention for record in snapshot.sklts if record.retention}),
        },
        "external_refs": {
            "toolkit": len(refs),
            "container": sum(ref.kind == "container" for ref in refs),
            "tool": sum(ref.kind == "tool" for ref in refs),
        },
    }
    return data, issues
