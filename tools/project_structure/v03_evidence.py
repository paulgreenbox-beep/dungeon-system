from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

from .v041_ids import OBJECT_ID_RE, valid_object_id
from .v041_paths import UnsafeOutputPath, atomic_bytes, output_boundary
from .model import Issue
from .v03_cas import CasError, cas_object_path, cas_put, cas_status, configured_store_root, evidence_root
from .v03_manifest import load_manifest
from .v03_scan import scan_project_v03
from .v03_snapshots import latest_snapshot
from .v03_toolkit import TOOLKIT_PROVIDER, _load_environment_catalog, parse_toolkit_refs
from .v03_issues import severity_for
from .validator import validate_snapshot


EVIDENCE_FORMAT = "dungeon.evidence.v1"
SEAL_FORMAT = "dungeon.seal.v1"
EVIDENCE_ID_RE = re.compile(r"^EV-[A-Za-z0-9][A-Za-z0-9._-]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
RESULT_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^[0-9a-f]{40,64}$")
MAX_METADATA_BYTES = 256 * 1024
MAX_CAPTURE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_FILES = 256


class EvidenceError(ValueError):
    pass


def project_path(root: Path, value: str) -> Path:
    """Resolve a local reference without traversal, ADS or escaping symlinks."""
    root = Path(root).resolve()
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or "\x00" in value:
        raise EvidenceError("path must be a non-empty local path of at most 4096 characters")
    raw = value.replace("\\", "/")
    windows = PureWindowsPath(raw)
    candidate = Path(raw)
    if windows.drive and not candidate.is_absolute():
        raise EvidenceError("foreign or drive-relative paths are not allowed")
    if ".." in candidate.parts or any(":" in part for part in candidate.parts[1:] if part != candidate.anchor):
        raise EvidenceError("path traversal and alternate data streams are not allowed")
    if not candidate.is_absolute():
        if any(":" in part for part in candidate.parts):
            raise EvidenceError("alternate data streams are not allowed")
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        resolved.relative_to(root)
    except (ValueError, OSError, RuntimeError) as exc:
        raise EvidenceError("path escapes the project root or cannot be resolved") from exc
    return resolved


def file_sha256(path: Path, *, maximum: int = MAX_CAPTURE_BYTES) -> str:
    """Hash actual bytes with bounded reads; return the plain SHA-256 digest."""
    try:
        if not path.is_file() or path.stat().st_size > maximum:
            raise EvidenceError(f"file is missing, not regular or exceeds {maximum} bytes: {path}")
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(min(1024 * 1024, maximum + 1 - size)):
                size += len(chunk)
                if size > maximum:
                    raise EvidenceError(f"file exceeds {maximum} bytes: {path}")
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise EvidenceError(f"file cannot be hashed: {exc}") from exc


def _digest(value: Any) -> str:
    if isinstance(value, str) and value.startswith("sha256:"):
        value = value[7:]
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise EvidenceError("sha256 must contain 64 lowercase hexadecimal characters")
    return value


def manifest_path(root: Path, evidence_id: str) -> Path:
    if not isinstance(evidence_id, str) or len(evidence_id) > 128 or not EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise EvidenceError("evidence id must match EV-<stable-id>")
    return project_path(root, f"evidence/manifests/{evidence_id}.json")


def seal_path(root: Path, evidence_id: str) -> Path:
    if not isinstance(evidence_id, str) or len(evidence_id) > 128 or not EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise EvidenceError("evidence id must match EV-<stable-id>")
    return project_path(root, f"evidence/seals/{evidence_id}.json")


def _load_json(path: Path, label: str) -> dict[str, Any]:
    def unique_fields(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise EvidenceError(f"{label} has ambiguous duplicate JSON field: {key}")
            result[key] = value
        return result
    def nonfinite(value):
        raise EvidenceError(f"{label} contains a non-finite JSON number")
    def finite_float(value):
        import math
        parsed = float(value)
        return parsed if math.isfinite(parsed) else nonfinite(value)
    try:
        with path.open("rb") as stream:
            encoded = stream.read(MAX_METADATA_BYTES + 1)
        if len(encoded) > MAX_METADATA_BYTES:
            raise EvidenceError(f"{label} exceeds {MAX_METADATA_BYTES} bytes")
        data = json.loads(encoded.decode("utf-8"), object_pairs_hook=unique_fields,
                          parse_constant=nonfinite, parse_float=finite_float)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise EvidenceError(f"{label} cannot be read: {exc}") from exc
    if not isinstance(data, dict):
        raise EvidenceError(f"{label} must be a JSON object")
    return data


def _write_json_exclusive(root: Path, path: Path, payload: dict[str, Any], *, readonly: bool = False) -> None:
    # A seal hash is authoritative; pathname chmod introduces an unsafe leaf race.
    encoded = (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise EvidenceError(f"evidence metadata exceeds {MAX_METADATA_BYTES} bytes")
    try:
        atomic_bytes(root, path, encoded, exclusive=True)
    except FileExistsError as exc:
        raise EvidenceError(f"immutable evidence file already exists: {path}") from exc

def _manifest_errors(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("format") != EVIDENCE_FORMAT:
        errors.append(f"format must be {EVIDENCE_FORMAT}")
    if not isinstance(data.get("id"), str) or not EVIDENCE_ID_RE.fullmatch(data.get("id", "")):
        errors.append("id is invalid")
    created_at = data.get("created_at")
    if not isinstance(created_at, str):
        errors.append("created_at is required")
    else:
        try:
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        except ValueError:
            errors.append("created_at must be an ISO-8601 timestamp")
    project = data.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("id"), str) or not project.get("id"):
        errors.append("project.id is required")
    objects = data.get("objects")
    if not isinstance(objects, list) or not objects or any(not valid_object_id(value) for value in objects):
        errors.append("objects must be a non-empty array of object_id strings")
    elif len(set(objects)) != len(objects):
        errors.append("objects must not contain duplicate object_ids")
    git = data.get("git")
    if not isinstance(git, dict) or (git.get("commit") is not None and not isinstance(git.get("commit"), str)) or (git.get("dirty") is not None and not isinstance(git.get("dirty"), bool)):
        errors.append("git.commit and git.dirty must be valid or null")
    structure = data.get("structure")
    if not isinstance(structure, dict) or (structure.get("snapshot") is not None and not isinstance(structure.get("snapshot"), str)):
        errors.append("structure.snapshot must be a snapshot id or null")
    elif not isinstance(structure.get("objects"), dict):
        errors.append("structure.objects must map object_ids to captured structural states")
    elif isinstance(objects, list) and all(isinstance(value, str) for value in objects) and set(structure["objects"]) != set(objects):
        errors.append("structure.objects must contain exactly the manifest object_ids")
    elif isinstance(objects, list):
        for object_id, record in structure["objects"].items():
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("state"), str)
                or record.get("state") not in {"SKLT", "RARE"}
                or not isinstance(record.get("sklt"), str)
                or not record.get("sklt")
            ):
                errors.append(f"structure.objects.{object_id} must contain a valid SKLT state and path")
            elif "obligation_sha256" in record and (not isinstance(record["obligation_sha256"], str) or not SHA256_RE.fullmatch(record["obligation_sha256"])):
                errors.append(f"structure.objects.{object_id}.obligation_sha256 must be a SHA-256 digest")
    execution = data.get("execution")
    if not isinstance(execution, dict) or any(not isinstance(execution.get(key), str) or not execution.get(key) for key in ("provider", "type", "id")):
        errors.append("execution.provider, execution.type and execution.id are required")
    elif execution.get("type") not in {"container", "tool"}:
        errors.append("execution.type must be container or tool")
    result = data.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("status"), str) or not RESULT_RE.fullmatch(result.get("status", "")):
        errors.append("result.status must be a non-empty uppercase status token")
    for section in ("inputs", "artifacts"):
        values = data.get(section)
        if not isinstance(values, dict):
            errors.append(f"{section} must be an object of names to sha256 refs")
            continue
        for name, reference in values.items():
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                errors.append(f"{section} contains an invalid name")
            if not isinstance(reference, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
                errors.append(f"{section}.{name} must be a sha256:<64 lowercase hex> reference")
    errors.extend(_link_errors(data, captured=True))
    return errors


def _draft_errors(draft: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(draft.get("id"), str) or not EVIDENCE_ID_RE.fullmatch(draft.get("id", "")):
        errors.append("id must match EV-<stable-id>")
    objects = draft.get("objects")
    if not isinstance(objects, list) or not objects or any(not valid_object_id(value) for value in objects):
        errors.append("objects must be a non-empty array of object_id strings")
    elif len(set(objects)) != len(objects):
        errors.append("objects must not contain duplicate object_ids")
    execution = draft.get("execution")
    if not isinstance(execution, dict) or any(not isinstance(execution.get(key), str) or not execution.get(key) for key in ("provider", "type", "id")):
        errors.append("execution.provider, execution.type and execution.id are required")
    elif execution.get("type") not in {"container", "tool"}:
        errors.append("execution.type must be container or tool")
    result = draft.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("status"), str) or not RESULT_RE.fullmatch(result.get("status", "")):
        errors.append("result.status must be a non-empty uppercase status token")
    for section in ("inputs", "artifacts"):
        values = draft.get(section, {})
        if not isinstance(values, dict):
            errors.append(f"{section} must be an object of names to sha256 refs")
            continue
        for name, reference in values.items():
            if not isinstance(name, str) or not NAME_RE.fullmatch(name):
                errors.append(f"{section} contains an invalid name")
            if not isinstance(reference, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
                errors.append(f"{section}.{name} must be a sha256:<64 lowercase hex> reference")
    structure = draft.get("structure", {})
    if not isinstance(structure, dict):
        errors.append("structure must be an object")
    elif structure.get("snapshot") is not None and not isinstance(structure.get("snapshot"), str):
        errors.append("structure.snapshot must be a snapshot id or null")
    errors.extend(_link_errors(draft, captured=False))
    return errors


def _link_errors(data: dict[str, Any], *, captured: bool) -> list[str]:
    errors: list[str] = []
    if "source_revision" in data:
        from .vcs_service import provenance_errors
        errors.extend(provenance_errors(data["source_revision"]))
        if not isinstance(data.get("source"), dict) or not data["source"].get("files"):
            errors.append("typed source revisions require captured source files, not only a server base")
    inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
    if "task_id" in data and (not isinstance(data["task_id"], str) or not TASK_ID_RE.fullmatch(data["task_id"])):
        errors.append("task_id must be a stable external task reference")
    if "scope" in data and data["scope"] not in ("PROTOTYPE", "PRODUCT"):
        errors.append("scope must be PROTOTYPE or PRODUCT")
    build = data.get("build")
    if "build" in data:
        if not isinstance(build, dict) or not isinstance(build.get("path"), str) or not build["path"].strip():
            errors.append("build.path is required")
        elif captured or "sha256" in build:
            try:
                _digest(build.get("sha256"))
            except EvidenceError as exc:
                errors.append(f"build.{exc}")
            else:
                if captured and (not SHA256_RE.fullmatch(build["sha256"]) or f"sha256:{_digest(build['sha256'])}" not in inputs.values()):
                    errors.append("build.sha256 must reference an executable captured in inputs")
    source = data.get("source")
    if "source" in data:
        if not isinstance(source, dict) or not isinstance(source.get("files"), list) or len(source["files"]) > MAX_SOURCE_FILES:
            errors.append(f"source.files must be an array of at most {MAX_SOURCE_FILES} files")
            return errors
        revision = source.get("revision")
        if revision is not None and (not isinstance(revision, str) or not REVISION_RE.fullmatch(revision)):
            errors.append("source.revision must be a full Git revision or null")
        if captured and ("revision" not in source or source.get("provenance") != "local-files"):
            errors.append("source must contain captured revision and local-files provenance")
        paths: list[str] = []
        for item in source["files"]:
            record = {"path": item} if isinstance(item, str) and not captured else item
            if not isinstance(record, dict) or not isinstance(record.get("path"), str) or not record["path"].strip():
                errors.append("source.files entries must contain a local path")
                continue
            paths.append(record["path"])
            if captured or "sha256" in record:
                try:
                    digest = _digest(record.get("sha256"))
                except EvidenceError as exc:
                    errors.append(f"source.files.{exc}")
                else:
                    if captured and (not SHA256_RE.fullmatch(record["sha256"]) or record.get("cas") != f"sha256:{digest}" or record["cas"] not in inputs.values()):
                        errors.append("source.files hashes must be captured CAS inputs")
        if len(set(paths)) != len(paths):
            errors.append("source.files must not repeat paths")
    return errors


def _capture_links(root: Path, draft: dict[str, Any], git: dict[str, Any], inputs: dict[str, str]) -> dict[str, Any]:
    """Capture project files, never trusting hashes supplied by a caller."""
    links = {key: draft[key] for key in ("task_id", "scope") if key in draft}
    source = draft.get("source")
    if source is not None and "revision" in source and source["revision"] != git["commit"]:
        raise EvidenceError("source revision mismatch with current Git HEAD")
    captures: list[tuple[str, Path, dict[str, str]]] = []
    for index, item in enumerate(source.get("files", []) if source else []):
        record = {"path": item} if isinstance(item, str) else item
        path = project_path(root, record["path"])
        digest = file_sha256(path)
        if "sha256" in record and _digest(record["sha256"]) != digest:
            raise EvidenceError(f"source hash mismatch: {record['path']}")
        captures.append((f"source-{index}", path, {"path": path.relative_to(root).as_posix(), "sha256": digest, "cas": f"sha256:{digest}"}))
    build = draft.get("build")
    if build is not None:
        path = project_path(root, build["path"])
        digest = file_sha256(path)
        if "sha256" in build and _digest(build["sha256"]) != digest:
            raise EvidenceError(f"executable hash mismatch: {build['path']}")
        captures.append(("executable", path, {"path": path.relative_to(root).as_posix(), "sha256": digest}))
        links["build"] = captures[-1][2]
    if len({record["path"] for _, _, record in captures if "cas" in record}) != len([record for _, _, record in captures if "cas" in record]):
        raise EvidenceError("source.files resolves to duplicate local files")
    if sum(path.stat().st_size for _, path, _ in captures) > MAX_CAPTURE_BYTES:
        raise EvidenceError(f"combined file capture exceeds {MAX_CAPTURE_BYTES} bytes")
    # Validate every file and every reserved name before publishing any inputs.
    for name, path, record in captures:
        expected = f"sha256:{record['sha256']}"
        if name in inputs and inputs[name] != expected:
            raise EvidenceError(f"reserved input '{name}' conflicts with captured bytes")
    destinations = [cas_object_path(root, f"sha256:{record['sha256']}") for _, _, record in captures]
    with output_boundary(configured_store_root(root), destinations):
        for name, path, record in captures:
            expected = f"sha256:{record['sha256']}"
            reference, _, _ = cas_put(root, path)
            if reference != expected:
                raise EvidenceError(f"file changed during evidence capture: {record['path']}")
            inputs[name] = reference
    if source is not None:
        links["source"] = {"revision": git["commit"], "provenance": "local-files", "files": [record for name, _, record in captures if name != "executable"]}
    return links


def _current_link_issues(root: Path, data: dict[str, Any], *, check_typed_revision: bool = True) -> list[Issue]:
    issues: list[Issue] = []
    for label, records in (("BUILD", [data["build"]] if "build" in data else []), ("SOURCE", data.get("source", {}).get("files", []))):
        for record in records:
            try:
                actual = file_sha256(project_path(root, record["path"]))
                if actual != _digest(record["sha256"]):
                    issues.append(Issue(f"{label}_HASH_MISMATCH", f"current bytes differ: {record['path']}"))
            except EvidenceError as exc:
                issues.append(Issue(f"{label}_UNAVAILABLE", str(exc)))
    if "source_revision" in data and check_typed_revision:
        from .vcs_service import object_revisions, provenance_matches
        try:
            current = object_revisions(root, data["objects"])
            if not provenance_matches(data["source_revision"], current):
                issues.append(Issue("SOURCE_REVISION_UNKNOWN", "typed source revisions are changed, dirty-state unknown or unavailable; not current proof"))
        except (ValueError, OSError):
            issues.append(Issue("SOURCE_REVISION_UNKNOWN", "current typed source revision cannot be established"))
    elif "source_revision" not in data and ("source" in data or "build" in data):
        revision = data["source"]["revision"] if "source" in data else data.get("git", {}).get("commit")
        if revision != data.get("git", {}).get("commit") or revision != _git_state(root)["commit"]:
            issues.append(Issue("SOURCE_REVISION_MISMATCH", "captured source revision differs from manifest Git or current HEAD"))
    return issues


def _execution_issues(root: Path, execution: dict[str, Any]) -> list[Issue]:
    provider = execution["provider"]
    kind = execution["type"]
    identity = execution["id"]
    record = SimpleNamespace(
        fields={"execution": f"provider: {provider} kind: {kind} id: {identity}"},
        path=None,
        object_id=None,
    )
    refs, parse_issues = parse_toolkit_refs(record)
    issues = list(parse_issues)
    if parse_issues or not refs:
        return issues or [Issue("INVALID_TOOLKIT_REF", "execution reference is invalid")]
    ref = refs[0]
    if provider != TOOLKIT_PROVIDER:
        return [Issue("UNRESOLVED_TOOLKIT_REF", f"external provider is not locally resolved: {provider}")]
    catalog, _, index_path = _load_environment_catalog(root)
    if catalog is None:
        location = index_path or "Rust_Makepad_Toolkit/docs/INDEX.md"
        return [Issue("UNRESOLVED_TOOLKIT_REF", f"cannot resolve Toolkit catalog through {location}")]
    if kind == "container":
        environments = catalog.get("environments", [])
        match = next((item for item in environments if isinstance(item, dict) and item.get("key") == identity), None)
        if match is None or match.get("kind") != "container":
            return [Issue("UNKNOWN_TOOLKIT_CONTAINER", f"unknown Toolkit container '{identity}'")]
        return []
    return [Issue("UNRESOLVED_TOOLKIT_REF", f"Toolkit tool id is not checked by the environment catalog: {ref.id}")]


def _git_state(root: Path) -> dict[str, Any]:
    environment = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        environment.pop(key, None)
    try:
        commit_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        if commit_result.returncode != 0:
            return {"commit": None, "dirty": None}
        status_result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        return {
            "commit": commit_result.stdout.strip(),
            "dirty": bool(status_result.stdout.strip()) if status_result.returncode == 0 else None,
        }
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None}


def _snapshot_ids(root: Path) -> set[str]:
    snapshots = root / "snapshots"
    if not snapshots.is_dir():
        return set()
    return {
        path.name
        for path in snapshots.iterdir()
        if path.is_dir() and (path / "manifest.json").is_file() and not path.name.startswith(".")
    }


def _known_objects(root: Path) -> tuple[dict[str, Any], list[Issue]]:
    # v0.4 owns discovery. Import at call time so parallel integration is safe.
    try:
        module = importlib.import_module(".v04_projects", __package__)
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}.v04_projects":
            raise
    else:
        catalog = module.project_catalog(root)
        issues = [Issue(item.get("code", "CATALOG_ERROR"), item.get("message", "project catalog issue")) for item in catalog.get("issues", []) if item.get("severity", "ERROR") == "ERROR"]
        return {identity: {"state": record.get("state"), "sklt": record.get("sklt")} for identity, record in catalog["objects"].items()}, issues
    snapshot, _, scan_issues = scan_project_v03(root)
    known = {record.object_id: {"state": record.state or "", "sklt": record.path.relative_to(root).as_posix()} for record in snapshot.sklts if record.object_id}
    issues = scan_issues + validate_snapshot(snapshot, include_projections=False)
    return known, [issue for issue in issues if severity_for(issue.code) == "ERROR"]


def create_evidence(root: Path, draft_path: Path) -> tuple[dict[str, Any] | None, list[Issue]]:
    """Preflight the manifest/seal and configured CAS before any capture write."""
    try:
        root = Path(root)
        draft = _load_json(Path(draft_path), "evidence draft")
        errors = _draft_errors(draft)
        if errors:
            return None, [Issue("INVALID_EVIDENCE_MANIFEST", message) for message in errors]
        # Inspect raw destinations before project_path/resolve can erase a link.
        destinations = [root / "evidence" / section / f"{draft['id']}.json" for section in ("manifests", "seals")]
        with output_boundary(root, destinations):
            configuration = _load_json(root / ".project/project.json", "project manifest")
            raw_store = configuration.get("evidence_store", {}).get("root")
            if not isinstance(raw_store, str) or not raw_store.strip():
                raise CasError("evidence_store.root must be a non-empty path")
            candidate = Path(os.path.expandvars(raw_store.strip())).expanduser()
            if not candidate.is_absolute():
                candidate = root / candidate
            # Normalize configured ../store lexically, preserving reparse aliases.
            raw_store_path = Path(os.path.abspath(candidate))
            with output_boundary(raw_store_path, [raw_store_path / "cas/sha256/.capture-preflight"]) as cas_guard:
                store = configured_store_root(root)
                if store != raw_store_path:
                    raise UnsafeOutputPath("configured CAS aliases another physical root")
                paths = []
                for item in draft.get("source", {}).get("files", []):
                    paths.append(item if isinstance(item, str) else item["path"])
                if "build" in draft:
                    paths.append(draft["build"]["path"])
                outputs = {f"sha256:{file_sha256(project_path(root, value))}" for value in paths}
                outputs.update(draft.get("inputs", {}).values())
                outputs.update(draft.get("artifacts", {}).values())
                for reference in outputs:
                    cas_guard.inspect(cas_object_path(root, reference))
                return _create_evidence(root, draft_path, draft=draft)
    except UnsafeOutputPath as exc:
        return None, [Issue("UNSAFE_OUTPUT_PATH", str(exc))]
    except (EvidenceError, CasError, OSError, ValueError, KeyError, TypeError) as exc:
        return None, [Issue("EVIDENCE_CAPTURE_MISMATCH", str(exc))]


def _create_evidence(root: Path, draft_path: Path, *, draft: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, list[Issue]]:
    root = Path(root).resolve()
    draft_path = Path(draft_path)
    try:
        if draft is None:
            draft = _load_json(draft_path, "evidence draft")
    except EvidenceError as exc:
        return None, [Issue("INVALID_EVIDENCE_MANIFEST", str(exc), draft_path)]
    errors = _draft_errors(draft)
    if errors:
        return None, [Issue("INVALID_EVIDENCE_MANIFEST", message, draft_path) for message in errors]

    try:
        target = manifest_path(root, draft["id"])
        if target.exists() or seal_path(root, draft["id"]).exists():
            return None, [Issue("EVIDENCE_EXISTS", f"evidence id '{draft['id']}' already exists", target)]
    except EvidenceError as exc:
        return None, [Issue("INVALID_EVIDENCE_MANIFEST", str(exc))]

    project_manifest, manifest_issues = load_manifest(root)
    if manifest_issues:
        return None, manifest_issues
    try:
        known_objects, blocking_structural = _known_objects(root)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, ImportError) as exc:
        return None, [Issue("INVALID_EVIDENCE_OBJECTS", str(exc))]
    if blocking_structural:
        return None, blocking_structural
    unknown = [object_id for object_id in draft["objects"] if object_id not in known_objects]
    if unknown:
        return None, [Issue("UNKNOWN_EVIDENCE_OBJECT", f"unknown object_id '{object_id}'") for object_id in unknown]

    toolkit_issues = _execution_issues(root, draft["execution"])
    errors = [issue for issue in toolkit_issues if issue.code != "UNRESOLVED_TOOLKIT_REF"]
    if errors:
        return None, errors

    inputs = dict(draft.get("inputs", {}))
    artifacts = dict(draft.get("artifacts", {}))
    missing: list[str] = []
    for reference in [*inputs.values(), *artifacts.values()]:
        try:
            if not verify_cas_bounded(root, reference)[0]:
                missing.append(reference)
        except CasError:
            missing.append(reference)
    if missing:
        return None, [Issue("MISSING_CAS_OBJECT", f"CAS object must be captured before evidence creation: {reference}") for reference in sorted(set(missing))]

    structure_draft = draft.get("structure", {})
    snapshot_id = structure_draft.get("snapshot")
    if snapshot_id is None:
        snapshot_id, _ = latest_snapshot(root)
    if snapshot_id is not None and snapshot_id not in _snapshot_ids(root):
        return None, [Issue("UNKNOWN_EVIDENCE_SNAPSHOT", f"unknown structural snapshot '{snapshot_id}'")]

    typed_revision = None
    if project_manifest.version_scopes or "source_revision" in draft:
        from .vcs_service import object_revisions, provenance_equivalent
        try:
            typed_revision = object_revisions(root, draft["objects"])
        except (OSError, ValueError) as exc:
            return None, [Issue("EVIDENCE_CAPTURE_MISMATCH", str(exc))]
        # Merely enabling Scion elsewhere must not change existing root-Git
        # Evidence/receipt behavior for objects still in the legacy main scope.
        if ("source_revision" not in draft and set(typed_revision["scopes"]) == {"main"}
                and typed_revision["scopes"]["main"]["backend"] == "GIT"
                and any(s.scope_id == "main" and s.backend == "GIT" and s.root == "."
                        for s in project_manifest.effective_version_scopes())):
            typed_revision = None
    if typed_revision is not None:
        if not isinstance(draft.get("source"), dict) or not draft["source"].get("files"):
            return None, [Issue("EVIDENCE_CAPTURE_MISMATCH", "VersionScope evidence requires non-empty source files captured into CAS")]
        if "source_revision" in draft and not provenance_equivalent(draft["source_revision"], typed_revision):
            return None, [Issue("EVIDENCE_CAPTURE_MISMATCH", "caller source_revision differs from captured backend state")]
    git = _git_state(root) if typed_revision is None else {"commit": None, "dirty": None}
    try:
        from .v041_proof import skeleton_obligation_sha256
        obligation_hashes = {identity: skeleton_obligation_sha256(root, known_objects[identity]["sklt"]) for identity in draft["objects"]}
        links = _capture_links(root, draft, git, inputs)
    except (EvidenceError, CasError, OSError) as exc:
        return None, [Issue("EVIDENCE_CAPTURE_MISMATCH", str(exc))]
    payload: dict[str, Any] = {
        "format": EVIDENCE_FORMAT,
        "id": draft["id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": {"id": project_manifest.project_id},
        "objects": list(draft["objects"]),
        "git": git,
        "structure": {
            "snapshot": snapshot_id,
            "objects": {object_id: {**known_objects[object_id], "obligation_sha256": obligation_hashes[object_id]} for object_id in draft["objects"]},
        },
        "execution": dict(draft["execution"]),
        "inputs": dict(inputs),
        "artifacts": dict(artifacts),
        "result": dict(draft["result"]),
        **links,
        **({"source_revision": typed_revision} if typed_revision is not None else {}),
    }
    link_issues = _current_link_issues(root, payload, check_typed_revision=False)
    if link_issues:
        return None, link_issues
    errors = _manifest_errors(payload)
    if errors:
        return None, [Issue("INVALID_EVIDENCE_MANIFEST", message) for message in errors]
    try:
        _write_json_exclusive(root, target, payload)
    except (EvidenceError, OSError) as exc:
        return None, [Issue("EVIDENCE_EXISTS", str(exc), target)]
    warnings = [issue for issue in toolkit_issues if issue.code == "UNRESOLVED_TOOLKIT_REF"]
    return payload, warnings


def inspect_evidence(root: Path, evidence_id: str) -> tuple[dict[str, Any] | None, list[Issue]]:
    try:
        path = manifest_path(root, evidence_id)
    except EvidenceError as exc:
        return None, [Issue("INVALID_EVIDENCE_ID", str(exc))]
    if not path.is_file():
        return None, [Issue("EVIDENCE_NOT_FOUND", f"evidence id '{evidence_id}' does not exist", path)]
    try:
        data = _load_json(path, "evidence manifest")
    except EvidenceError as exc:
        return None, [Issue("INVALID_EVIDENCE_MANIFEST", str(exc), path)]
    verification = verify_evidence(root, evidence_id)
    data["_evidence_state"] = verification["state"]
    data["_proof_state"] = verification["proof_state"]
    data["_proof_issues"] = verification["proof_issues"]
    if verification["issues"]:
        data["_verification_issues"] = verification["issues"]
    return data, []


def _seal_errors(root: Path, evidence_id: str, data: dict[str, Any], path: Path) -> list[Issue]:
    issues: list[Issue] = []
    try:
        seal = _load_json(path, "evidence seal")
    except EvidenceError as exc:
        return [Issue("BROKEN_EVIDENCE_SEAL", str(exc), path)]
    manifest_file = manifest_path(root, evidence_id)
    try:
        manifest_hash = _hash_bytes(manifest_file.read_bytes())
    except OSError as exc:
        return [Issue("BROKEN_EVIDENCE_SEAL", f"manifest cannot be read: {exc}", manifest_file)]
    git = data.get("git")
    expected = {
        "format": SEAL_FORMAT,
        "evidence_id": evidence_id,
        "manifest": manifest_hash,
        "git_commit": git.get("commit") if isinstance(git, dict) else None,
        "sealed": True,
    }
    for key, value in expected.items():
        if seal.get(key) != value:
            issues.append(Issue("BROKEN_EVIDENCE_SEAL", f"seal field '{key}' does not match evidence", path))
    if not isinstance(seal.get("sealed_at"), str):
        issues.append(Issue("BROKEN_EVIDENCE_SEAL", "seal has no sealed_at timestamp", path))
    return issues


def _hash_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def verify_cas_bounded(root: Path, reference: str) -> tuple[bool, str | None, int | None]:
    path = cas_object_path(root, reference)
    # The store may be external, but refs must resolve inside its configured CAS.
    try:
        path.resolve().relative_to(path.parents[2].resolve())
        actual = file_sha256(path)
        return actual == reference[7:], None if actual == reference[7:] else "CAS hash mismatch", path.stat().st_size
    except (ValueError, EvidenceError, OSError) as exc:
        return False, str(exc), None


def _verify_evidence_integrity(root: Path, evidence_id: str) -> dict[str, Any]:
    try:
        path = manifest_path(root, evidence_id)
    except EvidenceError as exc:
        return {"evidence_id": evidence_id, "state": "BROKEN", "issues": [{"code": "INVALID_EVIDENCE_ID", "message": str(exc)}]}
    if not path.is_file():
        return {"evidence_id": evidence_id, "state": "BROKEN", "issues": [{"code": "EVIDENCE_NOT_FOUND", "message": f"evidence id '{evidence_id}' does not exist"}]}
    try:
        data = _load_json(path, "evidence manifest")
    except EvidenceError as exc:
        return {"evidence_id": evidence_id, "state": "BROKEN", "issues": [{"code": "INVALID_EVIDENCE_MANIFEST", "message": str(exc)}]}
    issues = [Issue("INVALID_EVIDENCE_MANIFEST", message, path) for message in _manifest_errors(data)]
    if data.get("id") != evidence_id:
        issues.append(Issue("INVALID_EVIDENCE_MANIFEST", "manifest id does not match its filename", path))
    if not issues:
        snapshot_id = data["structure"]["snapshot"]
        if snapshot_id is not None and snapshot_id not in _snapshot_ids(root):
            issues.append(Issue("BROKEN_STRUCTURE_REFERENCE", f"structural snapshot is missing: {snapshot_id}", path))
        for reference in [*data.get("inputs", {}).values(), *data.get("artifacts", {}).values()]:
            try:
                valid, error, _ = verify_cas_bounded(root, reference)
            except CasError as exc:
                valid, error = False, str(exc)
            if not valid:
                issues.append(Issue("BROKEN_CAS_REFERENCE", f"{reference}: {error}", path))
        issues.extend(_execution_issues(root, data["execution"]))
        issues.extend(_current_link_issues(root, data))

    try:
        seal = seal_path(root, evidence_id)
    except EvidenceError as exc:
        return {"evidence_id": evidence_id, "state": "BROKEN", "issues": [{"code": "UNSAFE_EVIDENCE_PATH", "severity": "ERROR", "message": str(exc)}]}
    sealed = seal.is_file()
    if sealed:
        issues.extend(_seal_errors(root, evidence_id, data, seal))
    errors = [issue for issue in issues if issue.code != "UNRESOLVED_TOOLKIT_REF"]
    state = "BROKEN" if errors else ("SEALED" if sealed else "VERIFIED")
    freshness = {"SOURCE_HASH_MISMATCH", "BUILD_HASH_MISMATCH", "SOURCE_UNAVAILABLE", "BUILD_UNAVAILABLE", "SOURCE_REVISION_MISMATCH", "SOURCE_REVISION_UNKNOWN"}
    integrity_state = "BROKEN" if any(item.code not in freshness for item in errors) else ("SEALED" if sealed else "VERIFIED")
    return {
        "evidence_id": evidence_id,
        "state": state,
        "integrity_state": integrity_state,
        "manifest": f"evidence/manifests/{evidence_id}.json",
        "seal": f"evidence/seals/{evidence_id}.json" if sealed else None,
        "issues": [
            {
                "code": issue.code,
                "severity": "WARNING" if issue.code == "UNRESOLVED_TOOLKIT_REF" else "ERROR",
                "message": issue.message,
            }
            for issue in issues
        ],
    }


def verify_evidence(root: Path, evidence_id: str) -> dict[str, Any]:
    """Expose current proof separately; legacy state/issues retain their types."""
    from .v041_proof import PROOF_STATES, evaluate_proof
    result = _verify_evidence_integrity(root, evidence_id)
    result.setdefault("integrity_state", result["state"])
    if result["integrity_state"] == "BROKEN":
        result.update(proof_state="INVALID", proof_issues=list(result["issues"]))
        return result
    data = _load_json(manifest_path(root, evidence_id), "evidence manifest")
    proofs = [evaluate_proof(root, {"object_id": identity, "scope": data.get("scope"),
              "task_id": data.get("task_id"), "evidence_ids": [evidence_id],
              "executable_path": data.get("build", {}).get("path")}) for identity in data["objects"]]
    result["proof_state"] = max((item["proof_state"] for item in proofs), key=PROOF_STATES.index)
    result["proof_issues"] = [issue for proof in proofs for issue in proof["issues"]]
    return result


def evidence_applicability(
    root: Path, evidence_id: str, *, object_id: str, scope: str,
    task_id: str | None = None, executable_path: str | None = None,
    source_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Use exactly the same current evaluator as coverage_set/get."""
    from .v041_proof import evaluate_proof
    return evaluate_proof(root, {"object_id": object_id, "scope": scope,
                          "task_id": task_id, "evidence_ids": [evidence_id],
                          "executable_path": executable_path, "source_paths": source_paths or []})

def seal_evidence(root: Path, evidence_id: str) -> tuple[dict[str, Any] | None, list[Issue]]:
    try:
        root = Path(root)
        if not isinstance(evidence_id, str) or len(evidence_id) > 128 or not EVIDENCE_ID_RE.fullmatch(evidence_id):
            return None, [Issue("INVALID_EVIDENCE_ID", "evidence id must match EV-<stable-id>")]
        with output_boundary(root, [root / "evidence/seals" / f"{evidence_id}.json",
                                    root / "evidence/manifests" / f"{evidence_id}.json"]):
            return _seal_evidence(root, evidence_id)
    except UnsafeOutputPath as exc:
        return None, [Issue("UNSAFE_OUTPUT_PATH", str(exc))]


def _seal_evidence(root: Path, evidence_id: str) -> tuple[dict[str, Any] | None, list[Issue]]:
    verification = _verify_evidence_integrity(root, evidence_id)
    if verification["state"] == "BROKEN":
        try:
            path = manifest_path(root, evidence_id)
        except EvidenceError:
            path = None
        return None, [
            Issue(item["code"], item["message"], path)
            for item in verification["issues"]
            if item["severity"] == "ERROR"
        ]
    if verification["state"] == "SEALED":
        try:
            return _load_json(seal_path(root, evidence_id), "evidence seal"), []
        except EvidenceError as exc:
            return None, [Issue("BROKEN_EVIDENCE_SEAL", str(exc), seal_path(root, evidence_id))]

    try:
        path = manifest_path(root, evidence_id)
    except EvidenceError as exc:
        return None, [Issue("INVALID_EVIDENCE_ID", str(exc))]
    data = _load_json(path, "evidence manifest")
    try:
        manifest_hash = _hash_bytes(path.read_bytes())
    except OSError as exc:
        return None, [Issue("INVALID_EVIDENCE_MANIFEST", f"manifest cannot be read: {exc}", path)]
    seal = {
        "format": SEAL_FORMAT,
        "evidence_id": evidence_id,
        "manifest": manifest_hash,
        "git_commit": data.get("git", {}).get("commit"),
        "sealed": True,
        "sealed_at": datetime.now(timezone.utc).isoformat(),
    }
    target = seal_path(root, evidence_id)
    try:
        _write_json_exclusive(root, target, seal, readonly=True)
    except EvidenceError as exc:
        return None, [Issue("EVIDENCE_EXISTS", str(exc), target)]
    return seal, []


def evidence_for(root: Path, object_id: str) -> tuple[list[dict[str, Any]], list[Issue]]:
    if not valid_object_id(object_id):
        return [], [Issue("INVALID_OBJECT_ID", f"invalid object_id '{object_id}'")]
    manifests = evidence_root(root) / "manifests"
    if not manifests.is_dir():
        return [], []
    results: list[dict[str, Any]] = []
    for path in sorted(manifests.glob("EV-*.json")):
        try:
            data = _load_json(path, "evidence manifest")
        except EvidenceError:
            continue
        if object_id not in data.get("objects", []):
            continue
        evidence_id = data.get("id", path.stem)
        if not isinstance(evidence_id, str) or not EVIDENCE_ID_RE.fullmatch(evidence_id):
            continue
        seal = seal_path(root, evidence_id)
        verification = verify_evidence(root, evidence_id)
        results.append({
            "id": evidence_id,
            "state": verification["state"],
            "proof_state": verification["proof_state"],
            "result": data.get("result", {}).get("status"),
            "execution": data.get("execution"),
            "manifest": f"evidence/manifests/{path.name}",
            "seal": f"evidence/seals/{seal.name}" if seal.is_file() else None,
        })
    return results, []


def validate_snapshot_evidence_refs(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    snapshots = root / "snapshots"
    if not snapshots.is_dir():
        return issues
    for snapshot_dir in snapshots.iterdir():
        path = snapshot_dir / "manifest.json"
        if not snapshot_dir.is_dir() or not path.is_file() or snapshot_dir.name.startswith("."):
            continue
        try:
            data = _load_json(path, "snapshot manifest")
        except EvidenceError as exc:
            issues.append(Issue("INVALID_SNAPSHOT_MANIFEST", str(exc), path))
            continue
        references = data.get("evidence_ids", [])
        if not isinstance(references, list) or any(not isinstance(value, str) or not EVIDENCE_ID_RE.fullmatch(value) for value in references):
            issues.append(Issue("INVALID_SNAPSHOT_EVIDENCE_REF", "evidence_ids must be an array of evidence IDs", path))
            continue
        for evidence_id in references:
            if not manifest_path(root, evidence_id).is_file():
                issues.append(Issue("UNKNOWN_SNAPSHOT_EVIDENCE", f"snapshot references missing evidence '{evidence_id}'", path))
    return issues


def validate_cronica_evidence_refs(root: Path) -> list[Issue]:
    issues: list[Issue] = []
    cronica = root / "docs" / "cronica"
    if not cronica.is_dir():
        return issues
    field_pattern = re.compile(r"^\s*(?:evidence|evidence_ids)\s*:\s*(.*?)\s*$", re.IGNORECASE)
    for path in sorted(cronica.rglob("*.md")):
        if path.name.casefold() == "readme.md":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError) as exc:
            issues.append(Issue("INVALID_CRONICA_ENTRY", f"cannot read Cronica entry: {exc}", path))
            continue
        for line_number, line in enumerate(lines, 1):
            match = field_pattern.match(line)
            if not match:
                continue
            value = match.group(1)
            if value.strip().casefold() in {"", "[]", "none", "null", "not-created"}:
                continue
            ids = re.findall(r"EV-[A-Za-z0-9][A-Za-z0-9._-]*", value)
            if not ids:
                issues.append(Issue("INVALID_CRONICA_EVIDENCE_REF", "Evidence field must contain one or more EV-<stable-id> values", path, line_number))
                continue
            for evidence_id in ids:
                if not manifest_path(root, evidence_id).is_file():
                    issues.append(Issue("UNKNOWN_CRONICA_EVIDENCE", f"Cronica references missing evidence '{evidence_id}'", path, line_number))
    return issues


def evidence_store_status(root: Path) -> dict[str, Any]:
    cas = cas_status(root)
    manifests = evidence_root(root) / "manifests"
    seals = evidence_root(root) / "seals"
    manifest_count = sum(1 for _ in manifests.glob("EV-*.json")) if manifests.is_dir() else 0
    seal_count = sum(1 for _ in seals.glob("EV-*.json")) if seals.is_dir() else 0
    return {
        "cas": cas.as_dict(),
        "manifests": manifest_count,
        "sealed": seal_count,
        "unsealed": max(0, manifest_count - seal_count),
    }
