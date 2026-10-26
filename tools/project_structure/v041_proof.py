"""Current proof evaluation, independent of immutable evidence integrity.

The only positive raw adapter is ``toolkit-reliability-v2``. Its input is the
versioned evidence bundle emitted by Toolkit reliability.rs (schema_version=2,
tool_version=ai-ui-test-tool-auto-probe, contract_version=reliability-v2).
See Toolkit docs/INDEX.md, RELIABILITY.md and src/contract.rs / reliability.rs.
The CAS result envelope must have ``proof = {adapter, schema_version: 2, raw,
bindings: [{id, type, scope, action_id, operation}]}``. ``raw`` must also be an
artifact CAS reference. Every claim maps to one distinct successful canonical
assertion ActionRecord; every action in the bounded receipt is checked. This
adapter verifies correspondence, not executor authenticity or full GPU/UI
acceptance. Unversioned native trails and arbitrary PASS JSON cannot verify.

Unknown text is never parsed into positive proof. Three narrowly specified
negative audit signatures only veto claims: '<check>: FAIL/NOT_RUN/...', a
nonzero '<n> failed' test summary, and 'GPU NOT_RUN'. All other unknown raw
formats fail closed with PROOF_UNVERIFIED. Synthetic receipts in tests prove
only this plumbing; they are not factual application executions.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

from . import v03_evidence as ev
from .v03_cas import CasError, cas_object_path
from .v03_manifest import load_manifest

PROOF_STATES = ("VERIFIED", "INCOMPLETE", "CONFLICT", "STALE", "INVALID")
_PRIORITY = {state: index for index, state in enumerate(PROOF_STATES)}
RUNTIME_TYPES = frozenset(("runtime", "executable", "ui"))
_ASSERTIONS = frozenset(("assert_visible", "assert_interactable", "assert_valid_bounds", "windows_assert_automation_ids"))
_NONPASS = frozenset(("FAIL", "FAILED", "SKIPPED", "UNKNOWN", "NOT_RUN", "PARTIAL"))
_FRESHNESS = frozenset(("SOURCE_HASH_MISMATCH", "BUILD_HASH_MISMATCH", "SOURCE_UNAVAILABLE", "BUILD_UNAVAILABLE", "SOURCE_REVISION_MISMATCH", "SOURCE_REVISION_UNKNOWN"))


def skeleton_obligation_sha256(root: Path, sklt: str) -> str:
    """Hash the whole UTF-8 SKLT, canonicalizing BOM and CR/LF only.

    No field or comment is discarded: responsibility, inputs, outputs, effects,
    checks and dependencies all participate. Legacy manifests are never updated.
    """
    path = ev.project_path(root, sklt)
    with path.open("rb") as stream:
        content = stream.read(ev.MAX_METADATA_BYTES + 1)
    if len(content) > ev.MAX_METADATA_BYTES:
        raise ev.EvidenceError("SKLT obligation exceeds bounded capture size")
    canonical = content.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _status(value: Any) -> Any:
    return value.get("status") if isinstance(value, dict) else value


def _checks(values: Any, scope: str, reject, label: str) -> list[dict[str, Any]]:
    if not isinstance(values, list) or len(values) > ev.MAX_SOURCE_FILES:
        reject("INVALID", "INVALID_CHECK", f"{label} must be a bounded check array")
        return []
    rows, seen = [], set()
    for value in values:
        row = {"id": value} if isinstance(value, str) else dict(value) if isinstance(value, dict) else {}
        identity, kind, check_scope = row.get("id"), row.get("type"), row.get("scope", scope)
        if (not isinstance(identity, str) or not identity.strip() or len(identity) > 256
                or (kind is not None and (not isinstance(kind, str) or not kind or len(kind) > 64))
                or check_scope not in ("PROTOTYPE", "PRODUCT")):
            reject("INVALID", "INVALID_CHECK", f"{label} contains an invalid check identity")
            continue
        key = (identity, kind, check_scope)
        if key in seen:
            reject("INVALID", "DUPLICATE_CHECK", f"{label} repeats a check identity")
        seen.add(key)
        rows.append({**row, "scope": check_scope})
    return rows


def _negative_raw(content: bytes, claims: list[dict], reject) -> None:
    """Conservative audit vetoes; never an arbitrary-log success parser."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return
    for claim in claims:
        if claim.get("status") != "PASS":
            continue
        identity = re.escape(claim["id"])
        if re.search(rf"(?im)^\s*{identity}\s*:\s*(?:FAIL|FAILED|SKIPPED|UNKNOWN|NOT_RUN)\b", text):
            reject("CONFLICT", "PROOF_CONFLICT", "raw check status contradicts structured PASS")
    if any(c.get("status") == "PASS" for c in claims) and (
            re.search(r"\b[1-9][0-9]*\s+failed\b", text, re.IGNORECASE)
            or re.search(r"(?im)(?:^|[;\s])GPU\s+NOT_RUN\b", text)):
        reject("CONFLICT", "PROOF_CONFLICT", "raw failure/NOT_RUN audit signature contradicts PASS")


def _assertion_result(action: dict, reject) -> None:
    """Parse only the documented Toolkit assertion result shapes."""
    operation = action["action"]
    detail = action["result"].get("result")
    if not isinstance(detail, dict):
        reject("INCOMPLETE", "PROOF_UNVERIFIED", "Toolkit assertion has no structured observation")
        return
    if operation == "windows_assert_automation_ids":
        expected, missing, count = detail.get("expected"), detail.get("missing"), detail.get("actual_count")
        if isinstance(missing, list) and missing:
            reject("CONFLICT", "PROOF_CONFLICT", "raw assertion has missing automation IDs")
        if (not isinstance(expected, list) or not expected or len(expected) > ev.MAX_SOURCE_FILES
                or any(not isinstance(item, str) or not item for item in expected)
                or missing != [] or type(count) is not int or count <= 0):
            reject("INCOMPLETE", "PROOF_UNVERIFIED", "automation-ID assertion observation is incomplete")
        return
    expected_assertion = {"assert_visible": "visible", "assert_interactable": "interactable", "assert_valid_bounds": "valid_bounds"}.get(operation)
    if detail.get("passed") is False:
        reject("CONFLICT", "PROOF_CONFLICT", "raw assertion reports passed=false")
    control = detail.get("control")
    if (detail.get("assertion") != expected_assertion or detail.get("passed") is not True
            or not isinstance(control, dict) or not isinstance(control.get("automation_id"), str)
            or not control["automation_id"] or action.get("target") != control["automation_id"]):
        reject("INCOMPLETE", "PROOF_UNVERIFIED", "assertion identity/target/observation is incomplete")
        return
    if operation == "assert_visible" and control.get("visible") is not True:
        reject("CONFLICT" if control.get("visible") is False else "INCOMPLETE", "PROOF_CONFLICT" if control.get("visible") is False else "PROOF_UNVERIFIED", "visible assertion lacks visible control")
    if operation == "assert_interactable":
        interaction = detail.get("interactability")
        if not isinstance(interaction, dict) or interaction.get("interactable") is not True:
            reject("CONFLICT", "PROOF_CONFLICT", "interactable assertion has no matching observation")
    if operation == "assert_valid_bounds":
        bounds = control.get("bounds")
        if (not isinstance(bounds, dict) or any(type(bounds.get(key)) not in (int, float)
                or not math.isfinite(bounds[key]) or bounds[key] <= 0 for key in ("width", "height"))):
            reject("CONFLICT", "PROOF_CONFLICT", "valid-bounds assertion has invalid observed bounds")


def _raw_correspondence(root: Path, manifest: dict, report: dict, claims: list[dict], reject) -> None:
    artifacts = manifest["artifacts"]
    proof = report.get("proof")
    raw_ref = proof.get("raw") if isinstance(proof, dict) else None
    # Artifact names are not a trust boundary: stdout, screenshots and arbitrary
    # aliases cannot hide contradictory bytes or bypass raw correspondence.
    # Only the structured result and the adapter-bound raw digest have defined
    # purposes here. Every other digest fails closed, regardless of its name.
    candidates = {ref for name, ref in artifacts.items() if name != "result"}
    for reference in sorted(candidates):
        path = cas_object_path(root, reference)
        with path.open("rb") as stream:
            content = stream.read(ev.MAX_METADATA_BYTES + 1)
        if len(content) > ev.MAX_METADATA_BYTES:
            reject("INCOMPLETE", "PROOF_UNVERIFIED", "raw receipt exceeds adapter bound")
        else:
            _negative_raw(content, claims, reject)
        if reference != raw_ref:
            reject("INCOMPLETE", "PROOF_UNVERIFIED", "additional artifact has no supported correspondence")
    if (not isinstance(proof, dict) or proof.get("adapter") != "toolkit-reliability-v2"
            or type(proof.get("schema_version")) is not int or proof["schema_version"] != 2
            or raw_ref not in artifacts.values() or raw_ref == artifacts.get("result")):
        reject("INCOMPLETE", "PROOF_UNVERIFIED", "known adapter/version and associated CAS raw digest are required")
        return
    try:
        raw = ev._load_json(cas_object_path(root, raw_ref), "Toolkit raw receipt")
    except ev.EvidenceError:
        reject("INCOMPLETE", "PROOF_UNVERIFIED", "raw format is not supported versioned Toolkit JSON")
        return
    if (type(raw.get("schema_version")) is not int or raw["schema_version"] != 2
            or raw.get("tool_version") != "ai-ui-test-tool-auto-probe"
            or raw.get("contract_version") != "reliability-v2"
            or manifest["execution"]["provider"] != "rust_makepad_toolkit"):
        reject("INCOMPLETE", "PROOF_UNVERIFIED", "raw receipt is not the supported Toolkit schema")
        return
    actions, bindings = raw.get("actions"), proof.get("bindings")
    if (not isinstance(actions, list) or not 1 <= len(actions) <= ev.MAX_SOURCE_FILES
            or not isinstance(bindings, list) or len(bindings) != len(claims)):
        reject("INCOMPLETE", "PROOF_UNVERIFIED", "bounded raw actions and one binding per claim are required")
        return
    indexed = {}
    for action in actions:
        if (not isinstance(action, dict) or not isinstance(action.get("action_id"), str)
                or not action["action_id"] or action["action_id"] in indexed
                or not isinstance(action.get("session_id"), str) or not action["session_id"]
                or type(action.get("timestamp_unix_ms")) is not int or action["timestamp_unix_ms"] < 0
                or not isinstance(action.get("action"), str) or not isinstance(action.get("result"), dict)):
            reject("INVALID", "INVALID_RAW_RECEIPT", "invalid or duplicate Toolkit ActionRecord")
            continue
        indexed[action["action_id"]] = action
        response = action["result"]
        if (response.get("success") is False or response.get("error") is not None
                or response.get("application_healthy") is False
                or _status(response.get("result")) in _NONPASS
                or (isinstance(response.get("result"), dict) and response["result"].get("passed") is False)):
            reject("CONFLICT", "PROOF_CONFLICT", "Toolkit raw action failed or application was unhealthy")
        health = action.get("runtime_health")
        if isinstance(health, dict) and health.get("healthy") is False:
            reject("CONFLICT", "PROOF_CONFLICT", "Toolkit action records unhealthy runtime")
    keys, used = set(), set()
    for binding in bindings:
        if not isinstance(binding, dict):
            reject("INVALID", "INVALID_RAW_RECEIPT", "raw binding must be an object")
            continue
        key = (binding.get("id"), binding.get("type"), binding.get("scope"))
        if any(not isinstance(value, str) for value in key):
            reject("INCOMPLETE", "PROOF_UNVERIFIED", "raw binding needs exact id/type/scope")
            continue
        matching = [c for c in claims if (c.get("id"), c.get("type"), c.get("scope")) == key]
        action_id = binding.get("action_id")
        if not isinstance(action_id, str) or key in keys or action_id in used or len(matching) != 1:
            reject("CONFLICT", "PROOF_CONFLICT", "raw binding is duplicate or does not match a structured check")
            continue
        keys.add(key)
        used.add(action_id)
        action = indexed.get(action_id)
        if action is None or binding.get("operation") != action["action"]:
            reject("CONFLICT", "PROOF_CONFLICT", "raw binding action/operation differs from receipt")
            continue
        response = action["result"]
        # An observed UI assertion cannot prove a unit-test execution. An
        # integrity check maps only to receipt correspondence, not runtime.
        if key[1] not in RUNTIME_TYPES | {"integrity"} or action["action"] not in _ASSERTIONS:
            reject("INCOMPLETE", "PROOF_UNVERIFIED", "adapter does not support this check type or operation")
        if (response.get("success") is not True or response.get("phase") != "complete"
                or response.get("operation") != action["action"] or response.get("action_id") != action_id
                or response.get("application_healthy") is not True
                or (response.get("postcondition_required") is True and response.get("postcondition_verified") is not True)):
            reject("INCOMPLETE", "PROOF_UNVERIFIED", "canonical successful action result is incomplete")
        if action["action"] in _ASSERTIONS:
            _assertion_result(action, reject)
    if keys != {(c.get("id"), c.get("type"), c.get("scope")) for c in claims}:
        reject("INCOMPLETE", "PROOF_UNVERIFIED", "raw bindings do not cover all structured checks")


def evaluate_proof(root: Path, record: dict[str, Any], obj: dict[str, Any] | None = None, *, pins: bool = False) -> dict[str, Any]:
    """Recompute applicability from current trace/obligation/source/build/CAS.

    ``record`` uses existing coverage fields. No evidence or trace is modified.
    Result keeps PASS/FAIL and check-ID list compatibility and adds proof_state.
    """
    from . import v04_trace as trace_api

    issues, states = [], []
    manifest_hashes, seal_hashes, claims = {}, {}, []

    def reject(state, code, message, **details):
        states.append(state)
        item = {"code": code, "severity": "ERROR", "message": message, **details}
        if item not in issues:
            issues.append(item)

    try:
        root = Path(root).resolve()
        object_id, scope, task_id = record.get("object_id"), record.get("scope"), record.get("task_id")
        trace_api._identity(object_id)
        if scope is None:
            # Old v1 manifests without coverage linkage remain historical
            # integrity records, not invalid records and not execution proof.
            return {"status": "FAIL", "proof_state": "INCOMPLETE", "issues": [
                {"code": "PROOF_UNVERIFIED", "severity": "ERROR", "message": "historical evidence lacks explicit task/scope proof linkage"}],
                "checks": [], "evidence_hashes": {}, "seal_hashes": {}}
        trace_api._scope(scope)
        if obj is None:
            obj = trace_api._object(trace_api._catalog(root), object_id)
        trace = {}
        trace_hash = None
        if not task_id:
            reject("INCOMPLETE", "TASK_REQUIRED", "VERIFIED requires a linked task trace")
        else:
            try:
                trace, trace_hash = trace_api._read_trace(root, task_id)
            except trace_api.TraceError as exc:
                reject("INCOMPLETE", exc.code, str(exc))
        if trace and (object_id not in trace.get("object_ids", []) or trace.get("scope") != scope):
            reject("CONFLICT", "TASK_SCOPE_MISMATCH", "task trace does not bind this object and scope")
        if trace:
            if trace.get("status") not in ("PASS", "DONE", "COMPLETED", "VERIFIED"):
                reject("CONFLICT" if trace.get("status") in _NONPASS else "INCOMPLETE", "TASK_RESULT_CONFLICT", "task has no successful completed status")
            actual = _status(trace.get("actual_result"))
            if actual != "PASS":
                reject("CONFLICT" if actual in _NONPASS else "INCOMPLETE", "TASK_RESULT_CONFLICT", "task actual_result must be PASS")
        required = _checks(trace.get("required_checks", []), scope, reject, "required_checks")
        performed = _checks(trace.get("performed_checks", []), scope, reject, "performed_checks")
        if not required:
            reject("INCOMPLETE", "REQUIRED_CHECKS_MISSING", "task must declare non-empty required checks")
        for check in performed:
            if check.get("status") != "PASS":
                reject("CONFLICT", "TRACE_EVIDENCE_CONFLICT", "performed check did not PASS")
                reject("CONFLICT", "REQUIRED_CHECK_NOT_PASSED", "non-passing trace cannot be overridden by CAS PASS")
        evidence_ids = trace_api._evidence_ids(record.get("evidence_ids", []))
        if not evidence_ids or not set(evidence_ids).issubset(trace.get("evidence_refs", [])):
            reject("INCOMPLETE", "EVIDENCE_REQUIRED", "evidence must be linked by the selected task trace")
        refs = (trace_api._refs(root, obj.get("source", []), "catalog.source")
                + trace_api._refs(root, trace.get("source_refs", []), "source_refs")
                + trace_api._refs(root, trace.get("test_refs", []), "test_refs"))
        source_paths = list(record.get("source_paths") or [])
        executable = record.get("executable_path")
        revision = ev._git_state(root)["commit"]
        for ref in refs:
            row = trace_api._audit_ref(root, ref, revision, obj.get("git_scope") or "main")
            if row["file_state"] != "CURRENT":
                reject("STALE", "SOURCE_LINK_STALE", "declared source/test reference changed or disappeared")
            if isinstance(ref.get("source_revision"), dict) and row.get("revision_state") != "CURRENT":
                reject("INCOMPLETE", "SOURCE_REVISION_UNKNOWN", "typed source/test reference is not a current observation")
            if ref.get("type") in RUNTIME_TYPES or ref["path"].lower().endswith(".exe"):
                if executable is None:
                    executable = ref["path"]
                elif ev.project_path(root, executable) != ev.project_path(root, ref["path"]):
                    reject("CONFLICT", "BUILD_PATH_MISMATCH", "trace and coverage refer to different executables")
            else:
                source_paths.append(ref["path"])
        obligation = skeleton_obligation_sha256(root, obj["sklt"])
        project, project_issues = load_manifest(root)
        selected_scope = next((item for item in project.effective_version_scopes()
                               if item.scope_id == (obj.get("git_scope") or "main")), None)
        runtime = any(c.get("type") in RUNTIME_TYPES for c in required + performed)
        for evidence_id in evidence_ids:
            verification = ev._verify_evidence_integrity(root, evidence_id)
            for item in verification["issues"]:
                if item.get("severity", "ERROR") == "ERROR":
                    reject("STALE" if item["code"] in _FRESHNESS else "INVALID", item["code"], item["message"], evidence_id=evidence_id)
            if verification.get("integrity_state", verification["state"]) != "SEALED":
                reject("INCOMPLETE", "IMMUTABLE_EVIDENCE_REQUIRED", "intact sealed manifest and CAS are required", evidence_id=evidence_id)
            path = ev.manifest_path(root, evidence_id)
            if not path.is_file():
                continue
            digest = trace_api._hash(path)
            seal = ev.seal_path(root, evidence_id)
            seal_digest = trace_api._hash(seal) if seal.is_file() else None
            manifest_hashes[evidence_id], seal_hashes[evidence_id] = digest, seal_digest
            if pins and (record.get("evidence_hashes", {}).get(evidence_id) != digest or record.get("seal_hashes", {}).get(evidence_id) != seal_digest):
                reject("INVALID", "EVIDENCE_CHANGED", "evidence bytes differ from accepted coverage")
            data = ev._load_json(path, "evidence manifest")
            if ev._manifest_errors(data):
                continue
            if project_issues or data["project"]["id"] != project.project_id:
                reject("CONFLICT", "EVIDENCE_PROJECT_MISMATCH", "evidence belongs to another project")
            for key, expected, code in (("task_id", task_id, "EVIDENCE_TASK_MISMATCH"), ("scope", scope, "EVIDENCE_SCOPE_MISMATCH")):
                if data.get(key) != expected:
                    reject("CONFLICT", code, f"evidence {key} differs from requested proof")
            if object_id not in data["objects"]:
                reject("CONFLICT", "EVIDENCE_OBJECT_MISMATCH", "evidence does not bind the requested object")
            structural = data["structure"]["objects"].get(object_id)
            if (not isinstance(structural, dict) or structural.get("state") != obj.get("state")
                    or ev.project_path(root, structural.get("sklt")) != ev.project_path(root, obj["sklt"])):
                reject("STALE", "EVIDENCE_STRUCTURE_MISMATCH", "captured structural identity differs from current object")
            elif not structural.get("obligation_sha256"):
                reject("INCOMPLETE", "SKELETON_OBLIGATION_HASH_MISSING", "historical evidence has no obligation hash")
            elif structural["obligation_sha256"] != obligation:
                reject("STALE", "SKELETON_OBLIGATION_CHANGED", "current full SKLT obligation differs from captured contract")
            source = data.get("source", {})
            captured_revision = source.get("revision")
            if (selected_scope is None or selected_scope.backend != "GIT" or selected_scope.root != ".") and "source_revision" not in data:
                reject("CONFLICT", "SOURCE_REVISION_BACKEND_MISMATCH", "legacy root-Git evidence does not bind this VersionScope")
            if "source_revision" in data:
                # Existing receipt adapters bind Git strings. Do not silently treat a
                # Scion BLAKE3 changeset as Git or bless a dirty/unknown server base.
                from .vcs_service import object_revisions, provenance_matches
                current_versions = object_revisions(root, data["objects"])
                if not provenance_matches(data["source_revision"], current_versions):
                    reject("INCOMPLETE", "SOURCE_REVISION_UNKNOWN", "typed source revision or dirty state is unavailable")
                reject("INCOMPLETE", "VCS_PROOF_ADAPTER_UNSUPPORTED", "versioned multi-backend execution receipt adapter has not been accepted")
            if not isinstance(captured_revision, str) or not ev.REVISION_RE.fullmatch(captured_revision) or captured_revision != revision:
                reject("STALE" if captured_revision and revision else "INCOMPLETE", "SOURCE_REVISION_UNKNOWN", "exact current source revision is required")
            files = source.get("files", [])
            if not files:
                reject("INCOMPLETE", "SOURCE_HASHES_REQUIRED", "captured source files are required")
            captured_paths = {ev.project_path(root, item["path"]): item["sha256"] for item in files}
            for value in source_paths:
                if ev.project_path(root, value) not in captured_paths:
                    reject("INCOMPLETE", "SOURCE_NOT_CAPTURED", "declared source/test file was not captured")
            build = data.get("build")
            if executable is not None and (not build or ev.project_path(root, executable) != ev.project_path(root, build["path"]) or ev.file_sha256(ev.project_path(root, executable)) != build["sha256"]):
                reject("CONFLICT", "BUILD_HASH_MISMATCH", "exact requested executable path/hash is not bound")
            reference = data["artifacts"].get("result")
            if not reference:
                reject("INCOMPLETE", "STRUCTURED_RESULT_REQUIRED", "captured JSON result is required")
                continue
            report = ev._load_json(cas_object_path(root, reference), "CAS execution result")
            for key in ("task_id", "scope", "execution", "build"):
                if report.get(key) != data.get(key):
                    reject("CONFLICT", "EXECUTION_RESULT_MISMATCH", f"CAS result {key} differs from manifest")
            objects = report.get("objects")
            if not isinstance(objects, list) or any(not isinstance(x, str) for x in objects) or set(objects) != set(data["objects"]):
                reject("CONFLICT", "EXECUTION_RESULT_MISMATCH", "CAS result objects differ from manifest")
            report_source = report.get("source")
            if not isinstance(report_source, dict) or report_source.get("revision") != captured_revision:
                reject("CONFLICT", "EXECUTION_RESULT_MISMATCH", "CAS result source revision differs from manifest")
            else:
                report_files = report_source.get("files")
                if not isinstance(report_files, list) or any(not isinstance(x, dict) or not isinstance(x.get("path"), str) for x in report_files):
                    reject("CONFLICT", "EXECUTION_RESULT_MISMATCH", "CAS result lacks source hashes")
                elif {ev.project_path(root, x["path"]): ev._digest(x.get("sha256")) for x in report_files} != captured_paths:
                    reject("CONFLICT", "EXECUTION_RESULT_MISMATCH", "CAS result source hashes differ from inputs")
            result = report.get("result")
            if data["result"]["status"] != "PASS" or _status(result) != "PASS":
                reject("CONFLICT", "EXECUTION_NOT_PASSED", "manifest and report must both PASS")
            reported = _checks(result.get("checks", []) if isinstance(result, dict) else [], scope, reject, "CAS checks")
            if "checks" in data["result"]:
                declared = _checks(data["result"]["checks"], scope, reject, "manifest checks")
                if declared != reported:
                    reject("CONFLICT", "EXECUTION_RESULT_MISMATCH", "manifest checks differ from captured report")
            if not reported:
                reject("INCOMPLETE", "CHECK_RESULTS_REQUIRED", "non-empty captured checks are required")
            for check in reported:
                if check.get("status") != "PASS":
                    reject("CONFLICT", "CHECK_NOT_PASSED", "captured check did not PASS")
            claims.extend(reported)
            needs_build = runtime or any(c.get("type") in RUNTIME_TYPES for c in reported)
            if needs_build and not build:
                reject("INCOMPLETE", "RUNTIME_BUILD_EVIDENCE_MISSING", "runtime obligation needs exact build path/SHA-256")
                reject("INCOMPLETE", "EXECUTABLE_HASH_REQUIRED", "executable checks require a captured build")
            _raw_correspondence(root, data, report, reported, reject)
            # Re-read integrity and captured identities after consuming receipts.
            after = ev._verify_evidence_integrity(root, evidence_id)
            for item in after["issues"]:
                if item.get("severity", "ERROR") == "ERROR":
                    reject("STALE" if item["code"] in _FRESHNESS else "INVALID", item["code"], item["message"])
            if trace_api._hash(path) != digest or (trace_api._hash(seal) if seal.is_file() else None) != seal_digest:
                reject("INVALID", "EVIDENCE_CHANGED", "evidence changed during evaluation")
        for requirement in required:
            identity, kind, required_scope = requirement["id"], requirement.get("type"), requirement["scope"]
            reports = [c for c in claims if c["id"] == identity]
            runs = [c for c in performed if c["id"] == identity]
            if not reports:
                reject("INCOMPLETE", "REQUIRED_CHECKS_MISSING", "required check missing from report")
            if not runs:
                reject("INCOMPLETE", "REQUIRED_CHECK_NOT_PASSED", "required check missing from trace")
            # Legacy string IDs are accepted, but never erase type identity:
            # infer only from one explicitly typed performed/report pair.
            if kind is None:
                kinds = {c.get("type") for c in reports + runs}
                if len(kinds) != 1 or None in kinds:
                    reject("INCOMPLETE", "CHECK_IDENTITY_INCOMPLETE", "legacy required check needs one explicit consistent type")
                    continue
                kind = next(iter(kinds))
            for label, rows in (("report", reports), ("trace", runs)):
                if not rows:
                    reject("INCOMPLETE", "REQUIRED_CHECKS_MISSING" if label == "report" else "REQUIRED_CHECK_NOT_PASSED", f"required check missing from {label}")
                    continue
                if any(c.get("type") != kind for c in rows):
                    reject("CONFLICT", "CHECK_TYPE_MISMATCH", "required and performed/reported check types differ")
                if any(c.get("scope") != required_scope or c.get("scope") != scope for c in rows):
                    reject("CONFLICT", "CHECK_SCOPE_MISMATCH", "required and performed/reported check scopes differ")
                if not any(c.get("type") == kind and c.get("scope") == required_scope and c.get("status") == "PASS" for c in rows):
                    reject("CONFLICT", "REQUIRED_CHECK_NOT_PASSED", "exact required id/type/scope did not PASS")
        if trace_hash is not None and trace_api._read_trace(root, task_id)[1] != trace_hash:
            reject("STALE", "TASK_TRACE_CHANGED", "task trace changed during evaluation")
        if skeleton_obligation_sha256(root, obj["sklt"]) != obligation:
            reject("STALE", "SKELETON_OBLIGATION_CHANGED", "SKLT obligation changed during evaluation")
    except trace_api.TraceError as exc:
        reject("INVALID", exc.code, str(exc))
    except (ev.EvidenceError, CasError, OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
        reject("INVALID", "INVALID_EVIDENCE_PROOF", str(exc))
    state = max(states, key=_PRIORITY.get) if states else "VERIFIED"
    return {"status": "PASS" if state == "VERIFIED" else "FAIL", "proof_state": state,
            "issues": issues, "checks": sorted({c["id"] for c in claims if c.get("status") == "PASS"}),
            "evidence_hashes": manifest_hashes, "seal_hashes": seal_hashes}
