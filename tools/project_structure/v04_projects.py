"""Callable project discovery, read-only catalog and explicit adoption facade.

All public results are JSON-compatible dictionaries with status/issues. A
catalog is an in-memory view, never a persisted object index. The caller may
paginate its objects/modules; detailed SKLT text is intentionally omitted.
"""

from __future__ import annotations
from .v041_paths import output_boundary, atomic_bytes, UnsafeOutputPath

import hashlib
import json
import os
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from .libraries import LibraryRegistry, discover_libraries
from .model import ModuleSnapshot, ProjectSnapshot
from .parser import parse_glif, parse_projection, parse_sklt
from .projections import write_projections
from .v03_dependencies import normalize_dependencies
from .v03_hash import structural_hash
from .v03_index import INDEX_SCHEMA, build_object_index, write_object_index
from .v03_issues import issue_summary, issue_to_dict, severity_for
from .v03_manifest import DEFAULT_IGNORED, DEFAULT_RESERVED, ProjectManifest, is_ignored_relative, is_reserved_relative, load_manifest
from .v03_metadata import METADATA_SCHEMA, atomic_write_json, mark_artifact
from .v03_sources import validate_source_refs
from .v03_validation import _git_scope_issues, _schema_issues, validate_v03
from .validator import validate_snapshot
from .v04_sklt import checked_path, checked_root, discover, issue, read_bounded, read_sklt


PLAN_SCHEMA = "dungeon-adoption-plan/v1"
RECEIPT_SCHEMA = "dungeon-adoption-receipt/v1"
RECEIPT_PATH = ".project/adoption.json"
CATALOG_OBJECT_LIMIT = 20_000


def _hash(data: Any) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def _status(issues: list[dict]) -> str:
    return "FAIL" if any(item["severity"] == "ERROR" for item in issues) else "PASS"


def _dedupe(issues: list[dict]) -> list[dict]:
    unique = {_hash(item): item for item in issues}
    return sorted(unique.values(), key=lambda item: (item.get("path", ""), item["code"], str(item.get("line", "")), item["message"]))


def _git(root: Path) -> tuple[dict, list[dict]]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    # Ambient GIT_DIR / GIT_WORK_TREE must not report a different repository.
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(key, None)
    def run(*args: str):
        return subprocess.run(["git", "-C", str(root), "rev-parse", *args], capture_output=True, text=True, timeout=5, env=env)
    try:
        repository = run("--show-toplevel")
        if repository.returncode:
            if "not a git repository" in repository.stderr.lower():
                return {"detected": False, "repository_root": None, "head": None, "branch": None}, []
            return {"detected": None, "repository_root": None, "head": None, "branch": None}, [
                issue("GIT_DETECTION_FAILED", "Git could not establish repository identity; repair Git configuration or access and probe again", severity="WARNING")]
        repository_root = str(Path(repository.stdout.strip()).resolve())
        head, branch = run("--verify", "HEAD"), run("--abbrev-ref", "HEAD")
        return {"detected": True, "repository_root": repository_root, "at_project_root": Path(repository_root) == root,
                "head": head.stdout.strip() if head.returncode == 0 else None,
                "branch": branch.stdout.strip() if branch.returncode == 0 else None}, []
    except (OSError, subprocess.SubprocessError) as exc:
        return {"detected": None, "repository_root": None, "head": None, "branch": None}, [
            issue("GIT_UNAVAILABLE", f"Git detection could not be completed: {exc}", severity="WARNING")]


def _manifest(root: Path) -> tuple[ProjectManifest, dict | None, list[dict]]:
    manifest = ProjectManifest(root=root, ignore=sorted(DEFAULT_IGNORED))
    relative = ".project/project.json"
    issues: list[dict] = []
    try:
        path = checked_path(root, relative)
        if not os.path.lexists(path):
            return manifest, None, []
        manifest.exists = True
        raw = json.loads(read_bounded(path).decode("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("manifest must be a JSON object")
        if not isinstance(raw.get("project_id"), str) or not raw["project_id"].strip():
            issues.append(issue("INVALID_PROJECT_MANIFEST", "manifest needs a non-empty project_id", relative))
        if type(raw.get("schema_version", 1)) is not int:
            issues.append(issue("INVALID_PROJECT_MANIFEST", "schema_version must be an integer, not a boolean", relative))
        manifest, original_issues = load_manifest(root)
        issues.extend(issue_to_dict(item, root) for item in original_issues)
        module_paths: list[tuple[str, bool]] = []
        raw_modules = raw.get("modules", [])
        if isinstance(raw_modules, list):
            for item in raw_modules:
                if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                    continue
                try:
                    candidate = checked_path(root, item["path"])
                    if not candidate.is_dir():
                        raise ValueError(f"module directory does not exist: {item['path']}")
                    relative_path = candidate.relative_to(root).as_posix()
                    if is_reserved_relative(manifest, relative_path) and item.get("allow_reserved") is not True:
                        raise ValueError("reserved module path needs explicit allow_reserved=true")
                    if is_ignored_relative(manifest, relative_path):
                        raise ValueError("module path is excluded by manifest ignore rules")
                    recursive = item.get("recursive", True)
                    for earlier, earlier_recursive in module_paths:
                        a, b = Path(earlier), Path(relative_path)
                        if a == b or (a in b.parents and earlier_recursive) or (b in a.parents and recursive):
                            raise ValueError("module paths overlap; register each structural file once")
                    module_paths.append((relative_path, recursive))
                except (OSError, ValueError) as exc:
                    issues.append(issue("INVALID_MODULE_PATH", str(exc), relative))
        for value in manifest.docs.values():
            try:
                checked_path(root, value)
            except (OSError, ValueError) as exc:
                issues.append(issue("INVALID_CONFIG_PATH", str(exc), relative))
        for scope in manifest.git_scopes:
            if scope.mode == "EXTERNAL":
                continue
            for value in (scope.root, scope.repository):
                try:
                    checked_path(root, value)
                except (OSError, ValueError) as exc:
                    issues.append(issue("INVALID_CONFIG_PATH", str(exc), relative))
        return manifest, raw, issues
    except (OSError, UnicodeError, ValueError) as exc:
        # A broken link/directory in the manifest location is a bad configuration,
        # not an absent manifest and never an invitation to overwrite it.
        manifest.exists = os.path.lexists(root / ".project")
        return manifest, None, [issue("INVALID_PROJECT_MANIFEST", str(exc), relative)]


def _load(root_value: str | os.PathLike) -> dict:
    try:
        root = checked_root(root_value)
        # Verify actual directory access, not just a successful stat.
        with os.scandir(root):
            pass
    except (OSError, ValueError, TypeError) as exc:
        return {"root": None, "probe": {"status": "FAIL", "mode": "UNAVAILABLE", "project_mode": "UNAVAILABLE",
                "project_root": str(root_value), "dungeon_manifest": None, "sklt_count": None, "glif_count": None,
                "counts_exact": False, "git_detected": None, "git": {"detected": None}, "can_adopt": False,
                "issues": [issue("UNAVAILABLE_PROJECT_ROOT", str(exc))],
                "next_steps": ["Provide an accessible absolute directory root."]}, "objects": {}, "modules": [], "duplicates": {}, "records": [], "sklt_paths": []}
    manifest, raw_manifest, manifest_issues = _manifest(root)
    discovery = discover(root, manifest if not manifest_issues else ProjectManifest(root, ignore=sorted(DEFAULT_IGNORED)))
    issues = list(manifest_issues) + discovery["issues"]
    git, git_issues = _git(root)
    issues.extend(git_issues)
    canonical_modules: dict[str, ModuleSnapshot] = {}
    if not manifest_issues:
        canonical_modules.update({module.module_id: ModuleSnapshot(module.module_id, root / module.path) for module in manifest.modules})
    snapshot = ProjectSnapshot(root)
    records: list[dict] = []
    glif_by_id: dict[str, list[str]] = defaultdict(list)
    for entry in discovery["sklt"]:
        if len(records) >= CATALOG_OBJECT_LIMIT:
            issues.append(issue("CATALOG_LIMIT", f"catalog reached its {CATALOG_OBJECT_LIMIT} declaration/object limit; narrow module selection", entry["path"]))
            break
        try:
            parsed, canonical, parse_issues = read_sklt(root, entry)
            if len(records) + len(parsed) > CATALOG_OBJECT_LIMIT:
                parsed = parsed[:CATALOG_OBJECT_LIMIT - len(records)]
                issues.append(issue("CATALOG_LIMIT", f"catalog reached its {CATALOG_OBJECT_LIMIT} declaration/object limit; narrow module selection", entry["path"]))
            records.extend(parsed)
            issues.extend(parse_issues)
            if canonical is not None:
                module = canonical_modules.setdefault(entry["module"], ModuleSnapshot(entry["module"], root / entry["module_path"]))
                module.sklts.append(canonical)
        except (OSError, ValueError) as exc:
            issues.append(issue("UNREADABLE_SKLT", str(exc), entry["path"]))
    for entry in discovery["glif"]:
        try:
            path = checked_path(root, entry["path"])
            read_bounded(path)  # Bound the legacy parser before it reads JSON.
            glif, parse_issues = parse_glif(path)
            issues.extend(issue_to_dict(item, root) for item in parse_issues)
            module = canonical_modules.setdefault(entry["module"], ModuleSnapshot(entry["module"], root / entry["module_path"]))
            module.glifs.append(glif)
            if glif.object_id:
                glif_by_id[glif.object_id].append(entry["path"])
        except (OSError, ValueError) as exc:
            issues.append(issue("UNREADABLE_GLIF", str(exc), entry["path"]))
    snapshot.modules = list(canonical_modules.values())
    # The v0.3 dependency rules are retained for canonical objects.
    libraries, library_safety_issues = _safe_libraries(root)
    issues.extend(library_safety_issues)
    dependencies, dependency_issues = normalize_dependencies(snapshot, set(libraries.by_id))
    issues.extend(issue_to_dict(item, root) for item in dependency_issues)
    by_id: dict[str, list[dict]] = defaultdict(list)
    summaries: dict[str, dict] = {}
    physical_paths = {entry["module"]: entry["module_path"] for entry in discovery["sklt"] + discovery["glif"]}
    if not manifest_issues:
        for module in manifest.modules:
            summaries[module.module_id] = {"id": module.module_id, "path": module.path, "object_ids": [], "sklt_paths": [],
                                           "object_count": 0, "sklt_count": 0, "dialects": []}
    for record in records:
        if record["dialect"] == "CANONICAL":
            record["dependencies"] = [item.as_dict() for item in dependencies.get(record["object_id"], [])]
        record["glif"] = sorted(glif_by_id.get(record["object_id"], []))
        by_id[record["object_id"]].append(record)
        summary = summaries.setdefault(record["module"], {"id": record["module"], "path": physical_paths.get(record["module"]),
                    "object_ids": [], "sklt_paths": [], "object_count": 0, "sklt_count": 0, "dialects": []})
        summary["object_ids"].append(record["object_id"])
        summary["sklt_paths"].append(record["sklt"])
        summary["dialects"].append(record["dialect"])
    objects: dict[str, dict] = {}
    duplicates: dict[str, list[dict]] = {}
    for object_id, occurrences in sorted(by_id.items()):
        objects[object_id] = dict(occurrences[0])
        if len(occurrences) > 1:
            duplicates[object_id] = occurrences
            objects[object_id]["identity_conflict"] = True
            objects[object_id]["occurrences"] = [{"sklt": item["sklt"], "line_start": item["line_start"], "module": item["module"]} for item in occurrences]
            issues.append(issue("DUPLICATE_OBJECT_ID", f"object_id '{object_id}' has multiple declarations; inspect every occurrence", object_id=object_id,
                                occurrences=objects[object_id]["occurrences"]))
    for summary in summaries.values():
        for key in ("object_ids", "sklt_paths", "dialects"):
            summary[key] = sorted(set(summary[key]))
        summary["object_count"] = len(summary["object_ids"])
        summary["sklt_count"] = len(summary["sklt_paths"])
    mode = "INVALID_MANAGED" if manifest_issues else ("MANAGED" if manifest.exists else ("SKLT_ONLY" if discovery["sklt"] else "NOT_DUNGEON"))
    derived: dict[str, str] = {}
    if mode == "MANAGED":
        outputs = {"objects_index": ".project/objects.json", "grade": "docs/.grade"}
        outputs.update({f"exp:{module.name}": module.exp_path.relative_to(root).as_posix() for module in snapshot.modules})
        for key, relative in outputs.items():
            try:
                candidate = checked_path(root, relative, output=True)
                derived[key] = "PRESENT" if candidate.is_file() else "MISSING"
                if derived[key] == "MISSING":
                    issues.append(issue("MISSING_DERIVED_OUTPUT", "derived output is missing; refresh it explicitly if needed", relative, severity="WARNING"))
            except (OSError, ValueError) as exc:
                derived[key] = "UNSAFE"
                issues.append(issue("UNSAFE_DERIVED_OUTPUT", str(exc), relative))
    next_steps = {
        "MANAGED": ["Select a module/object from project_catalog; refresh missing derived outputs explicitly if needed."],
        "SKLT_ONLY": ["Read project_catalog and inspect one object's SKLT span.", "Run adoption_plan and explicitly accept supported candidate modules."],
        "NOT_DUNGEON": ["Run adoption_plan, review candidates, then explicitly apply accepted modules."],
        "INVALID_MANAGED": ["Repair the reported manifest/schema/path conflicts, then run project_probe again."],
    }[mode]
    issues = _dedupe(issues)
    unsupported_adoption = any(record["dialect"] != "CANONICAL" for record in records) or len(snapshot.sklts) != len(discovery["sklt"])
    can_adopt = mode in {"SKLT_ONLY", "NOT_DUNGEON", "MANAGED"} and discovery["complete"] and not duplicates and not unsupported_adoption and _status(issues) == "PASS"
    probe = {"status": _status(issues), "mode": mode, "project_mode": "DUNGEON" if mode == "MANAGED" else mode,
             "project_root": str(root), "project_id": manifest.project_id if manifest.exists and raw_manifest else None,
             "dungeon_manifest": manifest.exists, "sklt_count": len(discovery["sklt"]), "glif_count": len(discovery["glif"]),
             "object_count": len(objects), "module_count": len(summaries), "counts_exact": discovery["complete"],
             "object_count_exact": not any(item["code"] in {"CATALOG_LIMIT", "DECLARATION_LIMIT", "UNREADABLE_SKLT", "UNRECOGNIZED_SKLT_DIALECT"} for item in issues),
             "derived": derived,
             "git_detected": git["detected"], "git": git,
             "can_plan_adoption": True, "can_adopt": can_adopt, "adoption_executable": can_adopt,
             "adoption_support": "CANONICAL_ONLY",
             "adoption_blockers": sorted({item["code"] for item in issues if item["severity"] == "ERROR"} | ({"UNSUPPORTED_ADOPTION_DIALECT"} if unsupported_adoption else set())),
             "issues": issues, "next_steps": next_steps}
    return {"root": root, "probe": probe, "manifest": manifest, "raw_manifest": raw_manifest, "discovery": discovery,
            "objects": objects, "modules": sorted(summaries.values(), key=lambda item: item["id"]), "duplicates": duplicates,
            "records": records, "snapshot": snapshot, "sklt_paths": [entry["path"] for entry in discovery["sklt"]]}


def project_probe(root: str | os.PathLike) -> dict:
    """Read-only five-mode diagnosis. Missing docs/index/projections do not change MANAGED."""
    return _load(root)["probe"]


def project_catalog(root: str | os.PathLike) -> dict:
    """Read-only ID->record view; includes duplicates, modules and active sklt_paths.

    No SKLT text is returned. Servers can paginate/filter the mapping and use
    line_start/line_end (one-based inclusive) to inspect a single declaration.
    Duplicate IDs retain the first record plus all occurrences; never choose an
    occurrence implicitly for editing or inspection.
    """
    loaded = _load(root)
    return {**loaded["probe"], "schema": "dungeon-structural-catalog/v1", "objects": loaded["objects"],
            "modules": loaded["modules"], "duplicates": loaded["duplicates"], "sklt_paths": loaded["sklt_paths"],
            "read_only": True, "pagination": "CALLER"}


def _candidate_modules(loaded: dict) -> list[dict]:
    candidates: dict[str, dict] = {}
    if loaded["probe"]["mode"] == "MANAGED":
        for module in loaded["manifest"].modules:
            candidates[module.module_id] = {"id": module.module_id, "path": module.path, "basis": "EXISTING_MANIFEST_MODULE",
                                           "recursive": module.recursive,
                                           "requires_acceptance": True, "sklt_paths": [], "object_ids": [], "dialects": [], "glif": []}
    for entry in loaded["discovery"]["sklt"]:
        candidate = candidates.setdefault(entry["module"], {"id": entry["module"], "path": entry["module_path"],
                    "recursive": entry["module_path"] != ".",
                    "basis": "EXISTING_SKLT_DIRECTORY", "requires_acceptance": True, "sklt_paths": [], "object_ids": [], "dialects": [], "glif": []})
        candidate["sklt_paths"].append(entry["path"])
    for candidate in candidates.values():
        candidate_records = [record for record in loaded["records"] if record["sklt"] in candidate["sklt_paths"]]
        candidate["object_ids"] = sorted({record["object_id"] for record in candidate_records})
        candidate["dialects"] = sorted({record["dialect"] for record in candidate_records})
        candidate["glif"] = sorted({entry["path"] for entry in loaded["discovery"]["glif"] if entry["module"] == candidate["id"]})
        candidate["adoptable"] = (candidate["dialects"] == ["CANONICAL"] and len(candidate_records) == len(candidate["sklt_paths"])) or (
            not candidate["sklt_paths"] and candidate["basis"] == "EXISTING_MANIFEST_MODULE")
        candidate["confirmed_on_disk"] = True
    return sorted(candidates.values(), key=lambda item: item["id"])


def _output_paths(candidates: list[dict]) -> list[str]:
    return sorted({".project/project.json", ".project/objects.json", ".project/state.json", "docs/.grade"}
                  | {((candidate["path"] + "/") if candidate["path"] != "." else "") + ".exp" for candidate in candidates})


def _file_stamp(root: Path, relative: str) -> dict:
    try:
        path = checked_path(root, relative, output=relative.endswith((".exp", ".grade", ".json")))
        if not os.path.lexists(path):
            return {"path": relative, "kind": "ABSENT"}
        if not path.is_file():
            return {"path": relative, "kind": "DIRECTORY" if path.is_dir() else "SPECIAL"}
        content = read_bounded(path)
        return {"path": relative, "kind": "FILE", "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    except (OSError, ValueError) as exc:
        return {"path": relative, "kind": "UNSAFE", "reason": str(exc)}


def _fingerprint(loaded: dict, candidates: list[dict]) -> tuple[str, list[dict]]:
    root = loaded["root"]
    paths = {entry["path"] for key in ("sklt", "glif") for entry in loaded["discovery"][key]} | set(_output_paths(candidates))
    for record in loaded["records"]:
        paths.update(ref["path"] for ref in record["source"])
    # Library descriptors influence canonical validation/dependency typing.
    try:
        libs = checked_path(root, "libs")
        if libs.is_dir():
            paths.update(f"libs/{child.name}/lib.json" for child in sorted(libs.iterdir()) if child.is_dir() and not child.name.startswith("."))
    except (OSError, ValueError):
        paths.add("libs")
    inputs = [_file_stamp(root, relative) for relative in sorted(paths)]
    return _hash({"project_root": str(root), "files": inputs, "directories": loaded["discovery"]["directories"],
                  "sources": loaded["discovery"]["source_directories"], "git": loaded["probe"]["git"],
                  "discovery_complete": loaded["discovery"]["complete"]}), inputs


def _output_conflicts(loaded: dict, candidates: list[dict]) -> list[dict]:
    conflicts: list[dict] = []
    root = loaded["root"]
    protected_outputs = set(_output_paths(candidates)) | {RECEIPT_PATH}
    for record in loaded["records"]:
        for ref in record["source"]:
            try:
                source_path = checked_path(root, ref["path"]).relative_to(root).as_posix()
                if source_path in protected_outputs:
                    conflicts.append(issue("DERIVED_SOURCE_CONFLICT", "a declared source occupies a derived output path; correct the explicit contract and replan", source_path))
            except (OSError, ValueError):
                pass  # Source safety is checked explicitly before staging.
    for relative in _output_paths(candidates) + [RECEIPT_PATH]:
        stamp = _file_stamp(root, relative)
        if stamp["kind"] == "ABSENT":
            continue
        if relative == ".project/project.json" and loaded["probe"]["mode"] == "MANAGED":
            continue
        valid = False
        if stamp["kind"] == "FILE" and loaded["probe"]["mode"] == "MANAGED":
            path = checked_path(root, relative, output=True)
            try:
                if relative.endswith((".exp", ".grade")):
                    valid = not parse_projection(path)[1]
                else:
                    data = json.loads(read_bounded(path).decode("utf-8"))
                    schemas = {".project/objects.json": INDEX_SCHEMA, ".project/state.json": METADATA_SCHEMA, RECEIPT_PATH: RECEIPT_SCHEMA}
                    valid = isinstance(data, dict) and data.get("schema") == schemas.get(relative)
                    if relative == ".project/state.json":
                        valid = valid and all(isinstance(data.get(key, {}), dict) for key in ("artifacts", "docs", "snapshot"))
            except (OSError, UnicodeError, ValueError):
                pass
        if not valid:
            conflicts.append(issue("DERIVED_OUTPUT_CONFLICT", "output is unsafe or not owned by this managed project; move it aside explicitly and replan", relative))
    return conflicts


def adoption_plan(root: str | os.PathLike, project_id: str | None = None) -> dict:
    """Read-only deterministic plan; pass it unchanged with accepted module IDs to apply."""
    loaded = _load(root)
    issues = list(loaded["probe"]["issues"])
    if loaded["root"] is None:
        return {**loaded["probe"], "schema": PLAN_SCHEMA, "plan_id": None, "input_fingerprint": None, "candidate_modules": [], "conflicts": issues, "read_only": True}
    existing_id = loaded["probe"]["project_id"]
    selected_id = project_id if project_id is not None else (existing_id or "dungeon.project")
    if not isinstance(selected_id, str) or not selected_id.strip() or len(selected_id) > 256:
        issues.append(issue("INVALID_PROJECT_ID", "project_id must be a non-empty string of at most 256 characters"))
        selected_id = None
    if existing_id and selected_id != existing_id:
        issues.append(issue("PROJECT_ID_CONFLICT", "adoption cannot rename an existing managed project"))
    candidates = _candidate_modules(loaded)
    fingerprint, inputs = _fingerprint(loaded, candidates)
    conflicts = [item for item in issues if item["severity"] == "ERROR"] + _output_conflicts(loaded, candidates)
    for candidate in candidates:
        if not candidate["adoptable"]:
            issues.append(issue("UNSUPPORTED_ADOPTION_DIALECT", "read-only declarations are navigable, but canonical adoption requires explicit responsibilities, API fields and dependencies; supply canonical SKLT or an explicit reviewed dialect adapter", candidate["path"], severity="WARNING", module=candidate["id"]))
    body = {"schema": PLAN_SCHEMA, "status": "BLOCKED" if conflicts or any(not candidate["adoptable"] for candidate in candidates) else "PASS",
            "mode": loaded["probe"]["mode"], "project_root": str(loaded["root"]), "project_id": selected_id,
            "input_fingerprint": fingerprint, "inputs": inputs, "git": loaded["probe"]["git"],
            "dungeon_manifest": loaded["probe"]["dungeon_manifest"], "sklt_count": loaded["probe"]["sklt_count"],
            "sklt_paths": loaded["sklt_paths"], "counts_exact": loaded["probe"]["counts_exact"],
            "candidate_modules": candidates, "object_ids": sorted(loaded["objects"]), "duplicates": loaded["duplicates"],
            "source_directories": loaded["discovery"]["source_directories"],
            "glif_paths": [entry["path"] for entry in loaded["discovery"]["glif"]],
            "conflicts": _dedupe(conflicts), "issues": _dedupe(issues + conflicts), "read_only": True,
            "next_steps": ["Review candidate modules; pass this unchanged plan and explicitly accepted module IDs to adoption_apply.",
                           "Resolve reported conflicts/unsupported dialects and replan before applying affected modules."]}
    return {**body, "plan_id": "adopt-" + _hash(body)}


def _failure(code: str, message: str, *, issues: list[dict] | None = None) -> dict:
    return {"status": "BLOCKED", "applied": False, "files_changed": [],
            "issues": _dedupe([issue(code, message)] + (issues or [])),
            "next_steps": ["Resolve the reported issue, run adoption_plan again, and review the new plan."]}


def _accepted(plan: dict, values: list) -> list[str]:
    if not isinstance(values, (list, tuple)):
        raise ValueError("accepted_modules must be a list of candidate module IDs or unchanged candidate entries")
    known = {item["id"]: item for item in plan["candidate_modules"]}
    ids: list[str] = []
    for value in values:
        if isinstance(value, dict):
            module_id = value.get("id")
            if module_id not in known or value != known[module_id]:
                raise ValueError("accepted module entry must match an unchanged candidate entry")
        else:
            module_id = value
        if not isinstance(module_id, str) or module_id not in known:
            raise ValueError(f"unknown accepted module: {module_id}")
        if module_id in ids:
            raise ValueError(f"module accepted more than once: {module_id}")
        ids.append(module_id)
    if known and not ids and plan.get("mode") != "MANAGED":
        raise ValueError("explicitly accept at least one existing candidate module")
    return sorted(ids)


def _safe_libraries(root: Path) -> tuple[LibraryRegistry, list[dict]]:
    try:
        libs = checked_path(root, "libs")
        if libs.is_dir():
            for child in libs.iterdir():
                checked_path(root, child.relative_to(root).as_posix())
                if child.is_dir() and not child.name.startswith("."):
                    descriptor = checked_path(root, f"libs/{child.name}/lib.json")
                    if descriptor.is_file():
                        read_bounded(descriptor)
        return discover_libraries(root), []
    except (OSError, ValueError) as exc:
        return LibraryRegistry(root), [issue("UNSAFE_LIBRARY_PATH", str(exc), "libs")]


def _selected_snapshot(loaded: dict, accepted: list[str]) -> ProjectSnapshot:
    # Existing managed registrations are never removed or narrowed by adoption.
    modules = loaded["snapshot"].modules
    if loaded["probe"]["mode"] != "MANAGED":
        modules = [module for module in modules if module.name in accepted]
    return ProjectSnapshot(loaded["root"], modules=modules)


def _source_checks(snapshot: ProjectSnapshot, manifest: ProjectManifest, libraries: LibraryRegistry) -> list[dict]:
    raw_issues = validate_snapshot(snapshot, include_projections=False)
    raw_issues += _schema_issues(snapshot, libraries, manifest) + _git_scope_issues(snapshot, manifest)
    raw_issues += libraries.issues + normalize_dependencies(snapshot, set(libraries.by_id))[1]
    refs, source_issues = validate_source_refs(snapshot)
    # A broken source link is unresolved work, not a reason to invent or rewrite it.
    raw_issues += [item for item in source_issues if item.code != "SOURCE_PATH_MISSING"]
    issues = [issue_to_dict(item, snapshot.root) for item in raw_issues]
    for values in refs.values():
        for ref in values:
            try:
                checked_path(snapshot.root, ref.path)
            except (OSError, ValueError) as exc:
                issues.append(issue("UNSAFE_SOURCE_PATH", str(exc), ref.path))
    return _dedupe(issues)


def _validation(root: Path, snapshot: ProjectSnapshot, manifest: ProjectManifest, libraries: LibraryRegistry) -> dict:
    # v0.3 validation may read snapshots/evidence/docs recursively. Never hand it
    # a tree containing links; report the boundary instead of following them.
    for name in ("docs", "snapshots", "evidence"):
        try:
            base = checked_path(root, name)
            if base.is_dir():
                count = 0
                for current, dirs, files in os.walk(base, followlinks=False):
                    count += 1
                    if count > 10_000:
                        raise ValueError("validation directory limit reached")
                    for item in dirs + files:
                        checked_path(root, (Path(current) / item).relative_to(root).as_posix())
        except (OSError, ValueError) as exc:
            return {"status": "FAIL", "issues": [issue("VALIDATION_UNAVAILABLE", str(exc), name)]}
    try:
        return issue_summary(validate_v03(snapshot, manifest, libraries), root)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"status": "FAIL", "issues": [issue("VALIDATION_UNAVAILABLE", str(exc))]}


def _stage(root: Path, stage: Path, snapshot: ProjectSnapshot, manifest_data: dict,
           libraries: LibraryRegistry, preserve_manifest: bool) -> tuple[list[str], dict]:
    manifest_relative = ".project/project.json"
    if preserve_manifest:
        target = stage / manifest_relative
        target.parent.mkdir(parents=True)
        target.write_bytes(read_bounded(checked_path(root, manifest_relative)))
    else:
        atomic_write_json(stage / manifest_relative, manifest_data)
    stage_snapshot = ProjectSnapshot(stage)
    for module in snapshot.modules:
        module_relative = module.path.relative_to(root).as_posix()
        stage_module = ModuleSnapshot(module.name, stage / module_relative)
        stage_module.path.mkdir(parents=True, exist_ok=True)
        for original_records, staged_records, parser in ((module.sklts, stage_module.sklts, parse_sklt), (module.glifs, stage_module.glifs, parse_glif)):
            for record in original_records:
                relative = record.path.relative_to(root).as_posix()
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(read_bounded(checked_path(root, relative)))
                parsed, parse_issues = parser(target)
                if parse_issues:
                    raise ValueError(f"structural input changed or failed parsing during staging: {relative}")
                staged_records.append(parsed)
        stage_snapshot.modules.append(stage_module)
    stage_manifest, manifest_issues = load_manifest(stage)
    if manifest_issues:
        raise ValueError("staged manifest failed v0.3 validation")
    projection_issues = write_projections(stage_snapshot)
    if projection_issues:
        raise ValueError("staged structural projections failed v0.3 validation")
    index_data, index_issues = build_object_index(stage_snapshot, stage_manifest, libraries)
    if any(severity_for(item.code) == "ERROR" for item in index_issues):
        raise ValueError("staged canonical object index failed v0.3 validation")
    write_object_index(stage, index_data)
    state = checked_path(root, ".project/state.json", output=True)
    if state.is_file():
        (stage / ".project/state.json").write_bytes(read_bounded(state))
    input_hash = structural_hash(stage_snapshot, stage_manifest)
    for name, relative in (("exp", "<module>/.exp"), ("grade", "docs/.grade"), ("objects_index", ".project/objects.json")):
        state_path = stage / ".project/state.json"
        data = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        artifact = data.get("artifacts", {}).get(name, {})
        if not isinstance(artifact, dict) or artifact.get("input_hash") != input_hash or artifact.get("path") != relative:
            mark_artifact(stage, name, input_hash, relative)
    outputs = [".project/project.json", ".project/objects.json", ".project/state.json", "docs/.grade"]
    outputs += [module.exp_path.relative_to(stage).as_posix() for module in stage_snapshot.modules]
    return sorted(set(outputs)), index_data


def _atomic_bytes(root: Path, relative: str, content: bytes, expected: bytes | None) -> None:
    atomic_bytes(root, root / relative, content, expected=expected, exclusive=expected is None)


def adoption_apply(root: str | os.PathLike, plan: dict, accepted_modules: list) -> dict:
    """Explicitly apply an unchanged plan and accepted candidates.

    Stage canonical v0.3 outputs, recheck inputs, then publish with rollback on
    errors. No SKLT, source, GLIF, Git repository, docs prose, trace or evidence
    is created/rewritten. The receipt enables identical-plan no-write replays.
    Declaration adoption is deliberately blocked pending an explicit adapter.
    """
    try:
        if not isinstance(plan, dict) or plan.get("schema") != PLAN_SCHEMA:
            return _failure("INVALID_ADOPTION_PLAN", "provide an unchanged adoption_plan result")
        body = {key: value for key, value in plan.items() if key != "plan_id"}
        if plan.get("plan_id") != "adopt-" + _hash(body):
            return _failure("TAMPERED_ADOPTION_PLAN", "plan_id does not match plan contents")
        accepted = _accepted(plan, accepted_modules)
        loaded = _load(root)
        if loaded["root"] is None or plan.get("project_root") != str(loaded["root"]):
            return _failure("ADOPTION_ROOT_MISMATCH", "plan must target this accessible absolute root", issues=loaded["probe"]["issues"])
        project_root = loaded["root"]
        receipt_path = checked_path(project_root, RECEIPT_PATH, output=True)
        if receipt_path.is_file():
            receipt = json.loads(read_bounded(receipt_path).decode("utf-8"))
            if isinstance(receipt, dict) and receipt.get("schema") == RECEIPT_SCHEMA and receipt.get("plan") == plan:
                if receipt.get("accepted_modules") != accepted:
                    return _failure("ADOPTION_SELECTION_CONFLICT", "this plan was already applied with a different selection; replan")
                current_hash, _ = _fingerprint(loaded, _candidate_modules(loaded))
                if current_hash != receipt.get("post_fingerprint"):
                    return _failure("STALE_ADOPTION_PLAN", "project changed after this plan was applied; replan")
                libraries, safety_issues = _safe_libraries(project_root)
                validation = _validation(project_root, loaded["snapshot"], loaded["manifest"], libraries)
                return {"status": "PASS", "result": "ALREADY_APPLIED", "applied": False, "idempotent": True,
                        "plan_id": plan["plan_id"], "project_root": str(project_root), "mode": loaded["probe"]["mode"],
                        "project_mode": loaded["probe"]["project_mode"], "accepted_modules": accepted, "files_changed": [],
                        "object_count": len(loaded["objects"]), "validation": validation,
                        "issues": _dedupe(safety_issues + validation["issues"]), "unresolved_issues": validation["issues"]}
        fresh = adoption_plan(project_root, plan.get("project_id"))
        if fresh != plan:
            code = "STALE_ADOPTION_PLAN" if fresh.get("input_fingerprint") != plan.get("input_fingerprint") else "TAMPERED_ADOPTION_PLAN"
            return _failure(code, "plan no longer matches discovery, configuration and output preconditions; replan")
        if plan["mode"] in {"INVALID_MANAGED", "UNAVAILABLE"} or not plan["counts_exact"]:
            return _failure("ADOPTION_UNAVAILABLE", "repair configuration/discovery before applying", issues=plan["issues"])
        effective_ids = sorted(item["id"] for item in plan["candidate_modules"]) if plan["mode"] == "MANAGED" else accepted
        selected_candidates = [item for item in plan["candidate_modules"] if item["id"] in effective_ids]
        unsupported = [item for item in selected_candidates if not item["adoptable"]]
        if unsupported:
            return _failure("UNSUPPORTED_ADOPTION_DIALECT", "selected modules require canonical SKLT or an explicitly reviewed manifest dialect adapter; no fields will be invented",
                            issues=[item for item in plan["issues"] if item["code"] == "UNSUPPORTED_ADOPTION_DIALECT"])
        output_paths = set(_output_paths(selected_candidates)) | {RECEIPT_PATH}
        conflicts = [item for item in plan["conflicts"] if item["code"] != "DERIVED_OUTPUT_CONFLICT" or item["path"] in output_paths]
        if conflicts:
            return _failure("ADOPTION_CONFLICT", "resolve the reported conflicts before applying", issues=conflicts)
        snapshot = _selected_snapshot(loaded, accepted)
        libraries, safety_issues = _safe_libraries(project_root)
        blocking = safety_issues + _source_checks(snapshot, loaded["manifest"], libraries)
        if any(item["severity"] == "ERROR" for item in blocking):
            return _failure("ADOPTION_VALIDATION_FAILED", "selected canonical objects failed v0.3 source validation", issues=blocking)
        manifest_data = loaded["raw_manifest"] if plan["mode"] == "MANAGED" else {
            "schema_version": 1, "project_id": plan["project_id"],
            "modules": [{"id": item["id"], "path": item["path"], **({"recursive": False} if not item["recursive"] else {})} for item in selected_candidates],
            "git_scopes": [{"id": "main", "mode": "MONOREPO", "repository": ".", "root": "."}],
            "reserved_dirs": sorted(DEFAULT_RESERVED), "ignore": [], "docs": {},
        }
        with output_boundary(project_root, [project_root / p for p in output_paths | {".project/.adoption.lock"}]) as guard, tempfile.TemporaryDirectory(prefix="dungeon-adoption-") as temporary_dir:
            stage = Path(temporary_dir)
            outputs, index_data = _stage(project_root, stage, snapshot, manifest_data, libraries, plan["mode"] == "MANAGED")
            # Generation is complete; revalidate all inputs before the first root write.
            if adoption_plan(project_root, plan["project_id"]) != plan:
                return _failure("STALE_ADOPTION_PLAN", "inputs changed during staging; replan")
            replacements = {relative: read_bounded(stage / relative) for relative in outputs}
            before: dict[str, bytes | None] = {}
            for relative in outputs + [RECEIPT_PATH]:
                path = checked_path(project_root, relative, output=True)
                before[relative] = read_bounded(path) if path.is_file() else None
            created_dirs: list[Path] = []
            written: dict[str, bytes] = {}
            lock_fd: int | None = None
            lock_path = checked_path(project_root, ".project/.adoption.lock", output=True)
            try:
                # All directories/paths were checked before creating the exclusive lock.
                for relative in outputs + [RECEIPT_PATH]:
                    parent = checked_path(project_root, relative, output=True).parent
                    guard.directory(parent)
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                for relative, content in replacements.items():
                    if before[relative] != content:
                        _atomic_bytes(project_root, relative, content, before[relative])
                        written[relative] = content
                after = _load(project_root)
                if after["probe"]["mode"] != "MANAGED":
                    raise ValueError("published manifest did not yield a valid managed configuration")
                if set(after["objects"]) != set(index_data["objects"]) or after["duplicates"]:
                    raise ValueError("published catalog differs from staged stable object identities")
                validation = _validation(project_root, after["snapshot"], after["manifest"], libraries)
                post_fingerprint, _ = _fingerprint(after, _candidate_modules(after))
                receipt = {"schema": RECEIPT_SCHEMA, "plan": plan, "accepted_modules": accepted, "post_fingerprint": post_fingerprint}
                receipt_bytes = (json.dumps(receipt, indent=2, ensure_ascii=True) + "\n").encode()
                _atomic_bytes(project_root, RECEIPT_PATH, receipt_bytes, before[RECEIPT_PATH])
                written[RECEIPT_PATH] = receipt_bytes
                return {"status": "PASS", "result": "APPLIED", "applied": True, "idempotent": False,
                        "plan_id": plan["plan_id"], "project_root": str(project_root), "mode": "MANAGED", "project_mode": "DUNGEON",
                        "accepted_modules": accepted, "registered_modules": effective_ids, "object_count": len(index_data["objects"]),
                        "files_changed": sorted(written), "validation": validation,
                        "issues": _dedupe(blocking + validation["issues"]), "unresolved_issues": validation["issues"],
                        "next_steps": ["Use project_catalog for managed navigation; resolve remaining documentation/source/snapshot validation issues explicitly."]}
            except BaseException as exc:
                rollback_issues: list[dict] = [
                    issue("ADOPTION_ROLLBACK_CONFLICT", item.get("error", str(exc)), item["path"])
                    for item in getattr(exc, "residual", [])
                ]
                for relative, content in reversed(list(written.items())):
                    try:
                        path = checked_path(project_root, relative, output=True)
                        if read_bounded(path) != content:
                            raise ValueError("concurrent output change; cannot safely roll it back")
                        if before[relative] is None:
                            path.unlink()
                        else:
                            _atomic_bytes(project_root, relative, before[relative], content)
                    except (OSError, ValueError) as rollback_exc:
                        rollback_issues.append(issue("ADOPTION_ROLLBACK_CONFLICT", str(rollback_exc), relative))
                if isinstance(exc, (KeyboardInterrupt, SystemExit)) and not rollback_issues:
                    raise
                result = _failure("ADOPTION_PUBLISH_FAILED", str(exc), issues=rollback_issues)
                result["residual_files"] = [item["path"] for item in rollback_issues]
                return result
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)
                    lock_path.unlink(missing_ok=True)
                for parent in reversed(created_dirs):
                    try:
                        parent.rmdir()  # Remove only empty directories created by this attempt.
                    except OSError:
                        pass
                guard.remove_empty_created()
    except UnsafeOutputPath as exc:
        return _failure("UNSAFE_OUTPUT_PATH", str(exc))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        return _failure("INVALID_ADOPTION_REQUEST", str(exc))
