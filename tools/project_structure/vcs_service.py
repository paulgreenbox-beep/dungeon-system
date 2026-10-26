"""Read-only VersionScope orchestration. No scheduling or VCS write authority."""
from __future__ import annotations

from pathlib import Path
import stat
from typing import Any

from .v03_issues import issue_to_dict
from .v03_manifest import load_manifest


def _failure(code: str, message: str) -> dict:
    return {"status": "FAIL", "issues": [{"code": code, "message": message, "severity": "ERROR"}]}


def _no_reparse(path: Path) -> bool:
    if ".." in path.parts:
        return False
    try:
        for part in [*reversed(path.parents), path]:
            try:
                info = part.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return False
        return True
    except OSError:
        return False


def _catalog(root: Path):
    from .v04_projects import project_catalog
    from .model import Issue
    if not _no_reparse(root / ".project" / "project.json"):
        raise ValueError("unsafe project manifest path")
    manifest, issues = load_manifest(root)
    catalog = project_catalog(root)
    issues.extend(Issue(row.get("code", "VCS_STRUCTURE_ERROR"), row.get("message", "invalid structural context"))
                  for row in catalog.get("issues", []) if row.get("severity", "ERROR") == "ERROR")
    objects = []
    for identity, record in catalog.get("objects", {}).items():
        objects.append({"object_id": identity, "level_id": record.get("level"),
                        "scope_id": record.get("git_scope") or "main", "sklt": record["sklt"],
                        "source_refs": record.get("source", [])})
    return objects, manifest, issues


def object_revisions(root: Path, object_ids: list[str]) -> dict:
    """Capture only the explicitly selected objects' declared history scopes."""
    from .vcs_backends import capture_revisions
    objects, manifest, issues = _catalog(root)
    if issues:
        raise ValueError("structural errors prevent source revision capture")
    scopes = {item["scope_id"] for item in objects if item["object_id"] in object_ids}
    available = {scope.scope_id: scope for scope in manifest.effective_version_scopes()}
    if scopes - available.keys():
        raise ValueError("object refers to an unknown version scope")
    return {"schema_version": 1, "scopes": capture_revisions(root, [available[key] for key in sorted(scopes)])}


def provenance_errors(value: Any) -> list[str]:
    from .vcs_backends import validate_revision
    if (not isinstance(value, dict) or set(value) != {"schema_version", "scopes"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or not isinstance(value.get("scopes"), dict) or not value["scopes"]):
        return ["source_revision must contain schema_version 1 and non-empty scopes"]
    for key, revision in value["scopes"].items():
        if not isinstance(key, str) or not validate_revision(revision) or revision.get("scope_id") != key:
            return ["source_revision scopes must contain correctly namespaced typed revisions"]
    return []


def revision_identity(value: dict) -> dict:
    # Observation timestamps are not identities. UNKNOWN is never treated as fresh.
    return {key: value.get(key) for key in ("backend", "scope_id", "revision", "ref", "dirty_state")}


def provenance_matches(captured: dict, current: dict) -> bool:
    if provenance_errors(captured) or provenance_errors(current):
        return False
    if captured["scopes"].keys() != current["scopes"].keys():
        return False
    return all(before.get("availability") == after.get("availability") == "AVAILABLE"
               and before.get("revision") is not None and after.get("revision") is not None
               and before.get("dirty_state") in ("CLEAN", "DIRTY")
               and after.get("dirty_state") in ("CLEAN", "DIRTY")
               and revision_identity(before) == revision_identity(after)
               for key, before in captured["scopes"].items()
               for after in [current["scopes"][key]])


def provenance_equivalent(expected: dict, observed: dict) -> bool:
    """Compare caller guards without treating observation timestamps as IDs."""
    if provenance_errors(expected) or provenance_errors(observed):
        return False
    return (expected["scopes"].keys() == observed["scopes"].keys()
            and all(revision_identity(before) == revision_identity(observed["scopes"][key])
                    and before.get("availability") == observed["scopes"][key].get("availability")
                    for key, before in expected["scopes"].items()))


def vcs_query(root: Path, operation: str, scope_id: str | None = None,
              revision: str | None = None, limit: int = 20, object_id: str | None = None,
              target_backend: str | None = None) -> dict:
    from .vcs_backends import backend_for
    root = Path(root)
    if not root.is_absolute() or not root.is_dir():
        return _failure("INVALID_PROJECT_ROOT", "an existing absolute project root is required")
    # Root aliases/reparse points are rejected before resolve can hide their identity.
    if not _no_reparse(root / ".project" / "project.json"):
        return _failure("UNSAFE_VCS_ROOT", "project root or manifest contains traversal, symlink or reparse point")
    # Backend path guards apply to each query, including the implicit Git scope.
    root = root.absolute()
    manifest, issues = load_manifest(root)
    if issues:
        return {"status": "FAIL", "issues": [issue_to_dict(item, root) for item in issues]}
    scopes = manifest.effective_version_scopes()
    if scope_id is not None:
        scopes = [scope for scope in scopes if scope.scope_id == scope_id]
        if not scopes:
            return _failure("UNKNOWN_VERSION_SCOPE", "the requested version scope is not declared")
    if operation not in {"status", "inspect", "history", "diff", "sparse-plan", "migration-plan"}:
        return _failure("UNSUPPORTED_VCS_OPERATION", "only read-only VCS queries are supported")
    if type(limit) is not int or not 1 <= limit <= 100:
        return _failure("INVALID_VCS_LIMIT", "limit must be an integer between 1 and 100")
    if operation in {"sparse-plan", "migration-plan"}:
        try:
            objects, _, structural_issues = _catalog(root)
        except (ValueError, OSError):
            return _failure("UNSAFE_VCS_STRUCTURE", "structural context cannot be safely read")
        if structural_issues:
            return {"status": "FAIL", "issues": [issue_to_dict(item, root) for item in structural_issues]}
        selected = [obj for obj in objects if obj["scope_id"] in {scope.scope_id for scope in scopes}]
        if operation == "sparse-plan":
            selected = [obj for obj in selected if obj["object_id"] == object_id]
            if len(selected) != 1:
                return _failure("UNKNOWN_OBJECT", "select exactly one known object in the requested scope")
            scope = next(s for s in scopes if s.scope_id == selected[0]["scope_id"])
            from .v03_manifest import _local_scope_path
            paths = [ref["path"] for ref in selected[0]["source_refs"]]
            if any(not _local_scope_path(path) or not _no_reparse(root / path) for path in paths):
                return _failure("UNSAFE_SOURCE_REF", "sparse selection requires project-relative source refs")
            prefixes = []
            for path in paths:
                try:
                    prefixes.append(Path(path).relative_to(Path(scope.root)).as_posix())
                except ValueError:
                    return _failure("SOURCE_OUTSIDE_VERSION_SCOPE", "source ref is outside the selected history scope")
            return {"status": "PASS", "plan_only": True, "writes": False, "object_id": object_id,
                    "scope_id": scope.scope_id, "prefixes": sorted(set(prefixes)),
                    "dependency_closure": "NOT_COMPUTED", "checkout": "UNSUPPORTED", "issues": []}
        if target_backend not in ("GIT", "SCION"):
            return _failure("INVALID_VCS_BACKEND", "target_backend must be GIT or SCION")
        # References are listed, not rewritten or migrated. No blob/CAS traversal.
        snapshots = sorted(p.parent.name for p in (root / "snapshots").glob("*/manifest.json"))
        from .v03_cas import evidence_root
        evidence = sorted(p.stem for p in evidence_root(root).glob("manifests/*.json"))
        return {"status": "PASS", "plan_only": True, "writes": False,
                "current_scopes": [scope.as_dict() for scope in scopes], "target_backend": target_backend,
                "affected_objects": selected, "affected_levels": sorted({o["level_id"] for o in selected if o["level_id"]}),
                "snapshot_refs_to_review": snapshots, "evidence_refs_to_review": evidence,
                "reference_scan": "CONSERVATIVE_ALL_LOCAL_RECORDS_NOT_TRANSITIVE",
                "risks": ["No migration performed", "No cross-backend atomicity", "Review source/snapshot/evidence history links",
                          "Scion Git import is one-way; no transparent two-way sync"], "issues": []}
    results = {}
    for scope in scopes:
        backend = backend_for(root, scope)
        if operation == "status":
            result = backend.status()
        elif operation == "inspect":
            result = backend.inspect(revision=revision)
        elif operation == "history":
            result = backend.history(limit=limit, revision=revision)
        else:
            result = backend.diff()
        results[scope.scope_id] = result
    states = {result.get("status") for result in results.values()}
    return {"status": "PASS" if states == {"PASS"} else "FAIL" if states == {"FAIL"} else "PARTIAL", "experimental": True,
            "writes": False, "scopes": results, "issues": []}
