"""Small dependency-free, multi-project stdio MCP bridge for SKLT projects.

Managed projects use the Dungeon CLI from this plugin's home repository.
Uninitialized projects receive diagnosis and safe adoption plans. SKLT-only
projects support read-only navigation and explicitly authorized trace metadata.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any


VERSION = "0.4.2-rc1"
sys.dont_write_bytecode = True
PROTOCOL_VERSION = "2025-11-25"
HANDSHAKE_VERSIONS = {"2025-11-25", "2025-06-18", "2025-03-26"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
EVIDENCE_ID_RE = re.compile(r"^EV-[A-Za-z0-9][A-Za-z0-9._-]*$")
MAX_RESPONSE_BYTES = 262_144
SKLT_SCAN_LIMIT = 10_000
SKLT_SKIP_DIRS = {
    ".git", ".project", ".venv", "node_modules", "snapshots", "target", "build", "dist",
    "__pycache__", "assets", "docs", "evidence", "libs", "logs", "references", "tests", "tools",
}


@dataclass(frozen=True)
class ProjectContext:
    root: Path
    mode: str
    manifest: dict[str, Any] | None = None
    diagnosis: dict[str, Any] | None = None


@dataclass(frozen=True)
class RequestPolicy:
    """Operator-supplied HTTP capability, never populated from tool arguments."""

    project_roots: tuple[str, ...]
    writable_roots: tuple[str, ...] = ()
    input_roots: tuple[str, ...] = ()


_REQUEST_POLICY: ContextVar[RequestPolicy | None] = ContextVar("dungeon_request_policy", default=None)


@contextlib.contextmanager
def request_policy(policy: RequestPolicy):
    token = _REQUEST_POLICY.set(policy)
    try:
        yield
    finally:
        _REQUEST_POLICY.reset(token)


def _schema(properties: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


STRING = {"type": "string", "minLength": 1}
PROJECT_ROOT = {"type": "string", "minLength": 1, "description": "Absolute path to the project root."}
VCS_SCOPE_ID = {"type": "string", "minLength": 1, "maxLength": 128}
VCS_REVISION = {"type": "string", "minLength": 1, "maxLength": 256}
TOOLS = [
    {"name": "dungeon_project_probe", "description": "Diagnose MANAGED, SKLT_ONLY, NOT_DUNGEON, INVALID_MANAGED or UNAVAILABLE before exploration; returns counts, issues and onboarding steps without writes. Use adoption_plan for an uninitialized project.", "inputSchema": _schema(required=["project_root"]), "readOnly": True},
    {"name": "dungeon_adoption_plan", "description": "Plan onboarding of NOT_DUNGEON or SKLT_ONLY without writes. Returns candidate modules, existing IDs, conflicts and plan fingerprint; review accepted modules before explicit adoption_apply.", "inputSchema": _schema({"project_id": STRING}, ["project_root"]), "readOnly": True},
    {"name": "dungeon_adoption_apply", "description": "Apply a reviewed adoption plan only on explicit request to a writable root. Preserves existing SKLT/IDs, generates managed projections and validates; conflicts or changed inputs block writes. Re-plan when blocked.", "inputSchema": _schema({"plan": {"type": "object"}, "accepted_modules": {"type": "array", "items": STRING}}, ["project_root", "plan", "accepted_modules"]), "readOnly": False, "idempotent": True},
    {"name": "dungeon_project_summary", "description": "Read bounded module/object counts in MANAGED or SKLT_ONLY, without file writes or loading all Skeletons. Returns issues and selection hints; probe/adopt when uninitialized.", "inputSchema": _schema(), "readOnly": True},
    {"name": "dungeon_list_modules", "description": "Select modules in MANAGED or SKLT_ONLY using paginated summaries; no writes or source inference. Returns declared identities and counts; select a module then list_objects.", "inputSchema": _schema({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}), "readOnly": True},
    {"name": "dungeon_source_link_audit", "description": "Audit declared source refs in MANAGED or SKLT_ONLY without writes. Returns path/hash CURRENT, STALE, MISSING or UNKNOWN; semantic/symbol aspects remain UNKNOWN without AST/LSP. Review stale refs manually.", "inputSchema": _schema({"object_ids": {"type": "array", "items": STRING, "maxItems": 100}}), "readOnly": True},
    {"name": "dungeon_task_trace_get", "description": "Read one task's object/source/test/evidence links in MANAGED or SKLT_ONLY; no writes or agent scheduling. Returns provenance and issues; create an explicit trace when absent.", "inputSchema": _schema({"task_id": STRING}, ["task_id"]), "readOnly": True},
    {"name": "dungeon_task_trace_list", "description": "Read paginated task links (up to 100 per page) for selected objects in MANAGED or SKLT_ONLY without writes. Traces are external work records, not runtime proof; inspect the relevant task and evidence next.", "inputSchema": _schema({"object_id": STRING, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}), "readOnly": True},
    {"name": "dungeon_task_trace_write", "description": "Record an explicitly requested task/object/source/test/evidence link at a writable MANAGED or SKLT_ONLY root. No scheduling or role authority; stale expected_hash blocks updates. Review returned issues and re-read conflicts.", "inputSchema": _schema({"trace": {"type": "object"}, "expected_hash": STRING}, ["project_root", "trace"]), "readOnly": False},
    {"name": "dungeon_coverage_get", "description": "Read separate Prototype/Product coverage in MANAGED or SKLT_ONLY without writes. UNKNOWN is default; applicability of evidence may downgrade VERIFIED. Inspect mismatches instead of claiming runtime correctness.", "inputSchema": _schema({"object_id": STRING}), "readOnly": True},
    {"name": "dungeon_coverage_set", "description": "Record justified Prototype or Product coverage at a writable MANAGED or SKLT_ONLY root. Does not change SKLT/RARE or EXP; VERIFIED requires applicable CAS evidence and executable hash for EXE tests. Resolve missing proof before retrying.", "inputSchema": _schema({"object_id": STRING, "scope": {"type": "string", "enum": ["PROTOTYPE", "PRODUCT"]}, "state": {"type": "string", "enum": ["UNKNOWN", "NOT_STARTED", "PARTIAL", "IMPLEMENTED", "VERIFIED"]}, "rationale": STRING, "evidence_ids": {"type": "array", "items": STRING}, "task_id": STRING, "executable_path": STRING}, ["project_root", "object_id", "scope", "state", "rationale"]), "readOnly": False},
    {"name": "dungeon_connect_project", "description": "Probe an absolute project path and report its mode and available capabilities. Connection is stateless; pass project_root on later calls.", "inputSchema": _schema(required=["project_root"]), "readOnly": True},
    {"name": "dungeon_status", "description": "Read structural validation, projection freshness, snapshot and evidence-store status.", "inputSchema": _schema(), "readOnly": True},
    {"name": "dungeon_orientation", "description": "Read canonical docs/project.md and docs/map.md before navigating the project.", "inputSchema": _schema(), "readOnly": True},
    {"name": "dungeon_list_sklt", "description": "List active SKLT source paths in a selected project without creating an index or object IDs.", "inputSchema": _schema({"prefix": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}), "readOnly": True},
    {"name": "dungeon_read_sklt", "description": "Read one active SKLT file by project-relative path; no index or projection is changed.", "inputSchema": _schema({"sklt_path": STRING}, ["sklt_path"]), "readOnly": True},
    {"name": "dungeon_list_objects", "description": "List object IDs from the structural index; reports if the index is stale.", "inputSchema": _schema({"module": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}), "readOnly": True},
    {"name": "dungeon_inspect_object", "description": "Inspect an object by object_id and read its SKLT. Linked GLIF can be included on request.", "inputSchema": _schema({"object_id": STRING, "include_glif": {"type": "boolean"}}, ["object_id"]), "readOnly": True},
    {"name": "dungeon_validate", "description": "Validate SKLT, GLIF, dependencies, projections, snapshots and structural references.", "inputSchema": _schema(), "readOnly": True},
    {"name": "dungeon_refresh", "description": "Regenerate derived EXP, grade and object index, then validate. Use after structural changes.", "inputSchema": _schema(), "readOnly": False, "idempotent": True},
    {"name": "dungeon_snapshot", "description": "Create a named structural snapshot; optionally link existing evidence IDs.", "inputSchema": _schema({"name": STRING, "evidence_ids": {"type": "array", "items": STRING}}, ["name"]), "readOnly": False},
    {"name": "dungeon_snapshot_diff", "description": "Compare two named structural snapshots.", "inputSchema": _schema({"old": STRING, "new": STRING}, ["old", "new"]), "readOnly": True},
    {"name": "dungeon_evidence_for", "description": "List evidence linked to an object_id.", "inputSchema": _schema({"object_id": STRING}, ["object_id"]), "readOnly": True},
    {"name": "dungeon_evidence_inspect", "description": "Read an Evidence Manifest and its current verification state.", "inputSchema": _schema({"evidence_id": STRING}, ["evidence_id"]), "readOnly": True},
    {"name": "dungeon_evidence_verify", "description": "Verify an Evidence Manifest, seal and referenced CAS hashes.", "inputSchema": _schema({"evidence_id": STRING}, ["evidence_id"]), "readOnly": True},
    {"name": "dungeon_evidence_create", "description": "Capture an existing draft JSON file as a new Evidence Manifest; draft_path must be absolute.", "inputSchema": _schema({"draft_path": STRING}, ["draft_path"]), "readOnly": False},
    {"name": "dungeon_evidence_seal", "description": "Seal a verified Evidence Manifest; reruns never rewrite a prior record.", "inputSchema": _schema({"evidence_id": STRING}, ["evidence_id"]), "readOnly": False, "idempotent": True},
    {"name": "dungeon_cas_status", "description": "Read availability, object count and bytes of the configured external CAS.", "inputSchema": _schema(), "readOnly": True},
    {"name": "dungeon_cas_verify", "description": "Verify exact CAS bytes against a sha256 reference.", "inputSchema": _schema({"reference": STRING}, ["reference"]), "readOnly": True},
    {"name": "dungeon_cas_put", "description": "Store exact bytes of an existing absolute file path in CAS and return its SHA-256 reference.", "inputSchema": _schema({"file_path": STRING}, ["file_path"]), "readOnly": False, "idempotent": True},
    {"name": "dungeon_vcs_status", "description": "Read declared version-control scope status in MANAGED without writes.", "inputSchema": _schema({"scope_id": VCS_SCOPE_ID}, ["project_root"]), "readOnly": True},
    {"name": "dungeon_vcs_inspect", "description": "Inspect one revision in a declared version-control scope in MANAGED without writes.", "inputSchema": _schema({"scope_id": VCS_SCOPE_ID, "revision": VCS_REVISION}, ["project_root"]), "readOnly": True},
    {"name": "dungeon_vcs_history", "description": "Read bounded history for a declared version-control scope in MANAGED without writes.", "inputSchema": _schema({"scope_id": VCS_SCOPE_ID, "revision": VCS_REVISION, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["project_root"]), "readOnly": True},
    {"name": "dungeon_vcs_diff", "description": "Read a diff for a declared version-control scope in MANAGED without writes.", "inputSchema": _schema({"scope_id": VCS_SCOPE_ID}, ["project_root"]), "readOnly": True},
]
TOOL_DESCRIPTIONS = {
    "dungeon_connect_project": "Compatibility alias of project_probe: diagnose all five project modes, counts and next steps without writes. Connection is stateless; use adoption_plan when uninitialized.",
    "dungeon_status": "Read diagnostics for any project and structural validation/projection status for MANAGED. No writes or runtime proof; NOT_DUNGEON offers adoption steps and INVALID_MANAGED offers repair issues.",
    "dungeon_orientation": "Read bounded orientation docs in MANAGED or doc/SKLT entrypoint paths in SKLT_ONLY. No writes or source inference; select modules/objects next or probe unrecognized roots.",
    "dungeon_list_sklt": "List paginated active SKLT paths in MANAGED or SKLT_ONLY without writes. Excludes snapshots/build outputs; paths are not object identity. Select a file or list_objects; probe other modes.",
    "dungeon_read_sklt": "Read a bounded text page of an active SKLT in MANAGED or SKLT_ONLY without writes. Structural text does not prove runtime behavior; use next_text_offset for more and audit declared source links.",
    "dungeon_list_objects": "Read paginated object summaries in MANAGED or SKLT_ONLY from SKLT in memory, without index writes. Reports explicit/declared-symbol identity and duplicates; choose a module/object then inspect, repair duplicate declarations manually.",
    "dungeon_inspect_object": "Read one bounded SKLT object with dependencies, declared source/GLIF refs and coverage in MANAGED or SKLT_ONLY. No writes or inferred filename links; select from list_objects if missing, then audit refs and verify evidence.",
    "dungeon_validate": "Validate structural consistency and derived artifacts in MANAGED, read-only. SKLT_ONLY uses navigation/audit instead; runtime correctness is outside this result. Repair reported sources then explicitly refresh projections.",
    "dungeon_refresh": "Regenerate EXP, grade and object index and validate in writable MANAGED. Writes derived files only; coverage states remain separate. Repair structural errors before retrying; adopt SKLT_ONLY explicitly first.",
    "dungeon_snapshot": "Write a structural checkpoint in writable MANAGED, optionally linking evidence IDs. Stores no runtime bytes; validation errors block creation. Repair issues or inspect existing snapshots before choosing a new name.",
    "dungeon_snapshot_diff": "Compare two structural checkpoints in MANAGED without writes. Returns structural changes, not runtime comparison; inspect named snapshots when either is missing.",
    "dungeon_evidence_for": "List Evidence Manifests linked to one object in MANAGED without writes. Association alone proves no behavior; inspect/verify a relevant record, and adopt SKLT_ONLY before using its Evidence Store.",
    "dungeon_evidence_inspect": "Read a MANAGED Evidence Manifest and integrity state without writes. Current build applicability requires trace/coverage checks; verify referenced bytes or repair missing records when broken.",
    "dungeon_evidence_verify": "Verify MANAGED manifest/seal/CAS bytes and recorded build links without writes. Textual PASS alone is insufficient runtime proof; inspect build/revision mismatches and recapture evidence for changed bytes.",
    "dungeon_evidence_create": "Capture an explicitly supplied absolute draft as immutable evidence in writable MANAGED. Writes a small manifest after object/CAS checks; executable verification needs build SHA-256 and task/source linkage. Resolve draft issues then verify/seal.",
    "dungeon_evidence_seal": "Seal verified evidence in writable MANAGED, writing an immutable seal. This does not set object coverage; broken bytes block sealing. Recapture a new evidence ID after implementation changes.",
    "dungeon_cas_status": "Read configured MANAGED CAS availability/counts without writes. Missing CAS does not invalidate SKLT; capture chosen bytes explicitly when evidence requires them.",
    "dungeon_cas_verify": "Read and hash one MANAGED CAS reference without writes. Integrity is not behavioral correctness; recapture missing/corrupt bytes before evidence verification.",
    "dungeon_cas_put": "Store exact bytes from an existing absolute file in writable MANAGED CAS, deduplicating by SHA-256. Writes external bytes; no execution or test inference. Verify the returned ref before evidence creation.",
}
for item in TOOLS:
    item["description"] = TOOL_DESCRIPTIONS.get(item["name"], item["description"])
    item["inputSchema"]["properties"]["project_root"] = PROJECT_ROOT
    if not item["readOnly"] and "project_root" not in item["inputSchema"]["required"]:
        item["inputSchema"]["required"].append("project_root")
    if item["name"] in {"dungeon_list_objects", "dungeon_list_sklt"}:
        item["inputSchema"]["properties"]["offset"] = {"type": "integer", "minimum": 0}
    if item["name"] in {"dungeon_read_sklt", "dungeon_inspect_object"}:
        item["inputSchema"]["properties"].update({
            "text_offset": {"type": "integer", "minimum": 0},
            "max_chars": {"type": "integer", "minimum": 1, "maximum": 64_000},
        })
TOOL_BY_NAME = {item["name"]: item for item in TOOLS}
WRITE_TOOL_NAMES = frozenset(name for name, item in TOOL_BY_NAME.items() if not item["readOnly"])
VCS_TOOL_OPERATIONS = {
    "dungeon_vcs_status": "status",
    "dungeon_vcs_inspect": "inspect",
    "dungeon_vcs_history": "history",
    "dungeon_vcs_diff": "diff",
}
VCS_TOOL_NAMES = frozenset(VCS_TOOL_OPERATIONS)


def _tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": item["name"],
            "description": item["description"] + (
                " Pass an absolute project_root on every call for a non-default project."
                if item["readOnly"] else
                " An explicit absolute project_root is required and must be writable."
            ),
            "inputSchema": item["inputSchema"],
            "outputSchema": {"type": "object", "required": ["status", "issues", "next_steps"], "properties": {"status": {"type": "string"}, "issues": {"type": "array", "items": {"type": "object"}}, "next_steps": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": True},
            "annotations": {
                "readOnlyHint": item["readOnly"],
                "destructiveHint": False,
                "idempotentHint": item.get("idempotent", item["readOnly"]),
                "openWorldHint": False,
            },
        }
        for item in TOOLS
    ]


def _default_root() -> Path:
    configured = os.environ.get("DUNGEON_PROJECT_ROOT")
    if not configured:
        raise ValueError("DUNGEON_PROJECT_ROOT is not configured")
    root = _checked_root(configured)
    if not (root / "tools" / "project-structure.py").is_file() or not (root / ".project" / "project.json").is_file():
        raise ValueError(f"Dungeon CLI home root is invalid: {root}")
    return root


def _backend_tools_path() -> str:
    development_tools = Path(__file__).resolve().parents[3]
    candidates = [development_tools]
    configured = os.environ.get("DUNGEON_PROJECT_ROOT")
    if configured:
        candidates.append(Path(configured) / "tools")
    for candidate in candidates:
        if (candidate / "project_structure").is_dir():
            value = str(candidate)
            if value not in sys.path:
                sys.path.insert(0, value)
            return value
    raise RuntimeError("Dungeon project_structure backend is unavailable")


def _root_path(value: str | os.PathLike[str]) -> Path:
    _backend_tools_path()
    from project_structure.v041_paths import UnsafeOutputPath
    raw = os.fspath(value)
    if not isinstance(raw, str) or not raw:
        raise UnsafeOutputPath("project_root must be an absolute directory path")
    parts = re.split(r"[\\/]", raw)
    components = [part for part in parts if part]
    if any(part in {".", ".."} for part in components):
        raise UnsafeOutputPath("project_root contains a traversal or alias component")
    drive = PureWindowsPath(raw).drive
    if raw.startswith(("\\\\", "//")) or any(":" in part for part in components if part != drive):
        raise UnsafeOutputPath("project_root uses an unsupported UNC, device, or alternate-stream path")
    if any(part.endswith((" ", ".")) for part in components if part != drive):
        raise UnsafeOutputPath("project_root contains an ambiguous Windows path component")
    path = Path(raw)
    if not path.is_absolute():
        raise UnsafeOutputPath("project_root must be an absolute directory path")
    return path


def _checked_root(value: str | os.PathLike[str]) -> Path:
    path = _root_path(value)
    from project_structure.v041_paths import UnsafeOutputPath

    from project_structure.v04_sklt import checked_root

    try:
        return checked_root(path)
    except (OSError, ValueError) as exc:
        raise UnsafeOutputPath(str(exc)) from exc


def _validate_write_root(value: str | os.PathLike[str]) -> Path:
    path = _root_path(value)
    from project_structure.v041_paths import UnsafeOutputPath, validate_root

    try:
        validate_root(path)
        return _checked_root(path)
    except UnsafeOutputPath:
        raise
    except (OSError, ValueError) as exc:
        raise UnsafeOutputPath(str(exc)) from exc


def _read_root(value: str | os.PathLike[str]) -> Path:
    path = _root_path(value)
    if os.path.lexists(path):
        return _checked_root(value)
    ancestor = path
    while not os.path.lexists(ancestor):
        parent = ancestor.parent
        if parent == ancestor:
            raise ValueError("project_root has no existing physical ancestor")
        ancestor = parent
    _checked_root(ancestor)
    return path.resolve(strict=False)


def _selected_root(arguments: dict[str, Any], *, for_write: bool = False) -> Path:
    selected = arguments.get("project_root")
    policy = _REQUEST_POLICY.get()
    if policy is not None:
        selected = _required_string(arguments, "project_root")
        root = _read_root(selected)
        if root not in {_read_root(value) for value in policy.project_roots}:
            raise PermissionError("project_root is not authorized for this HTTP client")
    if selected is None:
        return _default_root()
    if for_write:
        return _validate_write_root(_required_string(arguments, "project_root"))
    return _read_root(_required_string(arguments, "project_root"))


def _core():
    _backend_tools_path()
    from project_structure import v04_projects, v04_trace
    return v04_projects, v04_trace


def _active_sklt_paths(root: Path) -> list[str]:
    paths: list[str] = []
    directories = 0
    for current, child_dirs, files in os.walk(root, topdown=True, followlinks=False):
        directories += 1
        if directories > SKLT_SCAN_LIMIT:
            raise ValueError("project has too many directories for bounded SKLT discovery")
        child_dirs[:] = sorted(
            name for name in child_dirs
            if name.lower() not in SKLT_SKIP_DIRS
            and not name.lower().startswith("target-")
            and not (Path(current) / name).is_symlink()
        )
        for name in files:
            if not name.lower().endswith(".sklt"):
                continue
            candidate = Path(current) / name
            try:
                relative = candidate.resolve().relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                paths.append(relative.as_posix())
            if len(paths) > SKLT_SCAN_LIMIT:
                raise ValueError("project has too many active SKLT files")
    return sorted(paths)


def _project_context(arguments: dict[str, Any], *, for_write: bool = False) -> ProjectContext:
    root = _selected_root(arguments, for_write=for_write)
    projects, _ = _core()
    diagnosis = projects.project_probe(root)
    mode = diagnosis.get("mode", diagnosis.get("project_mode", "UNAVAILABLE"))
    if mode == "DUNGEON":
        mode = "MANAGED"
    manifest = None
    if mode == "MANAGED":
        manifest = json.loads(_read_text(_project_file(root, ".project/project.json", ".json")))
    return ProjectContext(root, mode, manifest, diagnosis)


def _writable_roots(*, for_write: bool = False) -> set[Path]:
    policy = _REQUEST_POLICY.get()
    configured = json.dumps(policy.writable_roots) if policy is not None else os.environ.get("DUNGEON_WRITABLE_ROOTS")
    if configured is None:
        return set()
    try:
        values = json.loads(configured)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PermissionError("DUNGEON_WRITABLE_ROOTS is malformed; all writes are denied") from exc
    if type(values) is not list or any(not isinstance(value, str) or not value for value in values):
        raise PermissionError("DUNGEON_WRITABLE_ROOTS must be a JSON array of absolute paths; all writes are denied")
    roots: set[Path] = set()
    try:
        for value in values:
            # A future exact fixture/project path may be configured without
            # invalidating another existing authorized root. Its existing
            # ancestry is checked now; the selected root must exist and pass
            # the full physical lease check before any actual write.
            checker = (_validate_write_root if for_write else _checked_root) if os.path.lexists(Path(value)) else _read_root
            roots.add(checker(value))
    except (OSError, ValueError, TypeError) as exc:
        raise PermissionError("DUNGEON_WRITABLE_ROOTS contains an unsafe root; all writes are denied") from exc
    return roots


def _require_managed(context: ProjectContext, tool_name: str) -> None:
    if context.mode not in {"MANAGED", "DUNGEON"}:
        raise ValueError(f"{tool_name} requires a managed Dungeon project; use dungeon_list_sklt and dungeon_read_sklt for SKLT-only projects")


def _require_write(context: ProjectContext, arguments: dict[str, Any], tool_name: str) -> None:
    _require_managed(context, tool_name)
    _require_explicit_write(context, arguments, tool_name)


def _require_explicit_write(context: ProjectContext, arguments: dict[str, Any], tool_name: str) -> None:
    if "project_root" not in arguments:
        raise PermissionError(f"{tool_name} requires an explicit project_root; writes are denied")
    root = _validate_write_root(_required_string(arguments, "project_root"))
    if root != context.root:
        from project_structure.v041_paths import UnsafeOutputPath
        raise UnsafeOutputPath("the explicit project root changed after the read probe")
    if root not in _writable_roots(for_write=True):
        raise PermissionError(f"{context.root} is read-only for this plugin; add it to DUNGEON_WRITABLE_ROOTS only after approving writes")


def _authorize_write_root(arguments: dict[str, Any], tool_name: str) -> Path:
    if "project_root" not in arguments:
        raise PermissionError(f"{tool_name} requires an explicit project_root; writes are denied")
    selected = arguments.get("project_root")
    if not isinstance(selected, str) or not selected.strip():
        raise PermissionError(f"{tool_name} requires an explicit absolute project_root; writes are denied")
    root = _checked_root(selected)
    if root not in _writable_roots():
        raise PermissionError(f"{root} is read-only for this plugin; add this exact physical root to DUNGEON_WRITABLE_ROOTS only after approving writes")
    root = _validate_write_root(selected)
    if root not in _writable_roots(for_write=True):
        raise PermissionError(f"{root} is read-only for this plugin; add this exact physical root to DUNGEON_WRITABLE_ROOTS only after approving writes")
    return root


def _project_info(context: ProjectContext) -> dict[str, Any]:
    try:
        write_allowed = context.root in _writable_roots()
    except (PermissionError, OSError, ValueError, TypeError):
        write_allowed = False
    return {
        **(context.diagnosis or {}),
        "project_root": str(context.root),
        "mode": context.mode,
        "project_mode": "DUNGEON" if context.mode == "MANAGED" else context.mode,
        "project_id": context.manifest.get("project_id") if context.manifest else None,
        "sklt_files": (context.diagnosis or {}).get("sklt_count"),
        "write_enabled": context.mode == "MANAGED" and write_allowed,
        "metadata_write_enabled": context.mode in {"MANAGED", "SKLT_ONLY"} and write_allowed,
        "connection_persistent": False,
    }


def _required_string(arguments: dict[str, Any], key: str, *, pattern: re.Pattern[str] | None = None) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError(f"{key} must be a non-empty string")
    if pattern and not pattern.fullmatch(value):
        raise ValueError(f"{key} has an invalid format")
    return value


def _object_id(arguments: dict[str, Any], key: str = "object_id") -> str:
    value = _required_string(arguments, key)
    if not _valid_object_id(value):
        raise ValueError(f"{key} is not a valid logical object ID")
    return value


def _valid_object_id(value: Any) -> bool:
    _backend_tools_path()
    from project_structure.v041_ids import valid_object_id
    return valid_object_id(value)


def _required_input_file(arguments: dict[str, Any], key: str) -> str:
    value = _required_string(arguments, key)
    if _REQUEST_POLICY.get() is not None and not Path(value).is_absolute():
        raise ValueError(f"{key} must be an explicit absolute file path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{key} must be an absolute file path")
    if not path.is_file():
        raise ValueError(f"{key} must name an existing file")
    policy = _REQUEST_POLICY.get()
    if policy is not None:
        _checked_root(path.parent)
        if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
            raise PermissionError(f"{key} must not traverse a reparse point")
        allowed = tuple(_checked_root(value) for value in (*policy.project_roots, *policy.input_roots))
        if not any(path.resolve().is_relative_to(root) for root in allowed):
            raise PermissionError(f"{key} is outside this HTTP client's input roots")
    return str(path.resolve())


def _cli(root: Path, *arguments: str) -> tuple[dict[str, Any], bool]:
    _backend_tools_path()
    from project_structure.cli import main

    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        exit_code = main([*arguments, "--root", str(root), "--json"])
    try:
        payload = json.loads(output.getvalue())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Dungeon CLI returned invalid JSON for {arguments[0]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Dungeon CLI returned a non-object result for {arguments[0]}")
    payload["project_root"] = str(root)
    payload["project_mode"] = "DUNGEON"
    payload["mode"] = "MANAGED"
    return payload, exit_code == 0


def _page(rows: list, arguments: dict[str, Any]) -> dict[str, Any]:
    from project_structure.v04_cli import paginate
    return paginate(rows, arguments.get("offset", 0), arguments.get("limit", 100))


def _text_page(text: str, arguments: dict[str, Any]) -> dict[str, Any]:
    offset = arguments.get("text_offset", 0)
    limit = arguments.get("max_chars", 16_000)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 64_000:
        raise ValueError("text_offset must be non-negative and max_chars must be 1..64000")
    end = min(offset + limit, len(text))
    return {"text": text[offset:end], "text_offset": offset, "total_chars": len(text),
            "next_text_offset": end if end < len(text) else None, "truncated": end < len(text)}


def _validate_value(value: Any, schema: dict[str, Any], label: str) -> None:
    kind = schema.get("type")
    valid = {"object": isinstance(value, dict), "array": isinstance(value, list),
             "string": isinstance(value, str), "integer": type(value) is int,
             "boolean": type(value) is bool}.get(kind, True)
    if not valid:
        raise ValueError(f"{label} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{label} must be one of {schema['enum']}")
    if kind == "string" and not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 4096):
        raise ValueError(f"{label} has invalid length")
    if kind == "integer" and not schema.get("minimum", value) <= value <= schema.get("maximum", value):
        raise ValueError(f"{label} is outside its allowed range")
    if kind == "array":
        if len(value) > schema.get("maxItems", 500):
            raise ValueError(f"{label} has too many entries")
        for item in value:
            _validate_value(item, schema.get("items", {}), label)


def _project_file(root: Path, relative: str, suffix: str | None = None) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("structural path escapes Dungeon System root") from exc
    if suffix and candidate.suffix.lower() != suffix:
        raise ValueError(f"expected a {suffix} structural file")
    if not candidate.is_file():
        raise ValueError(f"structural file is missing: {relative}")
    return candidate


def _read_text(path: Path) -> str:
    if path.stat().st_size > 512_000:
        raise ValueError(f"structural file is unexpectedly large: {path.name}")
    return path.read_text(encoding="utf-8")


def _call(name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    if not isinstance(name, str) or name not in TOOL_BY_NAME:
        raise ValueError(f"unknown Dungeon MCP tool: {name}")
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be an object")
    if _REQUEST_POLICY.get() is not None:
        # No default checkout, ambient cwd or another client's environment may
        # select a target in the shared transport. Recheck in the worker too.
        _selected_root(arguments, for_write=name in WRITE_TOOL_NAMES)
        if "executable_path" in arguments:
            _required_input_file(arguments, "executable_path")
    if name in WRITE_TOOL_NAMES:
        _authorize_write_root(arguments, name)
    schema = TOOL_BY_NAME[name]["inputSchema"]
    unexpected = set(arguments) - set(schema["properties"])
    if unexpected:
        raise ValueError(f"unexpected tool argument: {sorted(unexpected)[0]}")
    missing = set(schema["required"]) - set(arguments)
    if missing:
        raise ValueError(f"missing tool argument: {sorted(missing)[0]}")
    if len(json.dumps(arguments, ensure_ascii=False).encode("utf-8")) > 1_048_576:
        raise ValueError("tool arguments exceed 1 MiB")
    for key, value in arguments.items():
        _validate_value(value, schema["properties"][key], key)
    context = _project_context(arguments, for_write=name in WRITE_TOOL_NAMES)
    root = context.root
    if name in VCS_TOOL_NAMES and context.mode not in {"MANAGED", "DUNGEON"}:
        return {
            "status": "FAIL",
            "project_root": str(root),
            "project_mode": context.mode,
            "issues": [{"code": "VCS_REQUIRES_MANAGED_PROJECT", "severity": "ERROR",
                        "message": "Version-control queries require a MANAGED Dungeon project."}],
        }, False
    managed_tools = {"dungeon_validate", "dungeon_refresh", "dungeon_snapshot", "dungeon_snapshot_diff",
                     "dungeon_evidence_for", "dungeon_evidence_inspect", "dungeon_evidence_verify",
                     "dungeon_evidence_create", "dungeon_evidence_seal", "dungeon_cas_status",
                     "dungeon_cas_verify", "dungeon_cas_put", *VCS_TOOL_NAMES}
    metadata_tools = {"dungeon_task_trace_write", "dungeon_coverage_set"}
    if name in managed_tools:
        _require_managed(context, name)
    if not TOOL_BY_NAME[name]["readOnly"]:
        if name in metadata_tools | {"dungeon_adoption_apply"}:
            _require_explicit_write(context, arguments, name)
        else:
            _require_write(context, arguments, name)

    if name in {"dungeon_connect_project", "dungeon_project_probe"}:
        return _project_info(context), True
    projects, trace = _core()
    if name == "dungeon_adoption_plan":
        payload = projects.adoption_plan(root, project_id=arguments.get("project_id"))
        return payload, payload.get("status") not in {"FAIL", "BLOCKED", "ERROR"}
    if name == "dungeon_adoption_apply":
        payload = projects.adoption_apply(root, arguments["plan"], arguments["accepted_modules"])
        return payload, payload.get("status") not in {"FAIL", "BLOCKED", "ERROR"}
    if context.mode not in {"MANAGED", "SKLT_ONLY"}:
        return {**_project_info(context), "status": "BLOCKED"}, False
    if name in VCS_TOOL_OPERATIONS:
        from project_structure.vcs_service import vcs_query

        payload = vcs_query(
            root,
            VCS_TOOL_OPERATIONS[name],
            scope_id=arguments.get("scope_id"),
            revision=arguments.get("revision"),
            limit=arguments.get("limit", 20),
        )
        payload.setdefault("project_root", str(root))
        payload.setdefault("project_mode", "DUNGEON")
        return payload, payload.get("status") in {"PASS", "PARTIAL"}
    if name in {"dungeon_project_summary", "dungeon_list_modules"}:
        catalog = projects.project_catalog(root)
        modules = [{key: row.get(key) for key in ("id", "path", "object_count", "sklt_count", "dialects")}
                   for row in catalog.get("modules", [])]
        page = _page(modules, arguments)
        return {**_project_info(context), "status": "PASS", "modules": page.pop("items"), **page,
                "object_count": len(catalog.get("objects", {})), "issues": catalog.get("issues", [])}, True
    if name == "dungeon_source_link_audit":
        object_ids = arguments.get("object_ids")
        if object_ids is not None and any(not _valid_object_id(value) for value in object_ids):
            raise ValueError("object_ids must contain valid logical object IDs")
        payload = trace.source_link_audit(root, object_ids)
        return payload, payload.get("status") not in {"FAIL", "ERROR", "NOT_FOUND"}
    if name == "dungeon_task_trace_get":
        payload = trace.task_trace_get(root, arguments["task_id"])
        return payload, payload.get("status") not in {"FAIL", "NOT_FOUND", "ERROR"}
    if name == "dungeon_task_trace_list":
        object_id = arguments.get("object_id")
        if object_id is not None:
            object_id = _object_id(arguments)
        payload = trace.task_trace_list(root, object_id, arguments.get("offset", 0), arguments.get("limit", 100))
        return payload, payload.get("status") not in {"FAIL", "ERROR"}
    if name == "dungeon_task_trace_write":
        trace_data = arguments["trace"]
        object_ids = trace_data.get("object_ids")
        if object_ids is not None and (
            not isinstance(object_ids, list)
            or any(not _valid_object_id(value) for value in object_ids)
        ):
            raise ValueError("trace.object_ids must contain valid logical object IDs")
        payload = trace.task_trace_write(root, trace_data, expected_hash=arguments.get("expected_hash"))
        return payload, payload.get("status") not in {"FAIL", "BLOCKED", "CONFLICT", "ERROR"}
    if name == "dungeon_coverage_get":
        object_id = _object_id(arguments) if arguments.get("object_id") is not None else None
        payload = trace.coverage_get(root, object_id=object_id)
        return payload, payload.get("status") not in {"FAIL", "ERROR", "NOT_FOUND"}
    if name == "dungeon_coverage_set":
        payload = trace.coverage_set(root, _object_id(arguments), arguments["scope"], arguments["state"], arguments["rationale"],
                                     evidence_ids=arguments.get("evidence_ids"), task_id=arguments.get("task_id"),
                                     executable_path=arguments.get("executable_path"))
        return payload, payload.get("status") not in {"FAIL", "BLOCKED", "ERROR", "CONFLICT"}

    if name == "dungeon_status":
        if context.mode == "SKLT_ONLY":
            return {
                **_project_info(context),
                "status": "SKLT_ONLY_READ_ONLY",
                "validation": "NOT_APPLICABLE",
                "objects_index": "NOT_APPLICABLE",
            }, True
        return _cli(root, "status")
    if name == "dungeon_orientation":
        if context.mode == "SKLT_ONLY":
            project_skeleton = None
            for relative in ("sklt/project.sklt", "project.sklt"):
                if (root / relative).is_file():
                    project_skeleton = {"path": relative, "read_with": "dungeon_read_sklt"}
                    break
            doc_names = ("SKLT-Writing-Guide.md", "VALIDATION.md", "SOURCE-INDEX.md", "project.md", "map.md")
            return {
                **_project_info(context),
                "project_skeleton": project_skeleton,
                "navigation_docs": [f"docs/{doc}" for doc in doc_names if (root / "docs" / doc).is_file()],
                "note": "Select a module, then a declared object. IDs come from explicit SKLT identities or qualified declared symbols, never from filenames.",
            }, True
        docs = context.manifest.get("docs", {}) if context.manifest else {}
        if not isinstance(docs, dict):
            raise ValueError("Dungeon manifest docs field is invalid")
        project_doc = docs.get("project", "docs/project.md")
        map_doc = docs.get("map", "docs/map.md")
        if not isinstance(project_doc, str) or not isinstance(map_doc, str):
            raise ValueError("Dungeon manifest document paths must be strings")
        return {
            "project_root": str(root),
            "project_mode": context.mode,
            "project": _read_text(_project_file(root, project_doc, ".md"))[:24_000],
            "map": _read_text(_project_file(root, map_doc, ".md"))[:24_000],
        }, True
    if name == "dungeon_list_sklt":
        limit = arguments.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("limit must be an integer from 1 to 500")
        prefix = arguments.get("prefix")
        if prefix is not None and (not isinstance(prefix, str) or not prefix.strip() or len(prefix) > 4096):
            raise ValueError("prefix must be a non-empty string")
        catalog = projects.project_catalog(root)
        paths = [path for path in catalog.get("sklt_paths", []) if prefix is None or path.startswith(prefix.replace("\\", "/"))]
        page = _page(paths, arguments)
        return {
            "project_root": str(root),
            "project_mode": context.mode,
            "paths": page.pop("items"),
            "issues": catalog.get("issues", []),
            **page,
        }, True
    if name == "dungeon_read_sklt":
        relative = _required_string(arguments, "sklt_path")
        if Path(relative).is_absolute():
            raise ValueError("sklt_path must be relative to project_root")
        candidate = _project_file(root, relative, ".sklt")
        normalized = candidate.relative_to(root).as_posix()
        if normalized not in projects.project_catalog(root).get("sklt_paths", []):
            raise ValueError("sklt_path is not an active project SKLT source")
        return {
            "project_root": str(root),
            "project_mode": context.mode,
            "sklt_path": normalized,
            **_text_page(_read_text(candidate), arguments),
        }, True
    if name == "dungeon_list_objects":
        catalog = projects.project_catalog(root)
        module = arguments.get("module")
        objects = catalog.get("objects", {})
        rows = [
            {"object_id": object_id, **{key: record.get(key) for key in ("module", "state", "sklt", "level", "identity_origin", "kind", "identity_conflict")}}
            for object_id, record in sorted(objects.items())
            if isinstance(record, dict) and (module is None or record.get("module") == module)
        ]
        page = _page(rows, arguments)
        return {"project_root": str(root), "mode": context.mode, "project_mode": "DUNGEON" if context.mode == "MANAGED" else context.mode,
                "objects": page.pop("items"), **page, "index_state": catalog.get("index_state", "IN_MEMORY"),
                "index_rebuilt": False, "issues": catalog.get("issues", [])}, True
    if name == "dungeon_inspect_object":
        object_id = _object_id(arguments)
        include_glif = arguments.get("include_glif", False)
        if type(include_glif) is not bool:
            raise ValueError("include_glif must be boolean")
        catalog = projects.project_catalog(root)
        if object_id in catalog.get("duplicates", {}):
            return {"status": "BLOCKED", "project_root": str(root), "object_id": object_id,
                    "issues": [{"code": "DUPLICATE_OBJECT_ID", "message": "Object identity is ambiguous; inspect the declared occurrences explicitly."}],
                    "occurrences": [{"sklt": row.get("sklt"), "line_start": row.get("line_start")} for row in catalog["duplicates"][object_id]],
                    "next_steps": ["Use dungeon_read_sklt on each occurrence and resolve the duplicate identity before object inspection."]}, False
        record = catalog.get("objects", {}).get(object_id)
        if not isinstance(record, dict):
            return {"status": "NOT_FOUND", "project_root": str(root), "project_mode": context.mode, "object_id": object_id}, False
        payload = {"status": "PASS", "project_root": str(root), "mode": context.mode,
                   "project_mode": "DUNGEON" if context.mode == "MANAGED" else context.mode,
                   "object_id": object_id, "index_rebuilt": False, **record}
        sklt_relative = payload.get("sklt")
        if not isinstance(sklt_relative, str):
            raise ValueError("object has no SKLT path in the index")
        text = _read_text(_project_file(root, sklt_relative, ".sklt"))
        start = record.get("line_start")
        end = record.get("line_end")
        if isinstance(start, int) and isinstance(end, int) and 1 <= start <= end:
            text = "\n".join(text.splitlines()[start-1:end])
        payload["skeleton"] = {"path": sklt_relative, **_text_page(text, arguments)}
        payload["issues"] = catalog.get("issues", [])
        payload["coverage"] = trace.coverage_get(root, object_id=object_id)
        if include_glif:
            payload["linked_glif"] = [
                {"path": relative, **_text_page(_read_text(_project_file(root, relative, ".glif")), arguments)}
                for relative in payload.get("glif", [])[:10]
            ]
        return payload, True
    if name == "dungeon_validate":
        return _cli(root, "validate")
    if name == "dungeon_refresh":
        steps = []
        for command in ("refresh", "validate"):
            payload, ok = _cli(root, command)
            steps.append({"command": command, "result": payload})
            if not ok:
                return {"status": "FAIL", "steps": steps}, False
        return {"status": "PASS", "steps": steps}, True
    if name == "dungeon_snapshot":
        snapshot_name = _required_string(arguments, "name", pattern=NAME_RE)
        evidence_ids = arguments.get("evidence_ids", [])
        if not isinstance(evidence_ids, list) or any(not isinstance(item, str) or not EVIDENCE_ID_RE.fullmatch(item) for item in evidence_ids):
            raise ValueError("evidence_ids must be an array of evidence IDs")
        options = [option for evidence_id in evidence_ids for option in ("--evidence-id", evidence_id)]
        return _cli(root, "snapshot", snapshot_name, *options)
    if name == "dungeon_snapshot_diff":
        return _cli(root, "snapshot-diff", _required_string(arguments, "old", pattern=NAME_RE), _required_string(arguments, "new", pattern=NAME_RE))
    if name == "dungeon_evidence_for":
        return _cli(root, "evidence-for", _object_id(arguments))
    if name in {"dungeon_evidence_inspect", "dungeon_evidence_verify", "dungeon_evidence_seal"}:
        evidence_id = _required_string(arguments, "evidence_id", pattern=EVIDENCE_ID_RE)
        command = name.replace("dungeon_", "").replace("_", "-")
        return _cli(root, command, evidence_id)
    if name == "dungeon_evidence_create":
        return _cli(root, "evidence-create", _required_input_file(arguments, "draft_path"))
    if name == "dungeon_cas_status":
        return _cli(root, "cas-status")
    if name == "dungeon_cas_verify":
        reference = _required_string(arguments, "reference")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
            raise ValueError("reference must be sha256:<64 lowercase hex characters>")
        return _cli(root, "cas-verify", reference)
    if name == "dungeon_cas_put":
        return _cli(root, "cas-put", _required_input_file(arguments, "file_path"))
    raise ValueError(f"unsupported Dungeon MCP tool: {name}")


def _send(message: dict[str, Any]) -> None:
    encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _tool_result(payload: dict[str, Any], ok: bool) -> dict[str, Any]:
    payload = dict(payload)

    def contains_unsafe(value: Any) -> bool:
        if isinstance(value, str):
            return "UNSAFE_OUTPUT_PATH" in value
        if isinstance(value, dict):
            return any(contains_unsafe(key) or contains_unsafe(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(contains_unsafe(item) for item in value)
        return False

    if contains_unsafe(payload):
        ok = False
        issues = payload.get("issues")
        if not isinstance(issues, list):
            issues = []
            payload["issues"] = issues
        promoted = False
        for issue in issues:
            if isinstance(issue, dict) and (
                issue.get("code") == "UNSAFE_OUTPUT_PATH" or contains_unsafe(issue)
            ):
                issue["code"] = "UNSAFE_OUTPUT_PATH"
                issue.setdefault("severity", "ERROR")
                promoted = True
        if not promoted:
            issues.append({"code": "UNSAFE_OUTPUT_PATH", "severity": "ERROR",
                           "message": "The requested root or output path is unsafe."})
        payload["status"] = "FAIL"
    payload.setdefault("status", "PASS" if ok else "FAIL")
    payload.setdefault("issues", [])
    payload.setdefault("next_steps", [])
    if not ok and not payload["issues"]:
        payload["issues"] = [{"code": "OPERATION_FAILED", "severity": "ERROR",
                              "message": str(payload.get("error", f"Operation returned {payload['status']}."))}]
    if not ok and not payload["next_steps"]:
        payload["next_steps"] = ["Probe the explicit project root and review the operation's inputs and reported issues before retrying."]
    def wrap(value: dict[str, Any], successful: bool) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": serialized}],
            "structuredContent": value,
            "isError": not successful,
        }

    result = wrap(payload, ok)
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_RESPONSE_BYTES:
        payload = {"status": "FAIL", "issues": [{"code": "RESPONSE_TOO_LARGE", "message": "Response exceeds the bounded MCP output size."}],
                   "next_steps": ["Select one module/object, reduce limit, or read the next text page."]}
        result = wrap(payload, False)
    return result


_MISSING = object()


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": None if request_id is _MISSING else request_id,
            "error": {"code": code, "message": message}}


def _request_error(request: Any) -> dict[str, Any] | None:
    if not isinstance(request, dict):
        return _rpc_error(_MISSING, -32600, "Invalid Request")
    request_id = request.get("id", _MISSING)
    if set(request) - {"jsonrpc", "id", "method", "params"}:
        return _rpc_error(request_id, -32600, "Invalid Request: unknown envelope field")
    if request.get("jsonrpc") != "2.0":
        return _rpc_error(request_id, -32600, "Invalid Request: jsonrpc must be 2.0")
    method = request.get("method")
    if not isinstance(method, str) or not method:
        return _rpc_error(request_id, -32600, "Invalid Request: method must be a non-empty string")
    if request_id is not _MISSING and (
        isinstance(request_id, bool)
        or not isinstance(request_id, (str, int, float, type(None)))
        or (isinstance(request_id, float) and not math.isfinite(request_id))
    ):
        return _rpc_error(_MISSING, -32600, "Invalid Request: invalid id")
    params = request.get("params", {})
    if not isinstance(params, dict):
        return _rpc_error(request_id, -32602, "Invalid params: params must be an object")

    allowed_params = {
        "initialize": {"protocolVersion", "capabilities", "clientInfo"},
        "tools/call": {"name", "arguments"},
        "notifications/initialized": set(),
        "ping": set(),
        "tools/list": set(),
        "resources/list": set(),
        "prompts/list": set(),
    }.get(method)
    # MCP RequestParams reserves _meta for transport metadata, not tool
    # arguments. Real Codex sends a progressToken on tools/list during startup.
    # Vendor metadata remains bounded by the wire-size limit and is never used
    # for authorization, project selection or proof evaluation.
    if allowed_params is not None:
        allowed_params = allowed_params | {"_meta"}
    if allowed_params is not None and set(params) - allowed_params:
        return _rpc_error(request_id, -32602, "Invalid params: unknown field")
    if "_meta" in params:
        metadata = params["_meta"]
        if not isinstance(metadata, dict):
            return _rpc_error(request_id, -32602, "Invalid params: _meta must be an object")
        if "progressToken" in metadata:
            token = metadata["progressToken"]
            if isinstance(token, bool) or not isinstance(token, (str, int, float)) or (
                isinstance(token, float) and not math.isfinite(token)
            ):
                return _rpc_error(request_id, -32602, "Invalid params: progressToken must be a string or finite number")
    if method == "tools/call":
        if not isinstance(params.get("name"), str) or not params["name"]:
            return _rpc_error(request_id, -32602, "Invalid params: name must be a non-empty string")
        if "arguments" in params and not isinstance(params["arguments"], dict):
            return _rpc_error(request_id, -32602, "Invalid params: arguments must be an object")
    if method == "initialize":
        if not isinstance(params.get("protocolVersion"), str):
            return _rpc_error(request_id, -32602, "Invalid params: protocolVersion must be a string")
        if not isinstance(params.get("capabilities"), dict) or not isinstance(params.get("clientInfo"), dict):
            return _rpc_error(request_id, -32602, "Invalid params: capabilities and clientInfo must be objects")

    return None


def _handle(request: Any) -> dict[str, Any] | None:
    error = _request_error(request)
    if error is not None:
        return error
    request_id = request.get("id", _MISSING)
    method = request["method"]
    params = request.get("params", {})

    if request_id is _MISSING:
        if method == "notifications/initialized":
            return None
        # A valid notification does not receive a response; unknown notification
        # methods are also silently ignored as required by JSON-RPC 2.0.
        if method not in {"initialize", "tools/call", "tools/list", "ping", "resources/list", "prompts/list"}:
            return None
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested in HANDSHAKE_VERSIONS else PROTOCOL_VERSION
        result = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "dungeon-system", "version": VERSION},
            "instructions": "Probe the explicit project_root before exploration. NOT_DUNGEON needs a reviewed adoption plan and explicit apply; SKLT_ONLY supports module/object navigation without adoption. Select summary, module, object, SKLT, Task Trace, source and Toolkit evidence. Keep Prototype/Product coverage separate from SKLT/RARE. Writes require an explicit allowed root; read calls never create an index. Legacy dungeon_connect_project aliases project_probe.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": _tool_list()}
    elif method == "tools/call":
        try:
            payload, ok = _call(params["name"], params.get("arguments", {}))
            result = _tool_result(payload, ok)
        except Exception as exc:
            unsafe = type(exc).__name__ == "UnsafeOutputPath" or "UNSAFE_OUTPUT_PATH" in str(exc)
            code = "UNSAFE_OUTPUT_PATH" if unsafe else "PROJECT_READ_ONLY" if isinstance(exc, PermissionError) else "INVALID_ARGUMENT" if isinstance(exc, (ValueError, TypeError, KeyError)) else "OPERATION_FAILED"
            result = _tool_result({"error": f"{type(exc).__name__}: {exc}",
                                   "issues": [{"code": code, "message": str(exc), "severity": "ERROR"}],
                                   "next_steps": ["Run dungeon_project_probe for the explicit root, then review the selected operation and its inputs."]}, False)
    elif method in {"resources/list", "prompts/list"}:
        result = {"resources": []} if method == "resources/list" else {"prompts": []}
    else:
        return _rpc_error(request_id, -32601, "Method not found")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant: {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON object field: {key}")
        result[key] = value
    return result


def main() -> None:
    for raw in sys.stdin.buffer:
        request_id = _MISSING
        try:
            if len(raw) > 1_048_576:
                response = _rpc_error(_MISSING, -32600, "Invalid Request: request exceeds 1 MiB")
            else:
                request = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                response = _handle(request)
        except (ValueError, json.JSONDecodeError) as exc:
            response = _rpc_error(request_id, -32700, str(exc))
        if response is not None:
            _send(response)


if __name__ == "__main__":
    main()
