"""Task/source/evidence metadata; discovery and execution remain external.

Reads never create .project. Writes touch only .project/task-traces and
.project/coverage, including in SKLT_ONLY projects explicitly opened for writes
by the caller. The CLI/MCP layer owns writable-root authorization.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from functools import wraps
from itertools import islice
from pathlib import Path
from typing import Any

from .v03_evidence import (
    EVIDENCE_ID_RE, OBJECT_ID_RE, RESULT_RE, TASK_ID_RE, EvidenceError,
    MAX_METADATA_BYTES, REVISION_RE, _digest, _git_state, _load_json,
    evidence_applicability, file_sha256, inspect_evidence, manifest_path,
    project_path, seal_path,
)
from .v03_sources import SOURCE_REF_RE
from .v041_ids import valid_object_id
from .v041_paths import UnsafeOutputPath, atomic_bytes, output_boundary
from .v041_proof import evaluate_proof
from .v04_sklt import checked_root

TRACE_FORMAT = "dungeon.task-trace.v1"
COVERAGE_FORMAT = "dungeon.coverage.v1"
SCOPES = ("PROTOTYPE", "PRODUCT")
COVERAGE_STATES = ("UNKNOWN", "NOT_STARTED", "PARTIAL", "IMPLEMENTED", "VERIFIED")
MAX_ITEMS = 256
MAX_RECORDS = 2000
MAX_RESPONSE_BYTES = 1024 * 1024


class TraceError(ValueError):
    def __init__(self, code: str, message: str, **details: Any):
        super().__init__(message)
        self.code, self.details = code, details


def _issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    return {"code": code, "severity": "ERROR", "message": message, **details}


def _api(function):
    @wraps(function)
    def call(root, *args, **kwargs):
        try:
            root = Path(root)
            if not root.is_absolute() or not root.is_dir():
                raise TraceError("UNAVAILABLE", "root must be an existing absolute project directory")
            boundary = output_boundary(root, [root / ".project/task-traces/.preflight", root / ".project/coverage/.preflight"]) if function.__name__ in ("task_trace_write", "coverage_set") else nullcontext()
            with boundary:
                result = function(checked_root(root), *args, **kwargs)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise TraceError("RESPONSE_TOO_LARGE", "select fewer objects or a smaller task list page")
            return result
        except UnsafeOutputPath as exc:
            return {"status": "FAIL", "issues": [_issue("UNSAFE_OUTPUT_PATH", str(exc))]}
        except TraceError as exc:
            return {"status": "FAIL", "issues": [_issue(exc.code, str(exc))], **exc.details}
        except (EvidenceError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            return {"status": "FAIL", "issues": [_issue("INVALID_TRACE_METADATA", str(exc))]}
    return call


def _project_catalog(root: Path) -> dict[str, Any]:
    try:
        module = importlib.import_module(".v04_projects", __package__)
    except ImportError as exc:
        raise TraceError("CATALOG_UNAVAILABLE", "v04_projects.project_catalog is not available") from exc
    return module.project_catalog(root)


def _catalog(root: Path) -> dict[str, Any]:
    catalog = _project_catalog(root)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("objects"), dict):
        raise TraceError("INVALID_CATALOG", "project_catalog must return an objects mapping")
    if len(catalog["objects"]) > MAX_RECORDS:
        raise TraceError("CATALOG_TOO_LARGE", f"catalog exceeds {MAX_RECORDS} objects")
    issues = catalog.get("issues", [])
    if not isinstance(issues, list) or any(not isinstance(item, dict) for item in issues):
        raise TraceError("INVALID_CATALOG", "catalog issues must be structured records")
    if any(catalog.get(key) in ("INVALID_MANAGED", "UNAVAILABLE") for key in ("mode", "project_mode")):
        raise TraceError("CATALOG_ISSUES", "repair the managed configuration or unavailable project root before linkage", catalog_issues=issues)
    if not isinstance(catalog.get("duplicates", {}), dict):
        raise TraceError("INVALID_CATALOG", "catalog duplicates must map ambiguous object_ids")
    # A catalog can retain valid objects alongside unsupported metadata files.
    # Applicability is checked on the selected record, not on global severity.
    return catalog


def _identity(value: Any, *, task: bool = False) -> str:
    valid = isinstance(value, str) and TASK_ID_RE.fullmatch(value) if task else valid_object_id(value)
    if not valid:
        raise TraceError("INVALID_TASK_ID" if task else "INVALID_OBJECT_ID", "identity must be a stable external ID")
    return value


def _object(catalog: dict[str, Any], identity: str) -> dict[str, Any]:
    _identity(identity)
    record = catalog["objects"].get(identity)
    if not isinstance(record, dict) or record.get("object_id") != identity:
        raise TraceError("UNKNOWN_OBJECT_ID", f"object_id is absent or ambiguous: {identity}")
    if record.get("dialect") == "DECLARATIONS" and record.get("kind") not in {
        "project", "module", "capability", "command", "action",
    }:
        raise TraceError("UNSUPPORTED_OBJECT_KIND",
                         "Trace/coverage linkage does not support this declaration kind; navigation may still inspect it",
                         object_id=identity, kind=record.get("kind"))
    issues = catalog.get("issues", [])
    occurrences = record.get("occurrences", [])
    if (
        record.get("identity_conflict")
        or identity in catalog.get("duplicates", {})
        or (isinstance(occurrences, list) and len(occurrences) > 1)
        or any(item.get("code") == "DUPLICATE_OBJECT_ID" and item.get("object_id") == identity for item in issues)
    ):
        raise TraceError("AMBIGUOUS_OBJECT_ID", f"select an unambiguous catalog object_id: {identity}", catalog_issues=issues)
    selected_issues = [
        item for item in issues
        if item.get("severity", "ERROR") == "ERROR" and (
            item.get("object_id") == identity
            or (item.get("object_id") is None and item.get("path") and item.get("path") == record.get("sklt"))
        )
    ]
    if selected_issues:
        raise TraceError("OBJECT_CATALOG_ISSUES", f"resolve catalog errors for selected object_id: {identity}", catalog_issues=issues)
    return record


def _scope(value: Any) -> str:
    if value not in SCOPES:
        raise TraceError("INVALID_SCOPE", "scope must be PROTOTYPE or PRODUCT")
    return value


def _array(value: Any, label: str) -> list:
    if not isinstance(value, list) or len(value) > MAX_ITEMS:
        raise TraceError("INVALID_TRACE_METADATA", f"{label} must be an array of at most {MAX_ITEMS} entries")
    return value


def _refs(root: Path, values: Any, label: str) -> list[dict[str, Any]]:
    """Accept catalog SourceRef dictionaries and explicit path strings."""
    if values is None or values == "":
        return []
    if isinstance(values, dict):
        values = [values]
    elif isinstance(values, str):
        if "path:" in values.lower():
            values = [{key: match.group(key) for key in ("path", "symbol", "span") if match.group(key)} for match in SOURCE_REF_RE.finditer(values)]
            if not values:
                raise TraceError("INVALID_SOURCE_REF", f"cannot parse {label}")
        else:
            values = [values]
    result: list[dict[str, Any]] = []
    for value in _array(values, label):
        record = {"path": value} if isinstance(value, str) else dict(value) if isinstance(value, dict) else None
        if record is None or "path" not in record:
            raise TraceError("INVALID_SOURCE_REF", f"{label} entries require a local path")
        record["path"] = project_path(root, record["path"]).relative_to(root).as_posix()
        for key in ("symbol", "span", "provenance", "type"):
            if key in record and (not isinstance(record[key], str) or len(record[key]) > 4096):
                raise TraceError("INVALID_SOURCE_REF", f"{label}.{key} must be a bounded string")
        if "hash" in record and "sha256" not in record:
            record["sha256"] = record.pop("hash")
        if "sha256" in record:
            record["sha256"] = _digest(record["sha256"])
        if "source_revision" in record and "revision" not in record:
            record["revision"] = record.pop("source_revision")
        if "revision" in record and record["revision"] is not None and (not isinstance(record["revision"], str) or not REVISION_RE.fullmatch(record["revision"])):
            raise TraceError("INVALID_SOURCE_REF", "revision must be a full Git revision or null")
        result.append(record)
    return result


def _evidence_ids(values: Any) -> list[str]:
    result = []
    for value in _array(values, "evidence_refs"):
        identity = value.get("evidence_id", value.get("id")) if isinstance(value, dict) else value
        if not isinstance(identity, str) or len(identity) > 128 or not EVIDENCE_ID_RE.fullmatch(identity):
            raise TraceError("INVALID_EVIDENCE_ID", "evidence_refs must contain EV-<stable-id> references")
        result.append(identity)
    if len(set(result)) != len(result):
        raise TraceError("INVALID_EVIDENCE_ID", "duplicate evidence references")
    return result


def _trace_path(root: Path, task_id: str) -> Path:
    _identity(task_id, task=True)
    name = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return project_path(root, f".project/task-traces/{name}.json")


def _coverage_path(root: Path, object_id: str, scope: str) -> Path:
    _identity(object_id)
    _scope(scope)
    name = hashlib.sha256(f"{object_id}\x00{scope}".encode("utf-8")).hexdigest()
    return project_path(root, f".project/coverage/{name}.json")


def _hash(path: Path) -> str:
    return f"sha256:{file_sha256(path, maximum=MAX_METADATA_BYTES)}"


@contextmanager
def _locked(root: Path, path: Path):
    lock = path.with_name(f".{path.name}.lock")
    with output_boundary(root, [path, lock]) as boundary:
        boundary.directory(path.parent)
        lock = boundary.inspect(lock)
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise TraceError("CONFLICT", "metadata is being updated; retry after its writer finishes") from exc
        os.close(descriptor)
        try:
            yield
        finally:
            boundary.inspect(lock).unlink(missing_ok=True)


def _write(root: Path, path: Path, payload: dict[str, Any], *, guard: bool = False, expected_hash: str | None = None) -> str:
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    if len(encoded) > MAX_METADATA_BYTES:
        raise TraceError("METADATA_TOO_LARGE", f"metadata exceeds {MAX_METADATA_BYTES} bytes")
    with _locked(root, path):
        current = path.read_bytes() if path.is_file() else None
        current_hash = f"sha256:{hashlib.sha256(current).hexdigest()}" if current is not None else None
        if guard and ((current_hash is not None and expected_hash != current_hash) or (current_hash is None and expected_hash is not None)):
            raise TraceError("CONFLICT", "expected_hash must match the current trace before an update", current_hash=current_hash)
        atomic_bytes(root, path, encoded, expected=current)
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

def _read_trace(root: Path, task_id: str) -> tuple[dict[str, Any], str]:
    path = _trace_path(root, task_id)
    if not path.is_file():
        raise TraceError("TASK_NOT_FOUND", f"task trace does not exist: {task_id}")
    data = _load_json(path, "task trace")
    if data.get("format") != TRACE_FORMAT or data.get("task_id") != task_id:
        raise TraceError("INVALID_TRACE_METADATA", "task trace identity or format does not match its path")
    return data, _hash(path)


def _check_id(value: Any) -> str:
    identity = value.get("id") if isinstance(value, dict) else value
    if not isinstance(identity, str) or not identity.strip() or len(identity) > 256:
        raise TraceError("INVALID_CHECK", "required_checks entries need a bounded check id")
    return identity


def _normal_trace(root: Path, trace: Any, catalog: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(trace, dict):
        raise TraceError("INVALID_TRACE_METADATA", "trace must be a JSON object")
    allowed = {"format", "task_id", "object_ids", "scope", "assigned_agent", "source_refs", "test_refs", "required_checks", "performed_checks", "evidence_refs", "status", "actual_result", "result", "updated_at"}
    if set(trace) - allowed:
        raise TraceError("INVALID_TRACE_METADATA", f"unsupported trace fields: {sorted(set(trace) - allowed)}")
    identity = _identity(trace.get("task_id"), task=True)
    object_ids = _array(trace.get("object_ids"), "object_ids")
    if not object_ids:
        raise TraceError("UNKNOWN_OBJECT_ID", "trace must bind at least one catalog object")
    for object_id in object_ids:
        _object(catalog, object_id)
    if len(set(object_ids)) != len(object_ids):
        raise TraceError("INVALID_OBJECT_ID", "trace contains duplicate object_ids")
    scope = _scope(trace.get("scope"))
    agent = trace.get("assigned_agent")
    if agent is not None and (not isinstance(agent, str) or not agent.strip() or len(agent) > 2048):
        raise TraceError("INVALID_AGENT_REF", "assigned_agent is only a bounded external reference string")
    status = trace.get("status", "UNKNOWN")
    if not isinstance(status, str) or len(status) > 64 or not RESULT_RE.fullmatch(status):
        raise TraceError("INVALID_TASK_STATUS", "status must be an external uppercase status token")
    required = _array(trace.get("required_checks", []), "required_checks")
    check_ids = [_check_id(value) for value in required]
    if len(set(check_ids)) != len(check_ids):
        raise TraceError("INVALID_CHECK", "required_checks contain duplicate ids")
    performed = _array(trace.get("performed_checks", []), "performed_checks")
    evidence_ids = _evidence_ids(trace.get("evidence_refs", []))
    for evidence_id in evidence_ids:
        evidence, issues = inspect_evidence(root, evidence_id)
        if evidence is None or evidence.get("_evidence_state") == "BROKEN":
            raise TraceError("INVALID_EVIDENCE_LINK", f"evidence cannot be verified: {evidence_id}", evidence_issues=[str(item.message) for item in issues])
        if not set(object_ids).intersection(evidence["objects"]):
            raise TraceError("EVIDENCE_OBJECT_MISMATCH", f"evidence does not cover any task object: {evidence_id}")
        if evidence.get("task_id", identity) != identity or evidence.get("scope", scope) != scope:
            raise TraceError("EVIDENCE_TASK_MISMATCH", f"evidence belongs to another task or scope: {evidence_id}")
    if "result" in trace and "actual_result" in trace and trace["result"] != trace["actual_result"]:
        raise TraceError("INVALID_TRACE_METADATA", "result and actual_result disagree")
    return {
        "format": TRACE_FORMAT, "task_id": identity, "object_ids": list(object_ids), "scope": scope,
        "assigned_agent": agent, "source_refs": _refs(root, trace.get("source_refs", []), "source_refs"),
        "test_refs": _refs(root, trace.get("test_refs", []), "test_refs"), "required_checks": required,
        "performed_checks": performed, "evidence_refs": evidence_ids, "status": status,
        "actual_result": trace.get("actual_result", trace.get("result")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


@_api
def task_trace_write(root, trace, expected_hash=None) -> dict:
    catalog = _catalog(root)
    data = _normal_trace(root, trace, catalog)
    if expected_hash is not None:
        expected_hash = f"sha256:{_digest(expected_hash)}"
    digest = _write(root, _trace_path(root, data["task_id"]), data, guard=True, expected_hash=expected_hash)
    return {"status": "PASS", "issues": catalog.get("issues", []), "trace": data, "hash": digest}


@_api
def task_trace_get(root, task_id) -> dict:
    data, digest = _read_trace(root, task_id)
    return {"status": "PASS", "issues": [], "trace": data, "hash": digest}


@_api
def task_trace_list(root, object_id=None, offset=0, limit=50) -> dict:
    if object_id is not None:
        _identity(object_id)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or offset > MAX_RECORDS:
        raise TraceError("INVALID_PAGINATION", f"offset must be between 0 and {MAX_RECORDS}")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise TraceError("INVALID_PAGINATION", "limit must be between 1 and 100")
    directory = project_path(root, ".project/task-traces")
    paths = list(islice(directory.glob("*.json"), MAX_RECORDS + 1)) if directory.is_dir() else []
    if len(paths) > MAX_RECORDS:
        raise TraceError("METADATA_TOO_LARGE", "too many task trace records")
    records: list[tuple[str, Path]] = []
    issues: list[dict[str, Any]] = []
    for path in sorted(paths):
        try:
            data = _load_json(project_path(root, str(path)), "task trace")
            if _trace_path(root, data.get("task_id")) != path or data.get("format") != TRACE_FORMAT or not isinstance(data.get("object_ids"), list):
                raise TraceError("INVALID_TRACE_METADATA", "trace identity or object_ids do not match the stored record")
            if object_id is None or object_id in data["object_ids"]:
                records.append((data["task_id"], path))
        except (TraceError, EvidenceError) as exc:
            issues.append(_issue("INVALID_TRACE_METADATA", str(exc)))
    records.sort(key=lambda record: record[0])
    page, byte_count = [], 0
    # Keep only the requested page in memory, bounded by bytes and count.
    for task_id, _ in records[offset:offset + limit]:
        data, _ = _read_trace(root, task_id)
        size = len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        if page and byte_count + size > MAX_RESPONSE_BYTES // 2:
            break
        page.append(data)
        byte_count += size
    next_offset = offset + len(page)
    return {"status": "FAIL" if issues else "PASS", "issues": issues, "traces": page, "total": len(records), "offset": offset, "limit": limit, "next_offset": next_offset if next_offset < len(records) else None}


def _audit_ref(root: Path, ref: dict[str, Any], revision: str | None) -> dict[str, Any]:
    path = project_path(root, ref["path"])
    row = {**ref, "path_state": "CURRENT" if path.is_file() else "MISSING", "hash_state": "UNKNOWN", "revision_state": "UNKNOWN", "symbol_state": "UNKNOWN", "semantic_state": "UNKNOWN"}
    if row["path_state"] == "CURRENT" and "sha256" in ref:
        row["actual_sha256"] = file_sha256(path)
        row["hash_state"] = "CURRENT" if row["actual_sha256"] == ref["sha256"] else "STALE"
    if ref.get("revision") is not None and revision is not None:
        row["revision_state"] = "CURRENT" if revision == ref["revision"] else "STALE"
    states = [row["path_state"], row["hash_state"], row["revision_state"]]
    row["file_state"] = "MISSING" if "MISSING" in states else "STALE" if "STALE" in states else "CURRENT"
    row["state"] = row["file_state"]
    if row["state"] == "CURRENT" and (ref.get("symbol") or (ref.get("revision") is not None and revision is None)):
        row["state"] = "UNKNOWN"
    return row


@_api
def source_link_audit(root, object_ids=None) -> dict:
    catalog = _catalog(root)
    ids = sorted(catalog["objects"]) if object_ids is None else _array(object_ids, "object_ids")
    revision = _git_state(root)["commit"]
    results = []
    issues = list(catalog.get("issues", []))
    for identity in ids:
        record = _object(catalog, identity)
        rows = [_audit_ref(root, ref, revision) for ref in _refs(root, record.get("source", []), "catalog.source")]
        states = [row["state"] for row in rows]
        state = "MISSING" if "MISSING" in states else "STALE" if "STALE" in states else "UNKNOWN" if not states or "UNKNOWN" in states else "CURRENT"
        for row in rows:
            if row["file_state"] in ("MISSING", "STALE"):
                issues.append({"code": f"SOURCE_{row['file_state']}", "severity": "WARNING", "message": row["path"], "object_id": identity})
        results.append({"object_id": identity, "state": state, "source_refs": rows})
    return {"status": "PASS", "issues": issues, "objects": results, "semantic_validation": "UNKNOWN"}


def _proof(root: Path, record: dict[str, Any], obj: dict[str, Any], *, pins: bool) -> dict[str, Any]:
    return evaluate_proof(root, record, obj, pins=pins)


@_api
def coverage_set(root, object_id, scope, state, rationale, evidence_ids=None, task_id=None, executable_path=None) -> dict:
    catalog = _catalog(root)
    obj = _object(catalog, object_id)
    scope = _scope(scope)
    if state not in COVERAGE_STATES:
        raise TraceError("INVALID_COVERAGE_STATE", "coverage state must be UNKNOWN/NOT_STARTED/PARTIAL/IMPLEMENTED/VERIFIED")
    if not isinstance(rationale, str) or len(rationale) > 16384 or (state != "UNKNOWN" and not rationale.strip()):
        raise TraceError("RATIONALE_REQUIRED", "coverage needs an explicit bounded rationale; NOT_STARTED requires a justified observation")
    evidence_ids = _evidence_ids(evidence_ids or [])
    if task_id is not None:
        _identity(task_id, task=True)
        trace, _ = _read_trace(root, task_id)
        if object_id not in trace.get("object_ids", []) or scope != trace.get("scope"):
            raise TraceError("TASK_SCOPE_MISMATCH", "task trace does not bind this object and scope")
    if executable_path is not None:
        executable_path = project_path(root, str(executable_path)).relative_to(root).as_posix()
    record = {"format": COVERAGE_FORMAT, "object_id": object_id, "scope": scope, "state": state, "rationale": rationale, "evidence_ids": evidence_ids, "task_id": task_id, "executable_path": executable_path, "updated_at": datetime.now(timezone.utc).isoformat()}
    if state == "NOT_STARTED":
        record["observation"] = {"kind": "EXPLICIT", "rationale": rationale}
    if state == "VERIFIED":
        result = _proof(root, record, obj, pins=False)
        if result["status"] != "PASS":
            result["issues"] = [*catalog.get("issues", []), *result["issues"]]
            return result
        record.update({key: result[key] for key in ("evidence_hashes", "seal_hashes")})
        record["proof_state"] = result["proof_state"]
    digest = _write(root, _coverage_path(root, object_id, scope), record)
    return {"status": "PASS", "issues": catalog.get("issues", []), "coverage": record, "hash": digest}


@_api
def coverage_get(root, object_id=None) -> dict:
    catalog = _catalog(root)
    ids = sorted(catalog["objects"]) if object_id is None else [object_id]
    coverage, issues = {}, list(catalog.get("issues", []))
    proof_failed = False
    for identity in ids:
        obj = _object(catalog, identity)
        scopes = {"object_id": identity, "structural_state": obj.get("state")}
        for scope in SCOPES:
            path = _coverage_path(root, identity, scope)
            record = {"object_id": identity, "scope": scope, "state": "UNKNOWN", "rationale": None, "evidence_ids": [], "task_id": None, "executable_path": None}
            if path.is_file():
                record = _load_json(path, "coverage record")
                if record.get("format") != COVERAGE_FORMAT or record.get("object_id") != identity or record.get("scope") != scope or record.get("state") not in COVERAGE_STATES:
                    raise TraceError("INVALID_COVERAGE_METADATA", "stored coverage identity, format or state is invalid")
                record["hash"] = _hash(path)
                if record["state"] == "VERIFIED":
                    try:
                        proof = _proof(root, record, obj, pins=True)
                    except (TraceError, EvidenceError, OSError, ValueError, TypeError, KeyError) as exc:
                        proof = {"status": "FAIL", "proof_state": "INVALID", "issues": [_issue("INVALID_EVIDENCE_PROOF", str(exc))]}
                    record["proof_state"] = proof["proof_state"]
                    if proof["status"] != "PASS":
                        proof_failed = True
                        record["recorded_state"], record["state"] = "VERIFIED", "UNKNOWN"
                        record["verification_issues"] = proof["issues"]
                        issues.extend({**item, "object_id": identity, "scope": scope} for item in proof["issues"])
            scopes[scope] = record
        coverage[identity] = scopes
    catalog_failed = object_id is None and any(item.get("severity", "ERROR") == "ERROR" for item in catalog.get("issues", []))
    return {"status": "FAIL" if proof_failed or catalog_failed else "PASS", "issues": issues, "coverage": coverage}
