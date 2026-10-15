from __future__ import annotations

from collections import Counter
from pathlib import Path

from .model import (
    ISOLATION_LEVELS,
    RETENTION_CLASSES,
    STORAGE_FLAGS,
    STORAGE_STATES,
    VALID_LEVELS,
    Issue,
    ModuleSnapshot,
    ProjectSnapshot,
)
from .parser import parse_projection


def module_counts(module: ModuleSnapshot) -> dict[str, int]:
    total = len(module.sklts)
    rare = sum(1 for record in module.sklts if record.state == "RARE")
    return {"sklt": total, "rare": rare}


def project_counts(snapshot: ProjectSnapshot) -> dict[str, int]:
    return {key: sum(module_counts(module)[key] for module in snapshot.modules) for key in ("sklt", "rare")}


def render_counts(counts: dict[str, int]) -> str:
    return f"sklt {counts['sklt']}\nrare {counts['rare']}\n"


def _explicit_glif_refs(record) -> list[str]:
    refs: list[str] = []
    for key in ("glif", "glif_path", "glif_ref", "interface"):
        value = record.fields.get(key)
        if isinstance(value, str) and value.strip():
            refs.append(value.strip())
        elif isinstance(value, list):
            refs.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
    return refs


def _ref_matches(record, reference: str, snapshot: ProjectSnapshot) -> bool:
    reference_path = Path(reference)
    if reference == record.object_id:
        return any(glif.object_id == record.object_id for glif in snapshot.glifs)
    if reference_path.suffix.lower() != ".glif":
        reference_path = reference_path.with_suffix(".glif")
    for glif in snapshot.glifs:
        try:
            if (record.path.parent / reference_path).resolve() == glif.path.resolve():
                return glif.object_id == record.object_id
        except OSError:
            continue
    return False


def _validate_duplicates(snapshot: ProjectSnapshot) -> list[Issue]:
    issues: list[Issue] = []
    id_records = {}
    object_records = {}
    for record in snapshot.sklts:
        if record.record_id:
            id_records.setdefault(record.record_id, []).append(record)
        if record.object_id:
            object_records.setdefault(record.object_id, []).append(record)
    for record_id, records in id_records.items():
        if len(records) > 1:
            for record in records:
                issues.append(Issue("DUPLICATE_SKLT_ID", f"SKLT id '{record_id}' is duplicated", record.path))
    for object_id, records in object_records.items():
        if len(records) > 1:
            for record in records:
                issues.append(Issue("DUPLICATE_OBJECT_ID", f"object_id '{object_id}' is duplicated in SKLT files", record.path))
    for glif in snapshot.glifs:
        duplicates = [element_id for element_id, count in Counter(glif.element_ids).items() if count > 1]
        for element_id in duplicates:
            issues.append(Issue("DUPLICATE_GLIF_ELEMENT_ID", f"GLIF element id '{element_id}' is duplicated", glif.path))
    return issues


def _validate_linkage(snapshot: ProjectSnapshot) -> list[Issue]:
    issues: list[Issue] = []
    object_ids = {record.object_id for record in snapshot.sklts if record.object_id}
    for glif in snapshot.glifs:
        if glif.object_id and glif.object_id not in object_ids:
            issues.append(Issue("GLIF_WITHOUT_SKLT", f"GLIF object_id '{glif.object_id}' has no matching SKLT", glif.path))
    for record in snapshot.sklts:
        for reference in _explicit_glif_refs(record):
            if not _ref_matches(record, reference, snapshot):
                issues.append(Issue("SKLT_WITH_BROKEN_GLIF_REF", f"SKLT references missing or mismatched GLIF '{reference}'", record.path))
    return issues


def _validate_projections(snapshot: ProjectSnapshot) -> list[Issue]:
    issues: list[Issue] = []
    for module in snapshot.modules:
        if not module.exp_path.exists():
            issues.append(Issue("INVALID_EXP", "module .exp projection is missing", module.exp_path))
            continue
        actual, projection_issues = parse_projection(module.exp_path)
        issues.extend(projection_issues)
        if actual is not None and actual != module_counts(module):
            issues.append(Issue("EXP_MISMATCH", f"expected {render_counts(module_counts(module)).strip()!r}", module.exp_path))

    global_path = snapshot.global_grade_path
    if not global_path.exists():
        issues.append(Issue("INVALID_EXP", "global docs/.grade projection is missing", global_path))
    else:
        actual, projection_issues = parse_projection(global_path)
        issues.extend(projection_issues)
        if actual is not None and actual != project_counts(snapshot):
            issues.append(Issue("GLOBAL_EXP_MISMATCH", f"expected {render_counts(project_counts(snapshot)).strip()!r}", global_path))
    return issues


def _validate_levels(snapshot: ProjectSnapshot) -> list[Issue]:
    issues: list[Issue] = []
    for record in snapshot.sklts:
        if "level" not in record.fields:
            continue
        level = record.level
        if level is None:
            issues.append(Issue("UNKNOWN_LEVEL", "level is empty", record.path))
        elif level not in VALID_LEVELS:
            issues.append(Issue("UNKNOWN_LEVEL", f"level '{level}' is not one of {', '.join(sorted(VALID_LEVELS))}", record.path))
    return issues


def _validate_storage_axes(snapshot: ProjectSnapshot) -> list[Issue]:
    issues: list[Issue] = []
    for record in snapshot.sklts:
        if "storage_state" in record.fields and record.storage_state not in STORAGE_STATES:
            issues.append(Issue("INVALID_STORAGE_STATE", f"storage_state '{record.storage_state}' is not one of {', '.join(sorted(STORAGE_STATES))}", record.path))
        if "retention" in record.fields:
            retention = record.retention
            if retention is None:
                issues.append(Issue("INVALID_RETENTION_CLASS", "retention is empty", record.path))
            elif retention not in RETENTION_CLASSES:
                issues.append(Issue("INVALID_RETENTION_CLASS", f"retention '{retention}' is not one of {', '.join(sorted(RETENTION_CLASSES))}", record.path))
        for flag in record.flags:
            if flag not in STORAGE_FLAGS:
                issues.append(Issue("INVALID_STORAGE_FLAG", f"flag '{flag}' is not one of {', '.join(sorted(STORAGE_FLAGS))}", record.path))
        if "isolation" in record.fields:
            isolation = record.isolation
            if isolation is None:
                issues.append(Issue("INVALID_ISOLATION_LEVEL", "isolation is empty", record.path))
            elif isolation not in ISOLATION_LEVELS:
                issues.append(Issue("INVALID_ISOLATION_LEVEL", f"isolation '{isolation}' is not one of {', '.join(sorted(ISOLATION_LEVELS))}", record.path))
    return issues


def validate_snapshot(snapshot: ProjectSnapshot, include_projections: bool = True) -> list[Issue]:
    issues = list(snapshot.issues)
    issues.extend(_validate_duplicates(snapshot))
    issues.extend(_validate_linkage(snapshot))
    issues.extend(_validate_levels(snapshot))
    issues.extend(_validate_storage_axes(snapshot))
    if include_projections:
        issues.extend(_validate_projections(snapshot))
    return issues
